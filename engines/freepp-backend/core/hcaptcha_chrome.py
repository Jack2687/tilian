"""Chrome semi-hybrid hCaptcha mint (Stripe invisible)。

突破链路 (2026-08-04 验证):
  Chrome 加载 Stripe hCaptcha invisible 页面 → hcaptcha.render + execute
  → 自动 PoW + getcaptcha (octet-stream) → token 经 getResponse 轮询拿到。

关键发现:
  - checksiteconfig v 参数是 hCaptcha 版本 hash (ba51eebd...) 不是 UUID
  - execute 必须传 widgetId (字符串), 传 div 元素会抛 invalid-captcha-id
  - callback 可能不触发, 需轮询 hcaptcha.getResponse(widgetId)
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit, unquote

SITEKEY = "463b917e-e264-403f-ad34-34af0ee10294"
IFRAME_URL = (
    "https://b.stripecdn.com/stripethirdparty-srv/assets/v33.5/"
    f"HCaptchaInvisible.html?siteKey={SITEKEY}"
)
DEFAULT_PROXY = "http://127.0.0.1:7890"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
# 会话 cookie 注入域：Stripe 资产/CDN 与主 API 分属两个注册域。
_COOKIE_DOMAINS = (".stripe.com", ".stripecdn.com")


def _proxy_for_playwright(proxy: str):
    """把 curl_cffi 格式的代理（可能 socks5h://user:pass@host:port）转成 Playwright 可用的配置。

    Chromium 不支持带认证的 socks5，遇到时起一个本地无认证 socks5 桥，
    出口 IP 不变。返回 {"server": ..., "username": ..., "password": ...} 或 None。
    """
    parsed = urlsplit(str(proxy or ""))
    if not parsed.hostname:
        return None
    scheme = parsed.scheme or "http"
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme == "socks5" and (parsed.username or parsed.password):
        # Chromium 不支持带认证的 socks5 → 本地桥
        try:
            bridge = _LocalSocks5Bridge(str(proxy or ""))
            return {"server": "http://127.0.0.1:" + str(bridge.port), "_bridge": bridge}
        except Exception:
            return None
    server = scheme + "://" + parsed.hostname
    if parsed.port:
        server += ":" + str(parsed.port)
    out = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


class _LocalSocks5Bridge:
    """本地无认证 HTTP 代理桥，把连接转发到带认证的 socks5 上游（出口 IP 不变）。

    移植自 gcash_engine/gcash_custom_flow.py 的 _LocalSocks5Bridge。
    """

    def __init__(self, proxy: str):
        import socket as _s
        self._sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        self._sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        parsed = urlsplit(proxy)
        self._upstream = (parsed.hostname, parsed.port or 1080)
        self._auth = (unquote(parsed.username or ""), unquote(parsed.password or ""))
        self._closed = False
        import threading as _t
        self._thread = _t.Thread(target=self._accept_loop, name="hcaptcha-socks5-bridge", daemon=True)
        self._thread.start()

    def _accept_loop(self):
        import socket as _s
        import threading as _t
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                _t.Thread(target=self._serve, args=(conn,), daemon=True).start()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    def _serve(self, conn):
        import socket as _s
        up = None
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
                if len(data) > 65536:
                    return
            head = data.split(b"\r\n", 1)[0].decode("latin1", "replace")
            parts = head.split(" ", 2)
            method = parts[0].upper() if parts else ""
            if method == "CONNECT":
                target = parts[1] if len(parts) > 1 else ""
                if ":" not in target:
                    return
                host, port_s = target.rsplit(":", 1)
                port = int(port_s)
            else:
                target = parts[1] if len(parts) > 1 else ""
                parsed_uri = urlsplit(target if "://" in target else "http://" + target)
                if not parsed_uri.hostname:
                    return
                host = parsed_uri.hostname
                port = parsed_uri.port or 80
            up = _s.create_connection(self._upstream, timeout=20)
            self._socks5_handshake(up, host, port)
            if method == "CONNECT":
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._pump(conn, up)
            else:
                path_and_query = (parsed_uri.path or "/") + (("?" + parsed_uri.query) if parsed_uri.query else "")
                rest = data.split(b"\r\n", 1)[1] if b"\r\n" in data else b""
                rewritten = (method + " " + path_and_query + " HTTP/1.1\r\n").encode("latin1") + rest
                up.sendall(rewritten)
                self._pump(conn, up)
        except Exception:
            pass
        finally:
            try:
                if up is not None:
                    up.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _socks5_handshake(self, up, host, port):
        import socket as _s
        user, pwd = self._auth
        up.sendall(bytes([0x05, 0x02, 0x00, 0x02]))
        resp = self._recv_exact(up, 2)
        if resp[1] != 0x02:
            raise RuntimeError("socks5 auth method rejected")
        ub = user.encode("utf-8")
        pb = pwd.encode("utf-8")
        up.sendall(bytes([0x01, len(ub)]) + ub + bytes([len(pb)]) + pb)
        resp = self._recv_exact(up, 2)
        if resp[1] != 0x00:
            raise RuntimeError("socks5 auth failed")
        hb = host.encode("ascii")
        up.sendall(bytes([0x05, 0x01, 0x00, 0x03, len(hb)]) + hb + port.to_bytes(2, "big"))
        resp = self._recv_exact(up, 4)
        if resp[1] != 0x00:
            raise RuntimeError("socks5 connect failed")
        atyp = resp[3]
        if atyp == 0x01:
            self._recv_exact(up, 4 + 2)
        elif atyp == 0x03:
            l = self._recv_exact(up, 1)[0]
            self._recv_exact(up, l + 2)
        else:
            raise RuntimeError("socks5 atyp unsupported")

    @staticmethod
    def _recv_exact(sock, n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("EOF")
            data += chunk
        return data

    def _pump(self, left, right):
        import select as _sel
        try:
            while not self._closed:
                r, _, _ = _sel.select([left, right], [], [], 0.5)
                for sock in r:
                    data = sock.recv(65536)
                    if not data:
                        return
                    dst = right if sock is left else left
                    try:
                        dst.sendall(data)
                    except OSError:
                        return
        except OSError:
            return

    def close(self):
        self._closed = True
        try:
            self._sock.close()
        except Exception:
            pass

RENDER_JS = """
() => new Promise((resolve, reject) => {
  if (!window.hcaptcha) return reject(new Error('no hcaptcha object'));
  const div = document.createElement('div');
  div.id = 'hcap-mint-widget';
  document.body.appendChild(div);
  let done = false;
  try {
    const widgetId = window.hcaptcha.render(div, {
      sitekey: __hcapSitekey,
      size: 'invisible',
      'error-callback': () => { done = true; resolve(null); },
    });
    let cbToken = null;
    window.hcaptcha.execute(widgetId, (tok) => { cbToken = tok; });
    window.hcaptcha.execute(widgetId);
    const poll = setInterval(() => {
      try {
        const t = window.hcaptcha.getResponse(widgetId);
        const tok = (t && t.length > 20) ? t : (cbToken && cbToken.length > 20 ? cbToken : null);
        if (tok && !done) {
          done = true; clearInterval(poll);
          // ekey = hCaptcha 的响应密钥（Stripe passive_captcha_ekey），可能为空。
          let ekey = null;
          try { ekey = window.hcaptcha.getRespKey ? window.hcaptcha.getRespKey() : null; } catch (e) {}
          resolve({ token: tok, ekey: (ekey && String(ekey)) || "" });
        }
      } catch (e) {}
    }, 1000);
    setTimeout(() => { if (!done) { done = true; clearInterval(poll); resolve(null); } }, 60000);
  } catch (e) {
    done = true;
    resolve(null);
  }
})
"""


def mint_hcaptcha_chrome(
    proxy: str = DEFAULT_PROXY,
    sitekey: str = SITEKEY,
    timeout: float = 90,
    headless: bool = True,
    user_agent: str = "",
    cookies: dict[str, str] | None = None,
    executable_path: str = "",
) -> dict[str, Any]:
    """用真实 Chrome 拿 Stripe hCaptcha passive token。

    Returns:
        {ok, token, ekey, token_len, elapsed_ms, error}
    """
    t0 = time.time()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright 未安装 (pip install playwright)"}

    # 把 curl_cffi 格式代理（可能 socks5h://user:pass@...）转成 Playwright 可用配置；
    # 带认证 socks5 会起本地桥。转换失败或无代理时直连。
    _pw_proxy = None
    _bridge = None
    if proxy:
        _pw_proxy = _proxy_for_playwright(proxy)
        if isinstance(_pw_proxy, dict):
            _bridge = _pw_proxy.pop("_bridge", None)
        if not _pw_proxy:
            return {"ok": False, "error": "proxy 格式无法转换给浏览器（" + str(proxy)[:40] + "…）"}

    try:
        with sync_playwright() as pwt:
            _launch_kwargs = {
                "headless": headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            # 浏览器选择：显式传入的 executable_path > 系统 Chrome > Playwright 自带 Chromium。
            # 显式路径供服务器在"未下载 Playwright 浏览器但装了 Chrome/Edge"时使用。
            chosen_browser = str(executable_path or "").strip()
            if chosen_browser and os.path.exists(chosen_browser):
                _launch_kwargs["executable_path"] = chosen_browser
            elif os.path.exists(CHROME):
                _launch_kwargs["executable_path"] = CHROME
            if _pw_proxy:
                _launch_kwargs["proxy"] = _pw_proxy
            try:
                browser = pwt.chromium.launch(**_launch_kwargs)
            except Exception as launch_exc:
                message = str(launch_exc)[:200]
                hint = ""
                if "Executable doesn't exist" in message or "playwright install" in message:
                    hint = ("；本机缺少 Playwright 浏览器：在引擎 Python 环境执行 "
                            "python -m playwright install chromium，或用 executable_path / "
                            "UPI_HCAPTCHA_CHROME_EXECUTABLE 指向已安装的 Chrome/Edge")
                return {"ok": False, "error": "chromium 启动失败: " + message + hint}
            # UA 对齐提交 confirm 的客户端，避免 hCaptcha/Stripe 因指纹不一致
            # 拒绝 passive token（旧硬编码 Chrome/146 曾导致 soft_reject）。
            page = browser.new_page(user_agent=str(user_agent or "").strip() or DEFAULT_UA)
            if cookies:
                try:
                    # cookies 为 {name: value} 映射（会话域信息在调用侧已丢失），
                    # 按 Stripe 两个注册域注入；失败不影响求解主流程。
                    jar_items = []
                    for _name, _value in cookies.items():
                        if not _name or _value is None:
                            continue
                        for _domain in _COOKIE_DOMAINS:
                            jar_items.append({"name": str(_name), "value": str(_value), "domain": _domain, "path": "/"})
                    if jar_items:
                        page.context.add_cookies(jar_items)
                except Exception:
                    pass
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            try:
                page.goto(IFRAME_URL, wait_until="domcontentloaded", timeout=min(timeout, 60) * 1000)
            except Exception as e:
                browser.close()
                if _bridge is not None:
                    try:
                        _bridge.close()
                    except Exception:
                        pass
                return {"ok": False, "error": f"goto: {e}"}
            page.wait_for_timeout(2500)
            has_hcap = page.evaluate("() => typeof window.hcaptcha")
            if has_hcap == "undefined":
                browser.close()
                if _bridge is not None:
                    try:
                        _bridge.close()
                    except Exception:
                        pass
                return {"ok": False, "error": "no hcaptcha object on page"}
            page.evaluate(f"() => {{ window.__hcapSitekey = '{sitekey}'; }}")
            result = page.evaluate(RENDER_JS)
            browser.close()
    except Exception as e:
        if _bridge is not None:
            try:
                _bridge.close()
            except Exception:
                pass
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if _bridge is not None:
        try:
            _bridge.close()
        except Exception:
            pass

    elapsed = int((time.time() - t0) * 1000)
    if result and isinstance(result, dict) and result.get("token"):
        return {
            "ok": True,
            "token": result["token"],
            "ekey": result.get("ekey") or "",
            "token_len": len(result["token"]),
            "elapsed_ms": elapsed,
        }
    return {"ok": False, "error": "no token in 60s (soft-reject or challenge)", "elapsed_ms": elapsed}


def save_token(token: str, path: str = "_hcap_chrome_token.txt") -> str:
    import os
    with open(path, "w", encoding="utf-8") as f:
        f.write(token)
    return os.path.abspath(path)


if __name__ == "__main__":
    import json
    res = mint_hcaptcha_chrome()
    print(json.dumps({k: v[:90] + "..." if k == "token" else v for k, v in res.items()}, indent=2))
    if res.get("ok"):
        save_token(res["token"])
