# -*- coding: utf-8 -*-
"""UPI 引擎专用的 OpenAI Sentinel 令牌铸造模块（Node + 真 sdk.js 桥）。

这是 gcash_engine/sentinel_token.py 的 UPI 私有版本，只服务 UPI 提链：

    requirementsToken -> POST /backend-api/sentinel/req（同一代理/同一 cookie）
    -> 服务器返回 {token(c), proofofwork{seed,difficulty}, turnstile{dx}}
    -> 真 SDK VM 算出 turnstile proof (t) -> 返回 {t, c, seed, diff, powReq}
    Node 侧再用同一指纹配置解 PoW 得到 p（gAAAAAB…~S）
    -> 拼出 openai-sentinel-token: {"p","t","c","id","flow"}
       以及 openai-sentinel-so-token: {"so","c","id","flow"}

与共享实现相比，本模块把 2026-09-17 印度 UPI.har 里可观测的指纹位补齐：
  * deployId -> p[6] = "prod-<deployId>"（与 oai-web-deployment-attestation 同源）
  * page_param -> p[15]（页面 URL 首个查询参数名，如 promo_campaign）
  * p[5] = chatgpt.com 承载的 sentinel sdk 地址
  * p[1] 使用页面语言下的时区长名；p[8] 与 language 同值
  * p[14] 由 device_id 派生（同一会话稳定）；p[10..12] 每次随机探针

资产目录：同目录下 upi_sentinel_assets/（bridge/bootstrap/sdk 均为 UPI 私有副本）。
失败降级：MIN_UPI_SENTINEL=0 关闭；MIN_UPI_SENTINEL_TOKEN 直注入（人工抓取，约 9 分钟）；
任何异常返回空 dict，由调用方按"无哨兵直连"继续。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_BRIDGE_VERSION = "20260810913b"
_ASSETS_DIR = Path(__file__).with_name("upi_sentinel_assets")
_BRIDGE_JS = _ASSETS_DIR / "sentinel_bridge.js"


# ── 常驻哨兵桥（对齐 momo 的 gopay 实现）────────────────────────────
# 原来每次 mint 都要冷启动一个 ~62MB 的 Node 进程；常驻桥复用进程，
# 但每次请求仍会重新 makeGenerator() + vm.createContext()，指纹状态与全新进程等价。
#   开关：UPI_SENTINEL_DAEMON_DIR（默认 /run/tilian）下的 upi-sentinel-*.sock
#         或 UPI_SENTINEL_DAEMON_SOCKET=/a.sock,/b.sock 显式指定
#         设 UPI_SENTINEL_DAEMON=0 关闭，回到纯一次性进程。
# 任何异常都会静默回退到一次性进程，不影响原有行为。
_DAEMON_DIR = str(os.environ.get("UPI_SENTINEL_DAEMON_DIR") or "/run/tilian").strip()
_DAEMON_COUNTER = [0]
# 单次调用某个常驻实例的超时，必须远小于整个 mint 预算（timeout_s）。
_DAEMON_CALL_TIMEOUT_S = max(3.0, float(os.environ.get("UPI_SENTINEL_DAEMON_TIMEOUT_S") or 15.0))


def _daemon_enabled() -> bool:
    raw = str(os.environ.get("UPI_SENTINEL_DAEMON") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _daemon_sockets() -> list:
    """常驻桥 socket 列表；只认 upi-sentinel-*.sock（排除 -curl- 传输助手）。"""
    try:
        import glob as _glob
    except Exception:
        return []
    paths: list = []
    explicit = str(os.environ.get("UPI_SENTINEL_DAEMON_SOCKET") or "").strip()
    if explicit:
        for item in explicit.split(","):
            item = item.strip()
            if item and os.path.exists(item):
                paths.append(item)
    if not paths and _DAEMON_DIR:
        try:
            paths = sorted(
                p for p in _glob.glob(os.path.join(_DAEMON_DIR, "upi-sentinel-*.sock"))
                if "-curl-" not in os.path.basename(p)
            )
        except Exception:
            paths = []
    return paths


def _mint_via_daemon(payload: bytes, timeout_s: float):
    """走常驻桥取结果；任何失败返回 None，调用方回退一次性进程。"""
    try:
        import socket as _socket
    except Exception:
        return None
    if not hasattr(_socket, "AF_UNIX"):
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
            # 协议：发一个 JSON 请求体 → 读到完整 JSON 响应即结束。
            # 不用 shutdown(SHUT_WR) 半关闭：Windows 命名管道下服务端会拿到 EPIPE，
            # 收不到响应；AF_UNIX 与管道都能用"读到 JSON 为止"这套。
            chunks = []
            data = b""
            while True:
                try:
                    piece = sock.recv(65536)
                except OSError:
                    break
                if not piece:
                    break
                chunks.append(piece)
                data = b"".join(chunks)
                try:
                    json.loads(data.decode("utf-8", "replace"))
                    break
                except Exception:
                    continue
            # 只有确实带回哨兵结果的响应才算成功，否则换下一个 socket 继续试。
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


def _bridge_payload(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str,
    page_url: str,
    language: str,
    timezone: str,
    platform: str,
    platform_label: str,
    screen_w: int,
    screen_h: int,
    max_touch_points: int,
    cookie_header: str,
    cores: int,
    deploy_id: str,
    page_param: str,
    sentinel_version: str = "",
) -> bytes:
    return json.dumps(
        {
            "ua": user_agent,
            "cores": cores,
            "deviceId": device_id,
            "flow": flow,
            "proxy": proxy,
            # SDK 版本：优先用运行期探测到的线上版本（见 sentinel_runtime.discover_sentinel_version），
            # 否则用本模块钉死的版本。版本会进 SDK URL 与 frame 的 sv= 参数。
            "version": str(sentinel_version or "").strip() or _BRIDGE_VERSION,
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
            # UPI 对齐新增：与部署证明同源的部署 id / 页面查询参数名
            "deployId": deploy_id,
            "pageParam": page_param,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _run_bridge(payload: bytes, timeout_s: float, extra_env: dict | None = None):
    """执行 Node 桥；返回 (stdout_bytes, stderr_text)。FileNotFoundError/Timeout 抛给调用方。"""
    node = os.environ.get("SENTINEL_NODE") or "node"
    env = os.environ.copy()
    env.setdefault("PYTHON_BIN", sys.executable)
    if extra_env:
        env.update(extra_env)
    process = subprocess.run(
        [node, str(_BRIDGE_JS)],
        input=payload,
        capture_output=True,
        timeout=timeout_s,
        cwd=str(_ASSETS_DIR),
        env=env,
        check=False,
    )
    return process.stdout or b"", (process.stderr or b"").decode("utf-8", "replace")[:300]


def selftest_config_array(**kwargs) -> list:
    """离线自检：不联网、不加载 SDK，直接返回桥会写入 p 的 25 元素指纹数组。

    用于把生成结果与 HAR 逐位比对（测试与人工排查都用它）。
    """
    payload = _bridge_payload(
        flow=str(kwargs.get("flow") or "chatgpt_checkout"),
        device_id=str(kwargs.get("device_id") or ""),
        user_agent=str(kwargs.get("user_agent") or ""),
        proxy="",
        page_url=str(kwargs.get("page_url") or "https://chatgpt.com/"),
        language=str(kwargs.get("language") or "en-US"),
        timezone=str(kwargs.get("timezone") or "UTC"),
        platform=str(kwargs.get("platform") or "Win32"),
        platform_label=str(kwargs.get("platform_label") or "Windows"),
        screen_w=int(kwargs.get("screen_w") or 1920),
        screen_h=int(kwargs.get("screen_h") or 1080),
        max_touch_points=int(kwargs.get("max_touch_points") or 0),
        cookie_header=str(kwargs.get("cookie_header") or ""),
        cores=int(kwargs.get("cores") or 16),
        deploy_id=str(kwargs.get("deploy_id") or ""),
        page_param=str(kwargs.get("page_param") or ""),
        sentinel_version=str(kwargs.get("sentinel_version") or ""),
    )
    stdout, stderr = _run_bridge(payload, float(kwargs.get("timeout_s") or 30.0), {"SENTINEL_SELFTEST": "1"})
    text = (stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError("哨兵桥自检无输出：" + _redact(stderr, 160))
    data = json.loads(text)
    return list(data.get("configArray") or [])


def mint_sentinel_sync(
    *,
    flow: str,
    device_id: str,
    user_agent: str,
    proxy: str = "",
    cores: int = 16,
    page_url: str = "https://chatgpt.com/",
    language: str = "en-US",
    timezone: str = "Asia/Kolkata",
    platform: str = "Win32",
    platform_label: str = "Windows",
    screen_w: int = 1920,
    screen_h: int = 1080,
    max_touch_points: int = 0,
    cookie_header: str = "",
    timeout_s: float = 120.0,
    deploy_id: str = "",
    page_param: str = "",
    sentinel_version: str = "",
    emit=None,
) -> dict:
    """生成 OpenAI Sentinel 头值（main dict）。

    返回 {"p":…, "t":…, "c":…, "id":…, "flow":…}（可直接作为
    openai-sentinel-token 头值，另带内部字段 _so 供 SO 头使用）
    或空 dict（失败，调用方按无哨兵降级）。
    """
    log = emit or (lambda stage, status, msg: None)

    disabled = str(
        os.environ.get("MIN_UPI_SENTINEL") or os.environ.get("MIN_GCASH_SENTINEL") or ""
    ).strip()
    if disabled in {"0", "false", "no"}:
        log("sentinel", "warn", "MIN_UPI_SENTINEL=0 已关闭哨兵生成，建单按无风控证明直连")
        return {}

    env_token = str(
        os.environ.get("MIN_UPI_SENTINEL_TOKEN") or os.environ.get("MIN_GCASH_SENTINEL_TOKEN") or ""
    ).strip()
    if env_token:
        try:
            parsed = json.loads(env_token)
            if isinstance(parsed, dict) and parsed.get("p") and parsed.get("c") and parsed.get("id"):
                log("sentinel", "run", "使用注入的哨兵令牌（人工抓取）")
                return parsed
        except Exception:
            pass
        log("sentinel", "warn", "注入的哨兵令牌不是合法 JSON，忽略")

    if not _BRIDGE_JS.is_file():
        log("sentinel", "warn", "UPI 哨兵桥文件缺失（upi_sentinel_assets/sentinel_bridge.js），无哨兵直连")
        return {}

    payload = _bridge_payload(
        flow=flow,
        device_id=device_id,
        user_agent=user_agent,
        proxy=proxy,
        page_url=page_url,
        language=language,
        timezone=timezone,
        platform=platform,
        platform_label=platform_label,
        screen_w=screen_w,
        screen_h=screen_h,
        max_touch_points=max_touch_points,
        cookie_header=cookie_header,
        cores=cores,
        deploy_id=deploy_id,
        page_param=page_param,
        sentinel_version=sentinel_version,
    )

    log(
        "sentinel",
        "run",
        "哨兵令牌生成中（Node 真 sdk.js，flow=" + str(flow)
        + ("，deployId=" + _redact(str(deploy_id), 12) if deploy_id else "，deployId=缺失")
        + "）…",
    )
    started = time.monotonic()
    stderr = ""
    stdout = None
    # 先试常驻桥（省掉每次冷启动 ~62MB Node 进程）；不可用/失败则回退一次性进程。
    if _daemon_enabled():
        stdout = _mint_via_daemon(payload, timeout_s)
    if stdout is None:
        try:
            stdout, stderr = _run_bridge(payload, timeout_s)
        except FileNotFoundError:
            log("sentinel", "warn", "哨兵生成需要 Node.js（未找到 node），无哨兵直连")
            return {}
        except subprocess.TimeoutExpired:
            log("sentinel", "warn", "哨兵生成超时（>" + str(int(timeout_s)) + "s），无哨兵直连")
            return {}
    elapsed = round(time.monotonic() - started, 1)

    output = (stdout or b"").decode("utf-8", "replace").strip()
    if not output:
        log("sentinel", "warn", "哨兵桥无输出：" + _redact(stderr, 160) + "，无哨兵直连")
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
    """Sentinel 头值（JSON 紧凑串），供 openai-sentinel-token 使用。"""
    if not isinstance(token, dict) or not token.get("p") or not token.get("c"):
        return ""
    payload = {key: value for key, value in token.items() if not str(key).startswith("_")}
    try:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
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
