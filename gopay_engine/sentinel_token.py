# -*- coding: utf-8 -*-
"""OpenAI Sentinel token mint（Node + 真 sdk.js 桥）—— GCash 建单风控证明。

对齐 engines/freepp-backend/ba_paypal/sentinel_mint.py 的成熟实现：
在 Node/V8 沙箱里加载与后端当前部署一致的 sdk.js（`sentinel_assets/sentinel_sdk.js`），
通过注入的 `SentinelSDK.__proto2(flow)` 钩子完成：

    requirementsToken → POST /backend-api/sentinel/req（同一代理/同一 cookie）
    → 服务器返回 {token(c), proofofwork{seed,difficulty}, turnstile{dx}}
    → 真 SDK VM 算出 turnstile proof (t) → 返回 {t, c, seed, diff, powReq}

Node 侧再用同一指纹配置解 PoW 得到 p（gAAAAAB…~S），拼出完整的
`openai-sentinel-token` 头值 JSON：{"p","t","c","id","flow"}。

失败降级：环境变量 `MIN_GCASH_SENTINEL_TOKEN` 直注入（人工从浏览器 DevTools 复制，
~9 分钟有效）；`MIN_GCASH_SENTINEL=0` 显式关闭并返回空。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_BRIDGE_VERSION = "20260810913b"
_ASSETS_DIR = Path(__file__).with_name("sentinel_assets")
_BRIDGE_JS = _ASSETS_DIR / "sentinel_bridge.js"

# ── 常驻哨兵桥（可选加速） ─────────────────────────────────────────────
# 原来每次 mint 都要冷启动一个 ~62MB 的 Node 进程，并发一高就是「并发数 × 62MB」。
# 打开常驻桥后，进程复用，但每次请求仍会重新 makeGenerator() + vm.createContext()，
# 指纹状态与全新进程逐字段等价（不会产生跨账号相关性）。
#   开关：SENTINEL_DAEMON_DIR（默认 /run/tilian）下的 sentinel-*.sock
#         或 SENTINEL_DAEMON_SOCKET=/a.sock,/b.sock 显式指定
#         设 SENTINEL_DAEMON=0 可完全关闭本加速，回到纯一次性进程。
# 任何异常都会静默回退到一次性进程，不影响原有行为。
_DAEMON_DIR = str(os.environ.get("SENTINEL_DAEMON_DIR") or "/run/tilian").strip()
_DAEMON_COUNTER = [0]

# 单次「调用某个守护进程实例」的超时，必须远小于整个 mint 的预算（timeout_s）。
# 线上实测：守护进程正常时一次签名中位 0.4s、负载高时最慢 1.4s。
# 曾用 timeout_s（90s）直接做 socket 超时，结果只要有一个实例假死
# （例如 systemd 回收时它陷入同步计算、收不到 SIGTERM），每个请求就要白等
# 90 秒才轮到下一个实例 —— 这正是「一半请求 0.4s、一半请求正好 90s」的成因。
# 取 15s 仍有 10 倍余量，并能把最坏情况从 len(paths)*90s 压到 len(paths)*15s。
_DAEMON_CALL_TIMEOUT_S = max(3.0, float(os.environ.get("SENTINEL_DAEMON_TIMEOUT_S") or 15.0))


def _daemon_enabled() -> bool:
    raw = str(os.environ.get("SENTINEL_DAEMON") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _daemon_sockets():
    try:
        import glob as _glob
    except Exception:
        return []
    paths = []
    explicit = str(os.environ.get("SENTINEL_DAEMON_SOCKET") or "").strip()
    if explicit:
        for item in explicit.split(","):
            item = item.strip()
            if item and os.path.exists(item):
                paths.append(item)
    if not paths and _DAEMON_DIR:
        try:
            # 只认哨兵桥的 socket。通配 "sentinel-*.sock" 会连带匹配到
            # sentinel-curl-*.sock（curl 传输助手守护进程）；把 mint 载荷发给它
            # 只会换来一个错误 JSON，而下面「非空即采纳」会把这个错误当成结果
            # 返回，导致 mint 静默降级成「无哨兵」——线上实测约一半请求命中。
            paths = sorted(
                p
                for p in _glob.glob(os.path.join(_DAEMON_DIR, "sentinel-*.sock"))
                if "-curl-" not in os.path.basename(p)
            )
        except Exception:
            paths = []
    return paths


def _mint_via_daemon(payload: bytes, timeout_s: float):
    """走常驻桥取结果；任何失败返回 None，调用方回退到一次性进程。"""
    try:
        import socket as _socket
    except Exception:
        return None
    if not hasattr(_socket, "AF_UNIX"):   # Windows 无 unix socket，直接回退
        return None
    paths = _daemon_sockets()
    if not paths:
        return None
    start = _DAEMON_COUNTER[0] % len(paths)
    _DAEMON_COUNTER[0] = (_DAEMON_COUNTER[0] + 1) % 1000000
    for offset in range(len(paths)):
        path = paths[(start + offset) % len(paths)]
        sock = None
        try:
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(max(3.0, min(float(timeout_s), _DAEMON_CALL_TIMEOUT_S)))
            sock.connect(path)
            sock.sendall(payload)
            try:
                sock.shutdown(_socket.SHUT_WR)   # 与一次性模式的 stdin EOF 语义一致
            except OSError:
                pass
            chunks = []
            while True:
                piece = sock.recv(65536)
                if not piece:
                    break
                chunks.append(piece)
            data = b"".join(chunks)
            # 只有确实带回哨兵结果的响应才算成功，否则换下一个 socket 继续试。
            # 裸判「非空」会把别的守护进程的错误 JSON 当成结果吞下去（见上），
            # 这是纵深防御：即使 socket 列表出错，也不会静默降级。
            if data.strip() and b'"main"' in data:
                return data
        except Exception:
            continue
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
    return None


def _redact(value: str, limit: int = 200) -> str:
    text = str(value or "")
    return text[:limit] + ("…" if len(text) > limit else "")


def mint_sentinel_sync(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str = "",
    cores: int = 16,
    page_url: str = "https://chatgpt.com/",
    language: str = "en-US",
    timezone: str = "Asia/Manila",
    platform: str = "Win32",
    platform_label: str = "Windows",
    screen_w: int = 1920,
    screen_h: int = 1080,
    max_touch_points: int = 0,
    cookie_header: str = "",
    timeout_s: float = 120.0,
    emit=None,
) -> dict:
    """生成 OpenAI Sentinel 头值（main dict）。

    返回 {"p":…, "t":…, "c":…, "id":…, "flow":…}（可直接作为
    `openai-sentinel-token` 头值）或空 dict（失败，调用方按无哨兵降级）。
    emit = callable(stage, status, msg) 与 gcash_service 日志风格一致。
    """
    log = emit or (lambda stage, status, msg: None)

    # 降级开关：显式关闭
    if str(os.environ.get("MIN_GCASH_SENTINEL") or "").strip() in {"0", "false", "no"}:
        log("sentinel", "warn", "MIN_GCASH_SENTINEL=0 已关闭哨兵生成，建单按无风控证明直连")
        return {}

    # 降级注入：人工从浏览器 DevTools 复制的完整 token（<9 分钟有效）
    env_token = str(os.environ.get("MIN_GCASH_SENTINEL_TOKEN") or "").strip()
    if env_token:
        try:
            parsed = json.loads(env_token)
            if isinstance(parsed, dict) and parsed.get("p") and parsed.get("c") and parsed.get("id"):
                log("sentinel", "run", "使用 MIN_GCASH_SENTINEL_TOKEN 注入的哨兵令牌（人工抓取）")
                return parsed
        except Exception:
            pass
        log("sentinel", "warn", "MIN_GCASH_SENTINEL_TOKEN 不是合法哨兵 JSON，忽略")

    if not _BRIDGE_JS.is_file():
        log("sentinel", "warn", "哨兵桥文件缺失（sentinel_assets/sentinel_bridge.js），无哨兵直连")
        return {}
    node = os.environ.get("SENTINEL_NODE") or "node"

    payload = json.dumps({
        "ua": user_agent,
        "cores": cores,
        "deviceId": device_id,
        "flow": flow,
        "proxy": proxy,
        "version": _BRIDGE_VERSION,
        "pageUrl": page_url,
        "language": language,
        "timezone": timezone,
        "platform": platform,
        "platformLabel": platform_label,
        "screenW": screen_w,
        "screenH": screen_h,
        "availH": max(0, screen_h - 48),
        "maxTouchPoints": max_touch_points,
        "cookieHeader": cookie_header,
        "sentinelOrigin": "https://chatgpt.com",
    }, separators=(",", ":")).encode("utf-8")

    log("sentinel", "run", "哨兵令牌生成中（Node 真 sdk.js，flow=" + str(flow) + "）…")
    started = time.monotonic()
    # 先试常驻桥（省掉每次冷启动 ~62MB Node 进程）；不可用/失败则回退一次性进程。
    stdout_bytes = None
    if _daemon_enabled():
        stdout_bytes = _mint_via_daemon(payload, timeout_s)
    if stdout_bytes is None:
        try:
            # Keep the proxy transport helper on the same Python runtime as this
            # engine; Windows PATH may expose no `python.exe` at all.
            bridge_env = os.environ.copy()
            bridge_env.setdefault("PYTHON_BIN", sys.executable)
            process = subprocess.run(
                [node, str(_BRIDGE_JS)],
                input=payload,
                capture_output=True,
                timeout=timeout_s,
                cwd=str(_ASSETS_DIR),
                env=bridge_env,
                check=False,
            )
        except FileNotFoundError:
            log("sentinel", "warn", "哨兵生成需要 Node.js（未找到 node），无哨兵直连")
            return {}
        except subprocess.TimeoutExpired:
            log("sentinel", "warn", "哨兵生成超时（>" + str(int(timeout_s)) + "s），无哨兵直连")
            return {}
        stdout_bytes = process.stdout or b""
    elapsed = round(time.monotonic() - started, 1)

    output = (stdout_bytes or b"").decode("utf-8", "replace").strip()
    if not output:
        err = (process.stderr or b"").decode("utf-8", "replace")[:300]
        log("sentinel", "warn", "哨兵桥无输出：" + _redact(err, 160) + "，无哨兵直连")
        return {}
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        log("sentinel", "warn", "哨兵桥输出非 JSON：" + _redact(output, 160) + "，无哨兵直连")
        return {}
    if result.get("error"):
        log("sentinel", "warn", "哨兵桥失败：" + _redact(str(result["error"]), 160) + "，无哨兵直连")
        return {}

    main = str(result.get("main") or "")
    if not main:
        log("sentinel", "warn", "哨兵桥未返回 main token，无哨兵直连")
        return {}
    try:
        token = json.loads(main)
    except json.JSONDecodeError:
        log("sentinel", "warn", "哨兵桥 main token 不是有效 JSON，无哨兵直连")
        return {}

    has_t = bool(result.get("hasT"))
    so_raw = str(result.get("so") or "")
    so_token = None
    if so_raw:
        try:
            so_token = json.loads(so_raw)
        except json.JSONDecodeError:
            so_token = None
    so_err = str(result.get("soErr") or "")
    log(
        "sentinel",
        "ok",
        "哨兵令牌完成（" + str(elapsed) + "s）t=" + ("有" if has_t else "无")
        + " so=" + ("有" if so_token else "无")
        + (("（" + _redact(so_err, 80) + "）") if so_err else "")
        + " flow=" + str(token.get("flow")) + " id=" + _redact(str(token.get("id")), 12),
    )
    if so_token:
        token["_so"] = so_token
    return token


def sentinel_header_value(token: dict) -> str:
    """Sentinel 头值（JSON 紧凑串），供 `openai-sentinel-token` 使用。"""
    if not isinstance(token, dict) or not token.get("p") or not token.get("c"):
        return ""
    try:
        return json.dumps(token, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return ""



def so_header_value(so_token: dict) -> str:
    """SO 头值（JSON 紧凑串），供 openai-sentinel-so-token 使用。"""
    if not isinstance(so_token, dict) or not so_token.get("so"):
        return ""
    try:
        return json.dumps(so_token, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return ""
