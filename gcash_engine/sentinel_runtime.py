# -*- coding: utf-8 -*-
"""GCash checkout 的浏览器信任上下文。

这里不保存 HAR 里的短期 token。Sentinel 每次通过当前 SDK/当前出口重新生成；
部署证明只从当前浏览器请求中捕获，或由运行环境显式注入。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, unquote, urlsplit


_ASSETS = Path(__file__).with_name("sentinel_assets")
_BRIDGE = _ASSETS / "sentinel_bridge.js"
_DEFAULT_SENTINEL_VERSION = "20260219f9f6"
_SENTINEL_VERSION_RE = re.compile(r"/sentinel/([^/\"'?]+)/sdk\.js")
_ATTESTATION_RE = re.compile(r"(eyJ[A-Za-z0-9_-]{120,400}\.[A-Za-z0-9_-]{40,60})")
_NAMED_ATTESTATION_RE = re.compile(
    r'''(?:webDeploymentAttestation|oai-web-deployment-attestation)["']?\s*[:=]\s*["']([^"']+)["']''',
    re.I,
)


_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def normalize_cookie_mapping(value) -> dict[str, str]:
    """Normalize a Cookie header, mapping, or Playwright cookie list."""
    out: dict[str, str] = {}

    def add(name, cookie_value) -> None:
        name = str(name or "").strip()
        cookie_value = str(cookie_value if cookie_value is not None else "").strip()
        if (
            not name
            or not cookie_value
            or len(name) > 256
            or len(cookie_value) > 8192
            or not _COOKIE_NAME_RE.fullmatch(name)
            or "\r" in cookie_value
            or "\n" in cookie_value
        ):
            return
        out[name] = cookie_value

    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith(("{", "[")):
            try:
                return normalize_cookie_mapping(json.loads(raw))
            except json.JSONDecodeError:
                pass
        for item in raw.split(";"):
            name, separator, cookie_value = item.strip().partition("=")
            if separator:
                add(name, cookie_value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                add(item.get("name"), item.get("value"))
    elif isinstance(value, dict):
        for name, cookie_value in value.items():
            if isinstance(cookie_value, dict):
                cookie_value = cookie_value.get("value")
            add(name, cookie_value)
    return out


def cookie_header_from_mapping(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in normalize_cookie_mapping(cookies).items())


def seed_login_cookies(device_id: str, session_token: str = "", cookies=None) -> dict[str, str]:
    out = normalize_cookie_mapping(cookies)
    # The generated browser identity is authoritative for this flow.
    out["oai-did"] = str(device_id or "").strip()
    session_token = str(session_token or "").strip()
    if session_token:
        # Session JSON exports do not say which NextAuth cookie name was used.
        # Sending both names matches the compatibility behavior used elsewhere
        # in this project; the server ignores the name it does not recognize.
        out["__Secure-next-auth.session-token"] = session_token
        out["next-auth.session-token"] = session_token
    return {name: value for name, value in out.items() if value}


def _decode_attestation_payload(value: str) -> dict:
    try:
        head = str(value or "").split(".", 1)[0]
        head += "=" * (-len(head) % 4)
        result = json.loads(base64.urlsafe_b64decode(head).decode("utf-8"))
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def client_version_from_attestation(value: str) -> str:
    deploy_id = str(_decode_attestation_payload(value).get("deployId") or "").strip()
    return "prod-" + deploy_id if deploy_id else ""


def find_attestation(value: str) -> str:
    """从 HTML/hydration JSON 找出结构正确且未过期的部署证明。"""
    now = int(time.time())
    text = str(value or "")
    candidates = [match.group(1) for match in _NAMED_ATTESTATION_RE.finditer(text)]
    candidates.extend(match.group(1) for match in _ATTESTATION_RE.finditer(text))
    for candidate in candidates:
        payload = _decode_attestation_payload(candidate)
        if (
            payload.get("version") == 1
            and payload.get("deployId")
            and payload.get("subject")
            and int(payload.get("expiresAt") or 0) > now
        ):
            return candidate
    return ""


def attestation_is_current(value: str) -> bool:
    payload = _decode_attestation_payload(value)
    return bool(
        payload.get("version") == 1
        and payload.get("deployId")
        and payload.get("subject")
        and int(payload.get("expiresAt") or 0) > int(time.time())
    )


def discover_sentinel_version(response_text: str) -> str:
    match = _SENTINEL_VERSION_RE.search(str(response_text or ""))
    return match.group(1) if match else _DEFAULT_SENTINEL_VERSION


@dataclass
class BrowserBootstrap:
    attestation: str = ""
    client_version: str = ""
    client_build: str = ""
    observation: str = ""
    sentinel_version: str = _DEFAULT_SENTINEL_VERSION
    cookies: dict[str, str] = field(default_factory=dict)
    browser_cookies: dict[str, str] = field(default_factory=dict)
    stripe_ids: dict[str, str] = field(default_factory=dict)
    sentinel_token: str = ""
    sentinel_flow: str = ""
    source: str = ""
    error: str = ""


def mint_sentinel(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str,
    page_url: str,
    language: str,
    timezone: str,
    cookies: dict[str, str],
    version: str = "",
    timeout_s: float = 120.0,
) -> tuple[str, str]:
    """运行真实 Sentinel SDK bridge，返回 (main token, SO token)。"""
    if not _BRIDGE.is_file():
        raise RuntimeError("Sentinel bridge 资源缺失")
    node = str(os.environ.get("SENTINEL_NODE") or "node").strip()
    payload = {
        "ua": str(user_agent or ""),
        "cores": 8,
        "deviceId": str(device_id or ""),
        "flow": str(flow or ""),
        "proxy": str(proxy or ""),
        "version": str(version or _DEFAULT_SENTINEL_VERSION),
        "pageUrl": str(page_url or "https://chatgpt.com/"),
        "language": str(language or "en-US"),
        "timezone": str(timezone or "UTC"),
        "cookieHeader": cookie_header_from_mapping(cookies),
        "sentinelOrigin": "https://chatgpt.com",
    }
    try:
        # The browser fallback bridge can spawn sentinel_curl_fetch.py for
        # SOCKS/non-HTTP proxies. Reuse this engine's interpreter explicitly.
        bridge_env = os.environ.copy()
        bridge_env.setdefault("PYTHON_BIN", sys.executable)
        process = subprocess.run(
            [node, str(_BRIDGE)],
            input=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            capture_output=True,
            timeout=max(10.0, float(timeout_s)),
            env=bridge_env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Sentinel 需要 Node.js") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Sentinel 动态令牌生成超时") from exc

    output = (process.stdout or b"").decode("utf-8", "replace").strip()
    if not output:
        detail = (process.stderr or b"").decode("utf-8", "replace")[:300]
        raise RuntimeError("Sentinel bridge 无输出: " + detail)
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Sentinel bridge 输出不是 JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Sentinel bridge 输出不是 JSON 对象")
    if result.get("error"):
        raise RuntimeError("Sentinel bridge 失败: " + str(result.get("error"))[:300])
    main = str(result.get("main") or "")
    so = str(result.get("so") or "")
    try:
        decoded = json.loads(main)
    except Exception as exc:
        raise RuntimeError("Sentinel 主令牌格式无效") from exc
    if (
        decoded.get("id") != device_id
        or decoded.get("flow") != flow
        or not decoded.get("c")
        or not decoded.get("p")
    ):
        raise RuntimeError("Sentinel 令牌与当前 device/flow 不一致")
    return main, so


def _playwright_proxy(proxy: str) -> Optional[dict]:
    parsed = urlsplit(str(proxy or ""))
    if not parsed.hostname:
        return None
    scheme = "socks5" if parsed.scheme == "socks5h" else (parsed.scheme or "http")
    server = f"{scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    value = {"server": server}
    if parsed.username:
        value["username"] = unquote(parsed.username)
    if parsed.password:
        value["password"] = unquote(parsed.password)
    return value


def capture_browser_bootstrap(
    *,
    access_token: str,
    session_token: str,
    cookies: Optional[dict[str, str]] = None,
    proxy: str,
    device_id: str,
    session_id: str,
    fingerprint: Optional[dict],
    executable_path: str,
    browser_type: str = "chromium",
    page_url: str = "",
    sentinel_flow: str = "",
    init_script: str = "",
    emit: Optional[Callable[[str, str, str], None]] = None,
    timeout_s: float = 45.0,
) -> BrowserBootstrap:
    """用当前网页部署预热会话并捕获 attestation/version/cookies。

    attestation 是浏览器部署层动态加入请求的签名，不能从旧 HAR 长期复用。
    该函数只捕获浏览器真正发出的头，不伪造签名。
    """
    log = emit or (lambda stage, status, message: None)
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    env_attestation = str(
        fp.get("web_deployment_attestation")
        or os.environ.get("GCASH_WEB_DEPLOYMENT_ATTESTATION")
        or ""
    ).strip()
    result = BrowserBootstrap(
        attestation=env_attestation,
        client_version=str(fp.get("oai_client_version") or os.environ.get("CHATGPT_CLIENT_VERSION") or "").strip(),
        client_build=str(fp.get("oai_client_build_number") or os.environ.get("CHATGPT_CLIENT_BUILD") or "").strip(),
        source="injected" if env_attestation else "",
        cookies=seed_login_cookies(device_id, session_token, cookies),
    )
    if env_attestation and not attestation_is_current(env_attestation):
        result.attestation = ""
        result.source = ""
        result.error = "注入的部署证明格式无效或已经过期"
    if result.attestation and not result.client_version:
        result.client_version = client_version_from_attestation(result.attestation)
    browser_type = str(browser_type or "chromium").strip().lower()
    if browser_type not in {"chromium", "firefox"}:
        result.error = f"不支持的浏览器类型: {browser_type}"
        return result
    if not executable_path and browser_type == "chromium":
        result.error = "没有可用 Chromium，无法动态捕获部署证明"
        return result

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        result.error = "Playwright 不可用: " + str(exc)[:160]
        return result

    ua = str(fp.get("ua") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
    locale = str(fp.get("locale") or fp.get("oai_language") or "en-US")
    timezone = str(fp.get("timezone") or "UTC")
    home_url = str(page_url or "https://chatgpt.com/?promo_campaign=plus-1-month-free")
    deadline = time.monotonic() + max(10.0, float(timeout_s))
    capture_timeout_ms = int(min(max(1.0, float(timeout_s)), 10.0) * 1000)
    browser = None

    try:
        with sync_playwright() as playwright:
            launch = {"headless": True, "timeout": capture_timeout_ms}
            if executable_path:
                launch["executable_path"] = executable_path
            if browser_type == "chromium":
                launch["args"] = ["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
            pw_proxy = _playwright_proxy(proxy)
            if pw_proxy:
                launch["proxy"] = pw_proxy
            browser = getattr(playwright, browser_type).launch(**launch)
            context = browser.new_context(user_agent=ua, locale=locale, timezone_id=timezone)
            if init_script:
                context.add_init_script(init_script)
            context.add_cookies([
                {"name": name, "value": value, "url": "https://chatgpt.com/"}
                for name, value in result.cookies.items()
            ])

            def observe_request(request) -> None:
                try:
                    raw_headers = request.all_headers()
                except Exception:
                    raw_headers = request.headers or {}
                headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
                if headers.get("oai-web-deployment-attestation"):
                    result.attestation = headers["oai-web-deployment-attestation"]
                    result.source = "browser"
                if headers.get("oai-client-version"):
                    result.client_version = headers["oai-client-version"]
                if headers.get("oai-client-build-number"):
                    result.client_build = headers["oai-client-build-number"]
                if headers.get("x-oai-is-client-observation"):
                    result.observation = headers["x-oai-is-client-observation"]
                request_url = str(getattr(request, "url", "") or "")
                host = (urlsplit(request_url).hostname or "").lower()
                if host == "stripe.com" or host.endswith(".stripe.com"):
                    encoded_values = [urlsplit(request_url).query]
                    post_data = getattr(request, "post_data", "")
                    if callable(post_data):
                        post_data = post_data()
                    if post_data:
                        encoded_values.append(str(post_data))
                    for encoded in encoded_values:
                        for key, value in parse_qsl(encoded, keep_blank_values=False):
                            if key in {"guid", "muid", "sid", "stripe_js_id"} and value:
                                result.stripe_ids.setdefault(key, value)

            page = context.new_page()
            # BrowserContext also receives service-worker initiated requests.
            context.on("request", observe_request)

            # 只给 ChatGPT 业务接口补 AT；Sentinel 本身按浏览器原始形态请求。
            def route_handler(route) -> None:
                request = route.request
                low = str(request.url or "").lower()
                if "chatgpt.com/backend-api/" in low and "/backend-api/sentinel/" not in low:
                    try:
                        headers = dict(request.all_headers())
                    except Exception:
                        headers = dict(request.headers or {})
                    headers["authorization"] = "Bearer " + str(access_token or "")
                    headers["oai-device-id"] = device_id
                    headers["oai-session-id"] = session_id
                    headers["oai-language"] = str(fp.get("oai_language") or locale)
                    route.continue_(headers=headers)
                else:
                    route.continue_()

            page.route("**/*", route_handler)
            navigation = page.goto(home_url, wait_until="domcontentloaded", timeout=capture_timeout_ms)
            if navigation is not None and not result.client_build:
                try:
                    result.client_build = str(navigation.headers.get("x-build") or "").strip()
                except Exception:
                    pass
            # Opening the shell alone does not always emit a backend request.
            # Trigger one after hydration so the current deployment middleware
            # has a request on which to attach its attestation headers.
            try:
                page.wait_for_timeout(1200)
                page.evaluate(
                    """() => {
                      const controller = new AbortController();
                      const timer = setTimeout(() => controller.abort(), 5000);
                      return fetch('/backend-api/models?history_and_training_disabled=false', {
                        credentials: 'include', headers: {'accept': 'application/json'},
                        signal: controller.signal
                      }).then(r => r.text()).catch(() => '').finally(() => clearTimeout(timer));
                    }"""
                )
            except Exception:
                pass
            # 让当前前端完成 hydration/observation 请求。不要 networkidle：
            # ChatGPT 页面存在长连接，networkidle 可能永远不满足。
            # The public home page no longer emits deployment attestation on
            # its initial requests.  When the caller needs a Sentinel token,
            # do not burn the whole proxy/session lifetime waiting here; mint
            # the token promptly and capture attestation later on Checkout.
            attestation_deadline = deadline
            if sentinel_flow:
                attestation_deadline = min(deadline, time.monotonic() + 3.0)
            while time.monotonic() < attestation_deadline and not result.attestation:
                page.wait_for_timeout(500)

            if not result.attestation:
                # 某些部署把签名放在 hydration 数据中，而不是首批 API 头。
                # 只接受能解码出正式 attestation 字段且尚未过期的二段签名。
                try:
                    serialized = page.evaluate(
                        """() => {
                          const values = [document.documentElement.outerHTML];
                          for (const key of ['__remixContext','__NEXT_DATA__','__reactRouterContext']) {
                            try { if (window[key]) values.push(JSON.stringify(window[key])); } catch (_) {}
                          }
                          return values.join('\\n');
                        }"""
                    )
                    found = find_attestation(str(serialized or ""))
                    if found:
                        result.attestation = found
                        result.source = "browser_hydration"
                except Exception:
                    pass

            # 只有实际需要令牌时才加载 SDK；空 flow 只捕获当前浏览器上下文。
            if sentinel_flow:
                try:
                    wrapper = page.evaluate(
                        """() => fetch('/backend-api/sentinel/sdk.js', {credentials:'include'}).then(r => r.text())"""
                    )
                    result.sentinel_version = discover_sentinel_version(str(wrapper or ""))
                except Exception:
                    pass

            # Generate the requirements token inside the same real browser page
            # that produced the deployment attestation.  Do not substitute the
            # Node/V8 sensor shim here: a structurally valid envelope is not the
            # same thing as a browser-issued proof.
            if sentinel_flow:
                try:
                    sdk_timeout_ms = int(min(max(5.0, timeout_s), 8.0) * 1000)
                    if not page.evaluate("() => typeof globalThis.SentinelSDK === 'object'"):
                        page.add_script_tag(url="https://chatgpt.com/backend-api/sentinel/sdk.js")
                    page.wait_for_function(
                        "() => globalThis.SentinelSDK && typeof globalThis.SentinelSDK.token === 'function'",
                        timeout=sdk_timeout_ms,
                    )
                    token = page.evaluate(
                        """async ({flow, timeoutMs}) => {
                          const sdk = globalThis.SentinelSDK;
                          const value = await Promise.race([
                            (async () => {
                              if (typeof sdk.init === 'function') await sdk.init(flow);
                              return sdk.token(flow);
                            })(),
                            new Promise((_, reject) => setTimeout(
                              () => reject(new Error('Sentinel token timeout')),
                              timeoutMs
                            )),
                          ]);
                          return typeof value === 'string' ? value : JSON.stringify(value);
                        }""",
                        {
                            "flow": sentinel_flow,
                            "timeoutMs": sdk_timeout_ms,
                        },
                    )
                    decoded = json.loads(str(token or ""))
                    if (
                        decoded.get("id") != device_id
                        or decoded.get("flow") != sentinel_flow
                        or not decoded.get("c")
                        or not decoded.get("p")
                    ):
                        raise RuntimeError("浏览器 Sentinel token 与当前 device/flow 不一致")
                    result.sentinel_token = str(token)
                    result.sentinel_flow = sentinel_flow
                    log("sentinel", "ok", f"已在当前 {browser_type} 页面生成 " + sentinel_flow + " Sentinel")
                except Exception as exc:
                    detail = type(exc).__name__ + ": " + str(exc)[:180]
                    result.error = (result.error + "; " if result.error else "") + "browser sentinel: " + detail
                    # The current home deployment may not expose SentinelSDK
                    # before the authenticated checkout UI is mounted.  Use
                    # the bundled current SDK bridge as a dynamic fallback;
                    # it calls /sentinel/req through this same proxy and its
                    # result is validated against the current device + flow.
                    try:
                        token, _so = mint_sentinel(
                            flow=sentinel_flow,
                            device_id=device_id,
                            user_agent=ua,
                            proxy=proxy,
                            page_url=home_url,
                            language=locale,
                            timezone=timezone,
                            cookies=result.cookies,
                            version=result.sentinel_version,
                            timeout_s=min(max(15.0, timeout_s), 60.0),
                        )
                        result.sentinel_token = token
                        result.sentinel_flow = sentinel_flow
                        log("sentinel", "ok", f"{browser_type} SDK 不可用，已由当前 Sentinel SDK bridge 动态生成 " + sentinel_flow)
                    except Exception as bridge_exc:
                        bridge_detail = type(bridge_exc).__name__ + ": " + str(bridge_exc)[:180]
                        result.error += "; sdk bridge: " + bridge_detail

            for cookie in context.cookies("https://chatgpt.com/"):
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                if name and value:
                    result.cookies[name] = value
                    result.browser_cookies[name] = value
            try:
                stripe_cookies = context.cookies("https://stripe.com/", "https://api.stripe.com/")
            except TypeError:
                stripe_cookies = context.cookies("https://stripe.com/")
            for cookie in stripe_cookies:
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                if name == "__stripe_mid" and value:
                    result.stripe_ids.setdefault("muid", value)
                elif name == "__stripe_sid" and value:
                    result.stripe_ids.setdefault("sid", value)
    except Exception as exc:
        result.error = type(exc).__name__ + ": " + str(exc)[:220]
        # Navigation may fail before the in-page SDK block is reached (for
        # example when a sticky proxy tunnel is being rotated). The bridge is
        # independent of page hydration, so still give the same live proxy and
        # device identity one chance to mint the requested flow token.
        if sentinel_flow and not result.sentinel_token:
            try:
                token, _so = mint_sentinel(
                    flow=sentinel_flow,
                    device_id=device_id,
                    user_agent=ua,
                    proxy=proxy,
                    page_url=home_url,
                    language=locale,
                    timezone=timezone,
                    cookies=result.cookies,
                    version=result.sentinel_version,
                    timeout_s=min(max(15.0, timeout_s), 60.0),
                )
                result.sentinel_token = token
                result.sentinel_flow = sentinel_flow
                log("sentinel", "ok", f"{browser_type} 导航失败，已由当前 Sentinel SDK bridge 动态生成 " + sentinel_flow)
            except Exception as bridge_exc:
                bridge_detail = type(bridge_exc).__name__ + ": " + str(bridge_exc)[:180]
                result.error += "; sdk bridge: " + bridge_detail
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    if result.attestation and not result.client_version:
        result.client_version = client_version_from_attestation(result.attestation)
    if result.attestation:
        log("attestation", "ok", "已从当前浏览器部署捕获动态 attestation（不落盘）")
    elif result.error:
        log("attestation", "warn", "浏览器部署证明捕获失败：" + result.error)
    else:
        log("attestation", "warn", "当前页面未发出含 attestation 的请求")
    return result
