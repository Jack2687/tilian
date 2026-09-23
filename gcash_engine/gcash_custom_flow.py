# -*- coding: utf-8 -*-
"""GCash custom 流程。

建单阶段严格对齐 ``GCash支付链接_HAR接口分析.docx`` 中的浏览器 HAR：
先读取 PH 定价配置，再以 ``checkout_ui_mode=custom`` 创建 Checkout 会话；
GCash 不是请求体中的 ``payment_method_types``，而是建单响应
``custom_payment_methods`` 中返回的自定义方式（``cpmt_...``）。

后续确认/跳转仍必须由上游会话的正常授权状态决定；本模块通过
sentinel_assets 内的真 sdk.js（Node 桥）生成并携带 openai-sentinel-token 与
oai-web-deployment-attestation 风控上下文（对齐 HAR 成功链路），失败时自动降级为
无风控证明直连。
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
import time
from pathlib import Path
import uuid
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from typing import Callable, Optional

# 与检测项目共用 OAICS 递归解析器。
_FREEPP_ROOT = str(Path(__file__).resolve().parent.parent / "engines" / "freepp-backend")
import sys
if _FREEPP_ROOT not in sys.path:
    sys.path.insert(0, _FREEPP_ROOT)
from core import oaics_proto as _oaics  # noqa: E402

from sms_tool.paypal_proxy import normalize_proxy_url  # noqa: E402
from sentinel_token import mint_sentinel_sync, sentinel_header_value  # noqa: E402
from attestation import fetch_attestation  # noqa: E402

_GCASH_CPMT_ID = "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ"


def _force_gcash_custom_method() -> bool:
    """Opt-in test override for accounts whose checkout omits ``cpmt_``.

    The upstream service normally advertises eligibility through
    ``custom_payment_methods``.  Keeping this disabled by default preserves
    that guard; enabling it only tests whether the upstream will accept the
    known GCash method when it was not advertised.
    """
    return os.getenv("GCASH_FORCE_CPMT", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _force_zero_checkout() -> bool:
    """Opt-in GoPay-style continuation when zero price is not confirmed."""
    return os.getenv("GCASH_FORCE_ZERO", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
_ADYEN_REDIRECT_RE = re.compile(r"https?://checkoutshopper-live\.adyen\.com[^\s\"'<>]+")
# GCash 授权页（最终产物）
_GCASH_AUTH_PAGE_RE = re.compile(r"https://m\.gcash\.com/gcashapp/gcash-merchants-auth/index\.html\?[^\s\"'<>]+")
# m.gcash.com 任意 URL（排除静态资源）
_GCASH_ANY_RE = re.compile(r"https://m\.gcash\.com/[^\s\"'<>]+")
_META_REFRESH_RE = re.compile(r"(?i)url\s*=\s*['\"]?([^'\"\s>]+)")
_STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
_PRICING_CONFIG_URL = "https://chatgpt.com/backend-api/checkout_pricing_config/configs"
_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
_CONFIRM_URL = "https://chatgpt.com/backend-api/payments/checkout/confirm"
_START_URL = "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start"
_CONTINUE_URL = "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/continue"
_CPMT_START_URL = "https://chatgpt.com/backend-api/payments/checkout/custom_payment_method/start"
_SUBSCRIPTIONS_URL = "https://chatgpt.com/backend-api/subscriptions"
_ELEMENTS_URL = "https://api.stripe.com/v1/elements/sessions"
_PM_URL = "https://api.stripe.com/v1/payment_methods"
_MAX_QR_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_QR_RESPONSE_BYTES = 4 * 1024 * 1024
_QR_IMAGE_SIZE = 350
_QR_AUTO_REFRESH_SECONDS = 300.0
# 等待首张二维码的超时（秒）：上游授权页二维码可能加载缓慢，
# 独立于单请求 timeout，避免 90s 上限过早回收浏览器会话。
_QR_FIRST_IMAGE_TIMEOUT = 150.0
_QR_REFRESH_RETRY_SECONDS = 25.0
_QR_SESSION_MAX_SECONDS = 20.0 * 60.0
_QR_BROWSER_RESTART_DELAY_SECONDS = 5.0
_BROWSER_LAUNCH_TIMEOUT_MS = 30_000
_BROWSER_NAVIGATION_TIMEOUT_MS = 45_000
_CHATGPT_CLIENT_VERSION = "prod-180ca8b8699a733aef330b7026892aee9bf85fbe"
_CHATGPT_CLIENT_BUILD = "9758774"
# 默认浏览器口径：与 curl_cffi 默认 impersonate=chrome136 的 TLS 指纹完全一致。
# 指纹档案（data/fingerprints.json）提供时一律以档案为准，避免 TLS(impersonate) 与 UA 不一致。
_CHATGPT_SEC_CH_UA = '"Google Chrome";v="136", "Not.A/Brand";v="8", "Chromium";v="136"'
_CHATGPT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"


def _deep_first(value, keys: tuple):
    if isinstance(value, dict):
        for k in keys:
            if k in value:
                return value[k]
        for child in value.values():
            found = _deep_first(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _deep_first(child, keys)
            if found is not None:
                return found
    return None


def _chatgpt_headers(
    access_token: str,
    target_path: str,
    referer: str = "https://chatgpt.com/",
    device_id: str = "",
    session_id: str = "",
    fingerprint: Optional[dict] = None,
    sentinel_token: str = "",
    attestation: str = "",
    client_observation: str = "",
    telemetry: str = "",
    cookie_header: str = "",
    extra: Optional[dict] = None,
):
    """OpenAI 前端请求头（对齐 HAR 浏览器上下文）。

    sentinel_token   = openai-sentinel-token 头值（p/t/c/id/flow JSON 串）
    attestation      = oai-web-deployment-attestation（部署证明，~1h）
    client_observation = x-oai-is-client-observation（客户端观测值）
    telemetry        = oai-telemetry（页面性能数组 JSON）
    cookie_header    = 完整 Cookie 头（含 __cf_bm/_cfuvid 等会话 cookie）
    """
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    ua_value = str(fp.get("ua") or _CHATGPT_UA)
    sec_ch_ua = str(fp.get("sec_ch_ua") or "").strip()
    if not sec_ch_ua:
        # 从 UA 推导同版本 sec-ch-ua 家族（指纹档案未显式提供时保证一致性）
        m = re.search(r"Chrome/(\d+)\.", ua_value)
        if m:
            ver = m.group(1)
            sec_ch_ua = '\"Google Chrome\";v=\"' + ver + '\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"' + ver + '\"'
        else:
            sec_ch_ua = _CHATGPT_SEC_CH_UA
    stable_device_id = device_id or str(uuid.uuid4())
    headers = {
        # UA 与 sec-ch-ua 家族必须同版本（TLS=impersonate 也应一致，见 _new_session）
        "User-Agent": ua_value,
        "Accept": "*/*",
        "Accept-Language": str(fp.get("accept_language") or "en-US,en;q=0.9"),
        "Authorization": "Bearer " + str(access_token or ""),
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "Content-Type": "application/json",
        "oai-device-id": stable_device_id,
        "oai-language": str(fp.get("oai_language") or "en-US"),
        "oai-session-id": session_id or str(uuid.uuid4()),
        "oai-client-version": _CHATGPT_CLIENT_VERSION,
        "oai-client-build-number": _CHATGPT_CLIENT_BUILD,
        # Web checkout sends the device cookie together with the matching
        # header.  Omitting it creates two different identities at the first
        # payment request and is frequently classified as unusual activity.
        "Cookie": cookie_header or ("oai-did=" + stable_device_id),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if sec_ch_ua:
        headers["sec-ch-ua"] = sec_ch_ua
        headers["sec-ch-ua-mobile"] = str(fp.get("sec_ch_ua_mobile") or "?0")
        # 平台与 UA 一致（档案未显式提供时从 platform 推导，避免 Mac UA + Windows hints 分裂）
        _ua_platform = str(fp.get("sec_ch_ua_platform") or "").strip()
        if not _ua_platform:
            _pf = str(fp.get("platform_os") or "").lower()
            if "mac" in _pf or "macintosh" in str(fp.get("platform") or "").lower():
                _ua_platform = '"macOS"'
            elif "win" in _pf:
                _ua_platform = '"Windows"'
            elif "android" in _pf:
                _ua_platform = '"Android"'
            else:
                _ua_platform = '"Windows"'
        headers["sec-ch-ua-platform"] = _ua_platform
    # 风控证明（HAR 成功链路建单请求携带）
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    if attestation:
        headers["oai-web-deployment-attestation"] = attestation
    if client_observation:
        headers["x-oai-is-client-observation"] = client_observation
    if telemetry:
        headers["oai-telemetry"] = telemetry
    account_id = _jwt_account_id(access_token)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    if target_path:
        headers["x-openai-target-path"] = target_path
        headers["x-openai-target-route"] = target_path
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key and value:
                headers[str(key)] = str(value)
    return headers


def _stripe_headers(cs: str, fingerprint: Optional[dict] = None) -> dict:
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    return {
        "Accept": "application/json",
        "Origin": "https://checkout.stripe.com",
        "Referer": "https://checkout.stripe.com/c/pay/" + cs,
        "User-Agent": str(fp.get("ua") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"),
    }


def _new_session(proxy: str, fingerprint: Optional[dict] = None):
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    impersonate = str(fp.get("impersonate") or "chrome136").strip()
    try:
        from curl_cffi.requests import Session as CurlSession
        try:
            session = CurlSession(impersonate=impersonate)
        except Exception:
            session = CurlSession(impersonate="chrome136")
    except Exception:
        import requests
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    else:
        session.proxies = {}
    return session


def _chrome_fallback_fingerprint(fingerprint: Optional[dict] = None) -> dict:
    """TLS 回退时同步切换 UA/Client Hints，避免 Chrome TLS 搭配 Firefox/Safari 请求头。"""
    fp = dict(fingerprint) if isinstance(fingerprint, dict) else {}
    fp.update({
        "impersonate": "chrome136",
        "kernel": "chrome",
        "kernel_label": "Chrome",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    })
    return fp


def _is_unusual_activity(detail: str) -> bool:
    low = str(detail or "").lower()
    return any(marker in low for marker in (
        "unusual activity", "suspicious activity", "abnormal activity",
        "异常活动", "可疑活动",
    ))


def _response_detail(response, limit: int = 300) -> str:
    """Return a concise upstream error without assuming a JSON shape."""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            value = payload.get("detail") or payload.get("error") or payload
        else:
            value = payload
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(getattr(response, "text", "") or "")
    return text[:limit]


def _gcash_custom_payment_method_id(custom_payment_methods) -> str:
    """Return only the HAR-observed GCash CPMT; never substitute another method."""
    if not isinstance(custom_payment_methods, list):
        return ""
    for item in custom_payment_methods:
        candidate = str((item.get("id") or item.get("type") or "") if isinstance(item, dict) else item)
        if candidate.lower() == _GCASH_CPMT_ID.lower():
            return _GCASH_CPMT_ID
    return ""


def _checkout_has_zero_discount(data: dict) -> bool:
    """Detect a 100% trial from the nested custom-checkout state."""
    state = data.get("checkout_state") if isinstance(data, dict) else None
    if not isinstance(state, dict):
        return False
    discounts = state.get("discountAmounts") or []
    has_discount = any(
        isinstance(item, dict)
        and (float(item.get("percentOff") or 0) >= 100
             or int(item.get("minorUnitsAmount") or 0) > 0)
        for item in discounts
    )
    if not has_discount:
        return False
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    final = total.get("total") if isinstance(total.get("total"), dict) else {}
    amount = final.get("minorUnitsAmount")
    return amount is None or int(amount or 0) == 0


def _jwt_account_id(access_token: str) -> str:
    """只在本地解码 JWT 载荷，提取订阅查询所需的账号 ID。"""
    parts = str(access_token or "").strip().split(".")
    if len(parts) != 3:
        return ""
    try:
        encoded = parts[1].replace("-", "+").replace("_", "/")
        encoded += "=" * (-len(encoded) % 4)
        claims = json.loads(base64.b64decode(encoded).decode("utf-8"))
        auth = claims.get("https://api.openai.com/auth") or {}
        return str(auth.get("chatgpt_account_id") or "").strip()
    except Exception:
        return ""


def _checkout_verify_return_url(source_url: str, location: str) -> str:
    """从任意 3xx Location 解析完整 checkout/verify 返回地址。"""
    target = urljoin(str(source_url or ""), str(location or "").strip())
    if not target:
        return ""
    parsed = urlsplit(target)
    if parsed.netloc.lower() != "chatgpt.com" or parsed.path != "/checkout/verify":
        return ""
    return target if parse_qs(parsed.query, keep_blank_values=True).get("redirectResult", [""])[0] else ""


def _action_result_from_verify_url(value: str) -> str:
    """提取 Adyen 的不透明 redirectResult；只传递，不尝试解码其内部内容。"""
    try:
        return str(parse_qs(urlsplit(str(value or "")).query, keep_blank_values=True).get("redirectResult", [""])[0])
    except Exception:
        return ""


def _has_active_plus(value) -> bool:
    """递归检查 subscriptions 响应中是否已有有效 Plus 记录。"""
    if isinstance(value, dict):
        plan = str(value.get("plan_type") or "").strip().lower()
        if plan == "plus" and value.get("is_delinquent") is not True:
            try:
                return int(value.get("seats_entitled", 1) or 0) >= 1
            except (TypeError, ValueError):
                return True
        return any(_has_active_plus(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_active_plus(item) for item in value)
    return False


def _verify_plus_subscription(
    session,
    access_token: str,
    device_id: str,
    session_id: str,
    fingerprint: Optional[dict],
    timeout: float,
    emit: Callable[[str, str, str], None],
) -> bool:
    account_id = _jwt_account_id(access_token)
    if not account_id:
        emit("subscriptions", "warn", "AT 中未找到 chatgpt_account_id，无法主动核验订阅")
        return False
    verify_headers = _chatgpt_headers(
        access_token,
        "/backend-api/subscriptions",
        device_id=device_id,
        session_id=session_id,
        fingerprint=fingerprint,
    )
    verify_headers["Chatgpt-Account-Id"] = account_id
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        try:
            check = session.get(
                _SUBSCRIPTIONS_URL,
                params={"account_id": account_id},
                headers=verify_headers,
                timeout=timeout,
            )
            if check.status_code < 400 and _has_active_plus(check.json()):
                emit("subscriptions", "ok", "Plus 订阅已生效（plan_type=plus）")
                return True
        except Exception:
            pass
        time.sleep(3.0)
    emit("subscriptions", "warn", "90 秒内尚未读到 Plus 订阅")
    return False


def _server_finish_and_verify(
    session,
    access_token: str,
    checkout_session_id: str,
    action_result: str,
    device_id: str,
    session_id: str,
    fingerprint: Optional[dict],
    timeout: float,
    emit: Callable[[str, str, str], None],
) -> bool:
    """前端未自动继续时，按官方前端字段完成 return，并以订阅状态验收。"""
    if not action_result:
        return False
    headers = _chatgpt_headers(
        access_token,
        "/backend-api/payments/checkout/custom_payment_method/continue",
        referer="https://chatgpt.com/checkout/verify",
        device_id=device_id,
        session_id=session_id,
        fingerprint=fingerprint,
    )
    account_id = _jwt_account_id(access_token)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    try:
        response = session.post(
            _CONTINUE_URL,
            json={
                "checkout_session_id": checkout_session_id,
                "action_result": {"redirectResult": action_result},
            },
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:
        emit("continue", "warn", "付款返回继续确认网络失败：" + type(exc).__name__ + "：" + str(exc)[:100])
        return False
    if response.status_code >= 400:
        emit("continue", "warn", "付款返回继续确认 HTTP " + str(response.status_code) + "：" + str(response.text or "")[:700])
        return False
    emit("continue", "ok", "付款返回已由 OpenAI continue 接口接收，开始核验 Plus 订阅")

    return _verify_plus_subscription(
        session,
        access_token,
        device_id,
        session_id,
        fingerprint,
        timeout,
        emit,
    )



def _png_dimensions(content: bytes) -> tuple[int, int] | None:
    """只读取 PNG 文件头尺寸，不读取二维码内容。"""
    raw = bytes(content or b"")
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        return None
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def _decode_data_image(data_url: str) -> tuple[str, bytes] | None:
    """解包 data:image Base64，仅用于读取图片格式头和尺寸。"""
    match = re.match(
        r"data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=_-]+)$",
        str(data_url or ""),
        re.I,
    )
    if not match:
        return None
    encoded = match.group(2).replace("-", "+").replace("_", "/")
    encoded += "=" * (-len(encoded) % 4)
    try:
        return "image/" + match.group(1).lower(), base64.b64decode(encoded, validate=False)
    except (TypeError, ValueError):
        return None


def _as_data_image(content: bytes, content_type: str = "image/png", require_qr_size: bool = False) -> str:
    """把图片原样包装为 data URL；二维码路径可严格要求 350x350。"""
    if not content or len(content) > _MAX_QR_IMAGE_BYTES:
        return ""
    mime = str(content_type or "image/png").split(";", 1)[0].strip().lower()
    if mime not in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"):
        mime = "image/png"
    if require_qr_size and (mime != "image/png" or _png_dimensions(content) != (_QR_IMAGE_SIZE, _QR_IMAGE_SIZE)):
        return ""
    return "data:" + mime + ";base64," + base64.b64encode(content).decode("ascii")


def _normalize_data_image(value: str) -> str:
    """规范化 data:image 图片字符串；不解码二维码内容。"""
    text = html.unescape(str(value or "")).replace("\\/", "/")
    match = re.search(
        r"data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=_-]{100,})",
        text,
        re.I,
    )
    if not match:
        return ""
    encoded = re.sub(r"\s+", "", match.group(2))
    if len(encoded) > _MAX_QR_IMAGE_BYTES * 2:
        return ""
    mime = "image/" + match.group(1).lower()
    return "data:" + mime + ";base64," + encoded


def _looks_like_qr_image(data_url: str) -> bool:
    """只接受 PNG 文件头尺寸严格为 350x350 的二维码图片（与原扫描策略一致）。"""
    decoded = _decode_data_image(data_url)
    return bool(decoded and decoded[0] == "image/png" and _png_dimensions(decoded[1]) == (_QR_IMAGE_SIZE, _QR_IMAGE_SIZE))


def _canvasify_via_browser(frame, data_url: str) -> str:
    """把任意 data:image 格式（webp/jpeg/gif）交给页面 canvas 按自然像素尺寸重采样为 PNG。

    只做格式转换，不做尺寸缩放：natural 350x350 的二维码转出后仍是 350x350 PNG，
    之后仍必须通过 350x350 严格校验（尺寸门槛不放宽）。
    用途：Chromium 默认 Accept 头会让部分 CDN 节点对不同的账号指纹/代理出口
    返回 WebP/JPEG 二维码，单纯按 data:image/png 校验会漏掉这些同样有效的图。
    """
    if not data_url or not data_url.startswith("data:image/"):
        return ""
    try:
        png = frame.evaluate(
            """async (src) => {
              const img = new Image();
              const loaded = await new Promise((resolve) => {
                img.onload = () => resolve(true);
                img.onerror = () => resolve(false);
                img.src = src;
              });
              if (!loaded) return '';
              try {
                const c = document.createElement('canvas');
                c.width = img.naturalWidth || img.width;
                c.height = img.naturalHeight || img.height;
                c.getContext('2d').drawImage(img, 0, 0);
                return c.toDataURL('image/png');
              } catch (e) { return ''; }
            }""",
            data_url,
        )
    except Exception:
        return ""
    return str(png or "")


def _image_candidate_score(text: str, start: int, end: int, source_hint: str = "") -> int:
    """按响应字段/URL上下文给图片候选排序，避免误选说明图片。"""
    context = text[max(0, start - 700):min(len(text), end + 700)]
    score = 0
    if re.search(r"qr|qr.?code|二维码", context, re.I):
        score += 1000
    if re.search(r"(?:image|base64|data|content|payload|value)", context, re.I):
        score += 100
    if re.search(r"qr|qr.?code|二维码", str(source_hint or ""), re.I):
        score += 500
    return score


def _extract_qr_data_image(body_text: str, source_hint: str = "", allow_unmarked: bool = False, _normalized: bool = False) -> str:
    """从 HTML/JSON/文本响应中提取二维码图片 Base64，不读取二维码内容。"""
    text = str(body_text or "")
    if not text:
        return ""

    # 优先选择响应中已经带 data:image 前缀的图片。
    data_pattern = re.compile(
        r"data:image/(?:png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=_-]{100,}",
        re.I,
    )
    data_candidates = list(data_pattern.finditer(text))
    if data_candidates:
        ranked = sorted(
            data_candidates,
            key=lambda match: _image_candidate_score(text, match.start(), match.end(), source_hint),
            reverse=True,
        )
        for best in ranked:
            best_score = _image_candidate_score(text, best.start(), best.end(), source_hint)
            normalized = _normalize_data_image(best.group(0))
            if _looks_like_qr_image(normalized) and (best_score >= 1000 or allow_unmarked):
                return normalized

    # GCash 的接口也可能只返回裸 Base64，例如 {"qrCode":"iVBORw0KGgo..."}。
    # 用图片文件头识别格式，避免把普通 token/signature Base64 当作二维码。
    raw_patterns = (
        ("image/png", re.compile(r"(?<![A-Za-z0-9+/=_-])iVBORw0KGgo[A-Za-z0-9+/=_-]{100,}(?![A-Za-z0-9+/=_-])")),
        ("image/jpeg", re.compile(r"(?<![A-Za-z0-9+/=_-])/9j/[A-Za-z0-9+/=_-]{100,}(?![A-Za-z0-9+/=_-])")),
        ("image/gif", re.compile(r"(?<![A-Za-z0-9+/=_-])R0lGOD[A-Za-z0-9+/=_-]{100,}(?![A-Za-z0-9+/=_-])")),
        ("image/webp", re.compile(r"(?<![A-Za-z0-9+/=_-])UklGR[A-Za-z0-9+/=_-]{100,}(?![A-Za-z0-9+/=_-])")),
    )
    raw_candidates = []
    for mime, pattern in raw_patterns:
        raw_candidates.extend((mime, match) for match in pattern.finditer(text))
    if raw_candidates:
        ranked = sorted(
            raw_candidates,
            key=lambda item: _image_candidate_score(text, item[1].start(), item[1].end(), source_hint),
            reverse=True,
        )
        for mime, best in ranked:
            best_score = _image_candidate_score(text, best.start(), best.end(), source_hint)
            encoded = re.sub(r"\s+", "", best.group(0)).replace("-", "+").replace("_", "/")
            if len(encoded) > _MAX_QR_IMAGE_BYTES * 2:
                continue
            candidate = "data:" + mime + ";base64," + encoded
            if _looks_like_qr_image(candidate) and (best_score >= 1000 or allow_unmarked):
                return candidate

    # JSON 中可能通过 \\/ 或 \\u 转义图片字符串；展开一次后再扫描。
    if not _normalized:
        try:
            parsed = json.loads(text)
            normalized = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError, json.JSONDecodeError):
            normalized = ""
        if normalized and normalized != text:
            return _extract_qr_data_image(normalized, source_hint, allow_unmarked, _normalized=True)
    return ""


def _extract_qr_from_browser_response(response) -> tuple[str, str]:
    """从同一浏览器会话的响应中提取严格 350x350 PNG。"""
    try:
        response_url = str(response.url or "")
        if int(response.status or 0) >= 400:
            return "", ""
        headers = response.headers or {}
        content_type = str(headers.get("content-type") or "").lower()
        request = response.request
        resource_type = str(request.resource_type or "").lower()
        if content_type.startswith("image/"):
            # 不依赖 URL 是否包含 qr：页面可能从跨域接口或普通图片路径返回二维码。
            # PNG 350x350 直接准入；WebP/JPEG 等其它格式保留原始 data URL，
            # 由调用方在浏览器 canvas 内按自然尺寸重采样成 PNG 后仍走 350x350 校验
            # （尺寸门槛不放宽，只兼容 CDN 按 UA/代理协商返回的格式差异）。
            body = response.body()
            direct = _as_data_image(body, content_type, require_qr_size=True)
            if direct:
                return direct, response_url
            if body and len(body) <= _MAX_QR_IMAGE_BYTES:
                mime = str(content_type).split(";", 1)[0].strip().lower()
                if mime in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"):
                    return "data:" + mime + ";base64," + base64.b64encode(body).decode("ascii"), response_url
            return "", ""
        if resource_type not in ("xhr", "fetch", "document") and not re.search(
            r"json|text|javascript", content_type, re.I
        ):
            return "", ""
        body = response.body()
        if not body or len(body) > _MAX_QR_RESPONSE_BYTES:
            return "", ""
        body_text = body.decode("utf-8", errors="replace")
        image = _extract_qr_data_image(body_text, source_hint=response_url, allow_unmarked=True)
        return image, response_url
    except Exception:
        # 某些跨域/已取消响应无法读取 body；不影响后续继续轮询其它响应。
        return "", ""


def _extract_qr_from_page_state(frame) -> list[str]:
    """扫描当前页面已暴露的图片数据，不读取二维码内容。"""
    try:
        values = frame.evaluate(
            """() => {
              const found = [];
              const push = value => {
                if (typeof value === 'string' && value.length >= 100) found.push(value);
              };
              const scanText = value => {
                if (typeof value !== 'string' || !value) return;
                const dataPattern = /data:image\\/(?:png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=_-]{100,}/gi;
                const rawPattern = /iVBORw0KGgo[A-Za-z0-9+/=_-]{100,}/g;
                for (const match of value.match(dataPattern) || []) push(match);
                for (const match of value.match(rawPattern) || []) push(match);
              };
              const visit = root => {
                const elements = root && root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of elements) {
                  for (const name of ['src', 'href', 'xlink:href', 'data-src', 'data-image', 'data-qrcode', 'value', 'content']) {
                    const value = el.getAttribute && el.getAttribute(name);
                    if (value) scanText(value);
                  }
                  if (el.tagName && el.tagName.toLowerCase() === 'img') {
                    scanText(el.currentSrc || el.src || '');
                  }
                  if (el.tagName && el.tagName.toLowerCase() === 'canvas') {
                    try {
                      // 画布可能通过 CSS 缩小显示；读取其内部像素尺寸后再由
                      // Python 严格校验 350x350，避免截图尺寸被 CSS 影响。
                      if (el.width > 0 && el.height > 0 && el.width <= 2048 && el.height <= 2048) {
                        push(el.toDataURL('image/png'));
                      }
                    } catch (_) {}
                  }
                  if (el.style) scanText(el.style.backgroundImage || '');
                  if (el.shadowRoot) visit(el.shadowRoot);
                }
              };
              visit(document);
              for (const script of document.scripts || []) scanText(script.textContent || '');
              return found.slice(0, 160);
            }"""
        )
        return values if isinstance(values, list) else []
    except Exception:
        return []


def _extract_qr_src_image(body_text: str, current_url: str, session, timeout: float) -> str:
    """获取页面标记为二维码的 img/source 资源，仍然复用原 requests 会话。"""
    text = str(body_text or "")
    for tag in re.findall(r"<(?:img|source)\b[^>]*>", text, re.I):
        if not re.search(r"qr|qr.?code|二维码", tag, re.I):
            continue
        src_match = re.search(r"\bsrc\s*=\s*['\"]([^'\"]+)['\"]", tag, re.I)
        if not src_match:
            continue
        src = html.unescape(src_match.group(1).strip())
        if src.lower().startswith("data:image/"):
            return src if _looks_like_qr_image(src) else ""
        try:
            response = session.get(urljoin(current_url, src), timeout=timeout)
            if response.status_code < 400:
                data_image = _as_data_image(
                    response.content,
                    response.headers.get("Content-Type", "image/png"),
                    require_qr_size=True,
                )
                if data_image:
                    return data_image
        except Exception:
            continue
    return ""


class _LocalSocks5Bridge:
    """本地 SOCKS5 认证桥：Chromium 不支持 socks5 认证代理，这里起一个
    127.0.0.1 上的 HTTP CONNECT 直通代理，把请求转发到带认证的 socks5 出口，
    浏览器出口 IP 与协议引擎完全一致（全程统一代理/会话）。
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
        self._thread = _t.Thread(target=self._accept_loop, name="gcash-socks5-bridge", daemon=True)
        self._thread.start()

    def _accept_loop(self):
        import socket as _s
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                _t = __import__("threading")
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
            # 1) 读 HTTP 请求头（只取第一行做路由，其余原样转发）
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
                # 浏览器对 HTTPS 走 CONNECT：目标 host:port
                target = parts[1] if len(parts) > 1 else ""
                if ":" not in target:
                    return
                host, port_s = target.rsplit(":", 1)
                port = int(port_s)
            else:
                # 兼容绝对 URI（GET http://host:port/path）：从 URI 提取目标
                target = parts[1] if len(parts) > 1 else ""
                parsed_uri = urlsplit(target if "://" in target else "http://" + target)
                if not parsed_uri.hostname:
                    return
                host = parsed_uri.hostname
                port = parsed_uri.port or 80
            # 2) 连接上游 socks5 + 认证（RFC1929）
            up = _s.create_connection(self._upstream, timeout=20)
            self._socks5_handshake(up, host, port)
            if method == "CONNECT":
                # 3) 回 200 后纯隧道（后续 TLS 字节/响应原样透传）
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._pump(conn, up)
            else:
                # 绝对 URI 模式：把请求行改写为 origin-form（host 头保留），其余头原样发
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
        # greeting: version 5, nmethods 2, no-auth + user/pass
        up.sendall(bytes([0x05, 0x02, 0x00, 0x02]))
        resp = self._recv_exact(up, 2)
        if resp[1] != 0x02:
            raise RuntimeError("socks5 auth method rejected")
        # auth: version 1, ulen, user, plen, pass
        ub = user.encode("utf-8")
        pb = pwd.encode("utf-8")
        up.sendall(bytes([0x01, len(ub)]) + ub + bytes([len(pb)]) + pb)
        resp = self._recv_exact(up, 2)
        if resp[1] != 0x00:
            raise RuntimeError("socks5 auth failed")
        # connect: v5, cmd connect, rsv, atyp domain
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
        import socket as _s
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


def _proxy_for_playwright(proxy: str, bridge_holder: Optional[list] = None) -> Optional[dict]:
    """返回 Playwright 可用代理配置；socks5 带认证时起本地桥（Chromium 不支持 socks5 认证）。

    bridge_holder 传入 list 时，创建的桥实例会存入 [0]，调用方负责 close()。
    """
    parsed = urlsplit(str(proxy or ""))
    if not parsed.hostname:
        return None
    scheme = parsed.scheme or "http"
    if scheme == "socks5h":
        scheme = "socks5"
    # Chromium 不支持带认证的 socks5 → 本地 SOCKS5 认证桥（出口 IP 不变）
    if scheme == "socks5" and (parsed.username or parsed.password):
        try:
            bridge = _LocalSocks5Bridge(str(proxy or ""))
            if bridge_holder is not None and isinstance(bridge_holder, list):
                bridge_holder[:] = [bridge]
            return {"server": "http://127.0.0.1:" + str(bridge.port)}
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


def _chromium_executable() -> str:
    """选择服务用户可直接启动的 Chromium；支持用环境变量覆盖。"""
    candidates = []
    configured = str(os.environ.get("GCASH_CHROMIUM_EXECUTABLE", "") or "").strip()
    if configured:
        candidates.append(configured)
    if os.name == "nt":
        local_app_data = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
        candidates.extend((
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ))
        if local_app_data:
            candidates.append(str(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    # Ubuntu 24.04 的 chromium-browser 包由 Snap 提供；Playwright 需要
    # 调用 Snap 内部二进制，不能直接把 /snap/bin/chromium 当 executable_path。
    candidates.extend((
        "/snap/chromium/current/usr/lib/chromium-browser/chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return ""


def _prepare_chromium_runtime(executable: str) -> None:
    """为 Snap 内部 Chromium 补充它在非 Snap 进程中需要的动态库路径。"""
    if not executable.startswith("/snap/chromium/"):
        return
    snap_root = Path(executable).parents[3]
    library_dirs = [
        snap_root / "usr" / "lib",
        snap_root / "usr" / "lib" / "x86_64-linux-gnu",
        Path("/snap/gnome-46-2404/current/usr/lib/x86_64-linux-gnu"),
        Path("/snap/gnome-46-2404/current/lib/x86_64-linux-gnu"),
        Path("/snap/mesa-2404/current/usr/lib/x86_64-linux-gnu"),
        Path("/snap/mesa-2404/current/lib/x86_64-linux-gnu"),
    ]
    existing = str(os.environ.get("LD_LIBRARY_PATH", "") or "")
    paths = [str(path) for path in library_dirs if path.is_dir()]
    if existing:
        paths.append(existing)
    if paths:
        os.environ["LD_LIBRARY_PATH"] = ":".join(paths)


def _safe_page_label(url: str) -> str:
    """日志只保留页面 origin/path，避免把授权 query 写进任务日志。"""
    parsed = urlsplit(str(url or ""))
    if not parsed.netloc:
        return str(url or "")[:120]
    return (parsed.scheme + "://" + parsed.netloc + parsed.path)[:180]


def _fingerprint_init_script(fp: dict) -> str:
    """完整反检测注入脚本（按账号指纹档案生成，页面脚本执行前注入）：

    - 隐藏 navigator.webdriver
    - 伪造 hardwareConcurrency / deviceMemory / platform / languages / maxTouchPoints
    - 伪造 screen 尺寸与色深
    - canvas 2D getImageData 确定性加噪（seed = 指纹 canvas_seed）
    - WebGL getParameter 伪造 vendor / renderer（含 UNMASKED 系列）
    - document.fonts.check 对指纹字体列表返回已安装
    """
    canvas_seed = str(fp.get("canvas_seed") or "deadbeef")
    hw = int(fp.get("hardware_concurrency") or 8)
    mem = int(fp.get("device_memory") or 8)
    platform = str(fp.get("platform") or "Win32")
    locale = str(fp.get("locale") or "en-US")
    screen = str(fp.get("screen") or "1920x1080")
    try:
        sw, sh = [int(x) for x in screen.lower().split("x")[:2]]
    except Exception:
        sw, sh = 1920, 1080
    webgl_vendor = str(fp.get("webgl_vendor") or "Google Inc. (Intel)")
    gpu_renderer = str(fp.get("gpu_renderer") or "ANGLE (Intel, Intel(R) UHD Graphics 620, OpenGL 4.1)")
    fonts = [str(f) for f in (fp.get("fonts") or []) if f]
    fonts_json = "[" + ",".join(json.dumps(f) for f in fonts) + "]"
    lang_short = locale.split("-", 1)[0]
    js = f"""
(() => {{
  'use strict';
  const seed = {json.dumps(canvas_seed)};
  let _s = 0;
  for (const ch of seed) _s = (_s * 31 + ch.charCodeAt(0)) >>> 0;
  const _rand = () => {{ _s = (_s * 1664525 + 1013904223) >>> 0; return _s / 4294967296; }};
  const _define = (obj, key, val) => {{
    try {{ Object.defineProperty(obj, key, {{ get: () => val, configurable: true }}); }} catch (e) {{}}
  }};
  _define(navigator, 'webdriver', undefined);
  _define(navigator, 'hardwareConcurrency', {hw});
  _define(navigator, 'deviceMemory', {mem});
  _define(navigator, 'platform', {json.dumps(platform)});
  _define(navigator, 'maxTouchPoints', 0);
  Object.defineProperty(navigator, 'languages', {{ get: () => [{json.dumps(locale)}, {json.dumps(lang_short)}], configurable: true }});
  _define(screen, 'width', {sw});
  _define(screen, 'height', {sh});
  _define(screen, 'availWidth', {sw});
  _define(screen, 'availHeight', {sh});
  _define(screen, 'colorDepth', 24);
  _define(screen, 'pixelDepth', 24);
  try {{
    const _gid = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function (x, y, w, h) {{
      const img = _gid.call(this, x, y, w, h);
      const d = img.data;
      for (let i = 0; i < d.length; i += 64) {{
        if (_rand() > 0.5) d[i] = (d[i] + 1) & 255;
        else d[i] = (d[i] - 1) & 255;
      }}
      return img;
    }};
  }} catch (e) {{}}
  const _webgl_fix = (proto) => {{
    try {{
      const _gp = proto.getParameter;
      proto.getParameter = function (p) {{
        if (p === 0x9245) return {json.dumps(webgl_vendor)};
        if (p === 0x9246) return {json.dumps(gpu_renderer)};
        if (p === 0x1f00) return {json.dumps(webgl_vendor)};
        if (p === 0x1f01) return {json.dumps(gpu_renderer)};
        return _gp.call(this, p);
      }};
    }} catch (e) {{}}
  }};
  _webgl_fix(WebGLRenderingContext.prototype);
  _webgl_fix(WebGL2RenderingContext.prototype);
  try {{
    const _fonts = {fonts_json};
    const _check = document.fonts.check.bind(document.fonts);
    document.fonts.check = function (font) {{
      if (_fonts.some((f) => String(font).toLowerCase().includes(f.toLowerCase()))) return true;
      return _check(font);
    }};
  }} catch (e) {{}}
}})();
"""
    return js


def _route_with_login(route, fp: dict, access_token: str) -> None:
    """请求拦截：对 ChatGPT/OpenAI 域请求附加 AT 登录态 + 指纹设备头。

    让浏览器内的授权流程（m.gcash.com 授权页回调 chatgpt.com 的
    confirm/approve/poll 等接口）表现为"已登录的真实用户"。
    m.gcash.com / adyen / stripe 等第三方请求不附加 AT，原样放行。
    """
    url = str(getattr(route.request, "url", "") or "")
    low = url.lower()
    if ("chatgpt.com" in low or "openai.com" in low) and url.startswith("http"):
        headers = dict(getattr(route.request, "headers", {}) or {})
        headers["Authorization"] = "Bearer " + str(access_token or "")
        device_id = str(fp.get("device_id") or "")
        if device_id:
            headers["oai-device-id"] = device_id
            headers["oai-session-id"] = str(fp.get("oai_session_id") or "") or device_id
        headers["oai-language"] = str(fp.get("oai_language") or "en-US")
        try:
            route.continue_(headers=headers)
            return
        except Exception:
            pass
    try:
        route.continue_()
    except Exception:
        pass


def _capture_qr_with_browser(
    url: str,
    session,
    proxy: str,
    timeout: float,
    referer: str = "",
    fingerprint: Optional[dict] = None,
    emit: Optional[Callable[[str, str, str], None]] = None,
    keep_alive: bool = False,
    on_image: Optional[Callable[[str], None]] = None,
    stop_event: Optional[threading.Event] = None,
    refresh_event: Optional[threading.Event] = None,
    access_token: str = "",
    on_paid: Optional[Callable[[str], None]] = None,
    checkout_session_id: str = "",
    device_id: str = "",
    session_id: str = "",
    session_deadline: float = 0.0,
) -> str:
    """在同一代理/会话身份下进入最终 GCash 页面并截图二维码元素。"""
    log = emit or (lambda stage, status, msg: None)
    phase = "准备浏览器"
    try:
        # 优先使用项目内浏览器目录；兼容服务器旧布局（/opt/tilian/
        # playwright-browsers）。目录不存在时不覆盖 Playwright 默认缓存目录。
        project_root = Path(__file__).resolve().parents[1]
        legacy_root = Path(__file__).resolve().parents[2]
        configured_root = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
        if not configured_root:
            for candidate in (project_root / "playwright-browsers", legacy_root / "playwright-browsers"):
                if candidate.is_dir():
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
                    break
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log("resolve", "warn", "二维码浏览器模块加载失败：" + type(exc).__name__ + "：" + str(exc)[:120])
        return ""
    try:
        with sync_playwright() as playwright:
            phase = "启动浏览器"
            # 有头窗口开关：GCASH_BROWSER_HEADED=1 时窗口可见（本地扫码支付同窗口完成回调）；
            # 无显示器环境（服务器 DISPLAY 缺失）自动回退 headless。
            headless = not (str(os.environ.get("GCASH_BROWSER_HEADED") or "").strip() in {"1", "true", "yes"})
            launch_kwargs = {"headless": headless}
            chromium_executable = _chromium_executable()
            if chromium_executable:
                _prepare_chromium_runtime(chromium_executable)
                launch_kwargs["executable_path"] = chromium_executable
                launch_kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
            _socks_bridge_holder = []
            browser_proxy = _proxy_for_playwright(proxy, _socks_bridge_holder)
            if browser_proxy:
                launch_kwargs["proxy"] = browser_proxy
            launch_kwargs["timeout"] = _BROWSER_LAUNCH_TIMEOUT_MS
            browser = playwright.chromium.launch(**launch_kwargs)
            try:
                fp = fingerprint if isinstance(fingerprint, dict) else {}
                context_kwargs = {
                    "user_agent": str(fp.get("ua") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"),
                    "locale": str(fp.get("locale") or "en-PH"),
                    "viewport": {"width": 1280, "height": 900},
                }
                timezone = str(fp.get("timezone") or "").strip()
                if timezone:
                    context_kwargs["timezone_id"] = timezone
                context = browser.new_context(**context_kwargs)
                # ── 完整反检测注入（canvas/WebGL/navigator/字体/硬件，按指纹档案）──
                try:
                    context.add_init_script(_fingerprint_init_script(fp))
                except Exception as exc:
                    log("resolve", "warn", "反检测注入失败：" + type(exc).__name__ + "：" + str(exc)[:100])
                # ── AT 登录态注入：拦截 ChatGPT/OpenAI 域请求附加 Bearer + 指纹设备头 ──
                if access_token:
                    try:
                        context.route("**/*", lambda route: _route_with_login(route, fp, access_token))
                        log("resolve", "run", "已注入 AT 登录态（浏览器内 ChatGPT 请求自动附加 Bearer + 指纹设备头）")
                    except Exception as exc:
                        log("resolve", "warn", "AT 登录态注入失败：" + type(exc).__name__ + "：" + str(exc)[:100])
                # 将当前 requests/curl_cffi 会话的 Cookie 原样转入浏览器上下文。
                cookies = []
                for cookie in session.cookies:
                    domain = str(getattr(cookie, "domain", "") or "")
                    if not domain:
                        continue
                    item = {
                        "name": str(cookie.name),
                        "value": str(cookie.value),
                        "domain": domain,
                        "path": str(getattr(cookie, "path", "/") or "/"),
                    }
                    if getattr(cookie, "secure", False):
                        item["secure"] = True
                    expires = getattr(cookie, "expires", None)
                    if expires:
                        try:
                            item["expires"] = float(expires)
                        except Exception:
                            pass
                    cookies.append(item)
                loaded_cookies = 0
                for cookie in cookies:
                    try:
                        context.add_cookies([cookie])
                        loaded_cookies += 1
                    except Exception:
                        # 单个不兼容 Cookie 不应阻断同一会话里的其它 Cookie。
                        continue
                # 确保浏览器 Cookie 里的 oai-did 与指纹 device_id 一致（登录态身份统一）
                try:
                    has_oaidid = any(str(x.get("name") or "") == "oai-did" for x in cookies)
                    if not has_oaidid and fp.get("device_id"):
                        context.add_cookies([{
                            "name": "oai-did",
                            "value": str(fp["device_id"]),
                            "domain": ".chatgpt.com",
                            "path": "/",
                        }])
                        loaded_cookies += 1
                except Exception:
                    pass
                page = context.new_page()
                page.set_default_timeout(0)
                page.set_default_navigation_timeout(0)
                browser_events = []

                def _record_browser_event(kind: str, detail: str) -> None:
                    # 只保留脱敏后的诊断摘要，避免把授权 query/用户数据写进任务日志。
                    text = str(detail or "")
                    text = re.sub(
                        r"https?://[^\s\"']+",
                        lambda match: _safe_page_label(match.group(0)),
                        text,
                    )
                    browser_events.append((str(kind), text[:180]))
                    if len(browser_events) > 30:
                        del browser_events[:-30]

                page.on("pageerror", lambda exc: _record_browser_event("pageerror", str(exc)))
                page.on("requestfailed", lambda request: _record_browser_event(
                    "requestfailed",
                    (str(request.failure or "") + " " + _safe_page_label(request.url)),
                ))
                page.on("console", lambda message: (
                    _record_browser_event("console", str(message.text))
                    if str(message.type).lower() in ("error", "warning") else None
                ))
                # 二维码不是必然落到 img/canvas；GCash 可能通过 fetch/XHR 返回
                # JSON 字段里的 Base64。先缓存响应对象，在主轮询里读取 body，
                # 这样仍处于同一个 Playwright 页面、Cookie、代理和指纹上下文。
                pending_responses = []
                navigation_urls = []
                checkout_return_urls = []
                checkout_return_logged = False

                def _remember_checkout_return(value: str) -> None:
                    nonlocal checkout_return_logged
                    candidate = _checkout_verify_return_url("", value)
                    if not candidate:
                        return
                    checkout_return_urls.append(candidate)
                    if len(checkout_return_urls) > 8:
                        del checkout_return_urls[:-8]
                    if not checkout_return_logged:
                        checkout_return_logged = True
                        log("continue", "run", "已捕获 checkout/verify 的 redirectResult（仅保存在当前进程内，不写入日志）")

                def _queue_browser_response(response) -> None:
                    pending_responses.append(response)
                    if len(pending_responses) > 160:
                        del pending_responses[:-160]
                    # redirectResult 可能只在 Adyen 302 的 Location 中短暂出现，
                    # /checkout/verify 随后因无 NextAuth Cookie 跳到根路径。必须在
                    # 响应事件发生时保存，不能依赖最终 page.url。
                    try:
                        status = int(response.status or 0)
                        if 300 <= status < 400:
                            location = str((response.headers or {}).get("location") or "")
                            candidate = _checkout_verify_return_url(str(response.url or ""), location)
                            if candidate:
                                _remember_checkout_return(candidate)
                    except Exception:
                        pass

                # 主页面与弹出窗口共用同一套捕获逻辑（同一 context = 同一 Cookie/代理/指纹）。
                # GCash 授权页的支付完成跳转可能走 window.open/iframe；新窗口的
                # response/request/navigation 必须同队列捕获，否则 redirectResult 丢失。
                def _attach_payment_events(target_page) -> None:
                    try:
                        target_page.on("response", _queue_browser_response)
                        target_page.on("request", lambda request: _remember_checkout_return(str(request.url or "")))
                    except Exception:
                        pass

                    def _remember_navigation(frame) -> None:
                        try:
                            if frame == target_page.main_frame:
                                frame_url = str(frame.url or "")
                                navigation_urls.append(frame_url)
                                _remember_checkout_return(frame_url)
                                if len(navigation_urls) > 20:
                                    del navigation_urls[:-20]
                        except Exception:
                            pass

                    try:
                        target_page.on("framenavigated", _remember_navigation)
                    except Exception:
                        pass

                _attach_payment_events(page)
                try:
                    context.on("page", _attach_payment_events)
                except Exception:
                    pass
                log("resolve", "run", "二维码浏览器已启动，复用会话 Cookie=" + str(loaded_cookies) + "，等待进入 GCash 页面")
                goto_kwargs = {
                    # 先拿到页面提交，再由下面的循环等待 JS/重定向完成；避免
                    # Adyen 中间页资源迟迟不结束时把扫描阻塞在 page.goto。
                    "wait_until": "commit",
                    "timeout": _BROWSER_NAVIGATION_TIMEOUT_MS,
                }
                # 当入口本身就是 Adyen 地址时，不要把它作为自己的 Referer；
                # 浏览器需要从 Adyen 入口实际经历跳转，才能建立 GCash 页面所需的状态。
                if referer and referer != url:
                    goto_kwargs["referer"] = referer
                phase = "进入页面"
                page.goto(url, **goto_kwargs)
                # 二维码可能由页面脚本延迟渲染；保持当前会话持续扫描，直到图片实际出现。
                last_diag = 0.0
                reached_gcash = False
                last_image = ""
                awaiting_refresh_image = False
                refresh_started_at = 0.0
                network_responses_scanned = 0
                page_state_values_scanned = 0
                def _reload_qr_page(reason: str) -> bool:
                    """手动刷新/5分钟自动刷新/超时重试 共用同一套刷新：在当前浏览器
                    上下文内用 page.goto 重新进入 GCash 授权链（不新建窗口/上下文/
                    换指纹），获取新的二维码。用 goto 而非 reload：GCash 授权 URL 的
                    一次性状态 + reload 偶发 chrome-error 失败页会被外层循环永远等下去。
                    """
                    nonlocal awaiting_refresh_image, refresh_started_at, reached_gcash, last_diag, last_image
                    pending_responses.clear()
                    try:
                        page.goto(url, **goto_kwargs)
                    except Exception as exc:
                        log("resolve", "warn",
                            ("[" + reason + "] 刷新重新加载失败：" + type(exc).__name__ + "：" + str(exc)[:140]) +
                            "（保留当前页面，稍后自动重试）")
                        return False
                    awaiting_refresh_image = True
                    refresh_started_at = time.monotonic()
                    reached_gcash = False
                    last_diag = 0.0
                    last_image = ""
                    log("resolve", "run", "[" + reason + "] 刷新：已在同一会话重新加载 GCash 授权页，等待新二维码")
                    return True

                while True:
                    if stop_event and stop_event.is_set():
                        return ""
                    if session_deadline and time.monotonic() >= session_deadline:
                        log("resolve", "warn", "二维码会话已达到 20 分钟上限，浏览器已自动回收；如仍需付款请重新提链")
                        if stop_event:
                            stop_event.set()
                        return ""
                    # ── 统一刷新入口（手动/自动/重试同一套）──
                    # ① 用户手动刷新（refresh_event）：任何时刻到达都立即消费，
                    #    不依赖 awaiting_refresh_image——否则"二维码未到/页面停在
                    #    失败页"时事件被搁置，手动刷新永远没反应。
                    manual_refresh_requested = bool(
                        keep_alive and refresh_event and refresh_event.is_set() and not awaiting_refresh_image
                    )
                    if manual_refresh_requested:
                        if refresh_event:
                            refresh_event.clear()
                        _reload_qr_page("手动")
                        continue
                    # ② 刷新后 15 秒仍无新图：自动重试（含手动刷新失败后的重试）
                    refresh_retry_due = bool(
                        keep_alive and awaiting_refresh_image and refresh_started_at and
                        time.monotonic() - refresh_started_at >= _QR_REFRESH_RETRY_SECONDS
                    )
                    # ③ 等待新图期间用户再次点击：不重复加载，提示正在刷新中
                    #    （重复 goto 会打断正在进行的同一个刷新导航，且不会更快）；
                    #    前端收到"正在刷新中"提示，等 25 秒重试路径或新图回调即可。
                    refresh_requested_again = bool(
                        keep_alive and awaiting_refresh_image and refresh_event and refresh_event.is_set()
                    )
                    if refresh_requested_again:
                        if refresh_event:
                            refresh_event.clear()
                        log("resolve", "run", "正在刷新中：新二维码尚未返回，本次点击已忽略（等待 25 秒自动重试或新图回调）")
                        continue
                    if refresh_retry_due:
                        _reload_qr_page("刷新重试")
                        continue
                    # ④ 页面停在 Chromium 错误页（chrome-error:// 等）：
                    #    自动刷新/跳转失败的后遗症，用同一 goto 路径恢复。
                    active_url = str(page.url or "")
                    if active_url.startswith("chrome-error://") or not active_url.startswith("http"):
                        log("resolve", "run", "页面处于 Chromium 错误页（加载失败），在当前会话重新进入 GCash 授权页")
                        _reload_qr_page("错误页恢复")
                        continue
                    is_gcash_page = "m.gcash.com" in active_url.lower() and not re.search(
                        r"\.(js|css|png|jpg|jpeg|svg|ico|woff2?)(\?|$)", active_url, re.I
                    )
                    if not is_gcash_page:
                        now = time.monotonic()
                        if now - last_diag >= 10:
                            log("resolve", "run", "二维码等待跳转中：当前页面=" + _safe_page_label(active_url))
                            last_diag = now
                        page.wait_for_timeout(1000)
                        continue
                    if not reached_gcash:
                        reached_gcash = True
                        log("resolve", "run", "已进入 GCash 页面，开始持续扫描渲染元素：" + _safe_page_label(active_url))
                    # 兼容备用版本在不同 Playwright 版本中的行为：显式扫描
                    # Page 主文档，再扫描 page.frames，避免主 frame 尚未出现在
                    # frames 快照时漏掉二维码状态。
                    frames = [page] + list(page.frames)
                    candidates = []
                    element_diag = []
                    visible_elements = 0
                    marked_elements = 0
                    rendered_elements = 0
                    seen_elements = set()
                    handled_image = False

                    # 先消费网络响应。这里允许响应没有 qr 字段名：只要正文中
                    # 出现真实 PNG/JPEG/GIF/WebP Base64 文件头，就包装成图片回调。
                    network_image = ""
                    network_image_url = ""
                    while pending_responses:
                        response = pending_responses.pop(0)
                        network_responses_scanned += 1
                        image, response_url = _extract_qr_from_browser_response(response)
                        if image and _looks_like_qr_image(image):
                            network_image = image
                            network_image_url = response_url
                            break
                        if image and image.startswith("data:image/") and not _looks_like_qr_image(image):
                            # WebP/JPEG 等非 PNG 350：浏览器 canvas 按自然尺寸重采样为 PNG 后再校验
                            png = _canvasify_via_browser(page, image)
                            if _looks_like_qr_image(png):
                                network_image = png
                                network_image_url = response_url
                                break

                    # 有些页面先把接口返回的 Base64 写入脚本/状态，再异步挂到 DOM；
                    # 这类数据不会以可选中的 img 或独立二维码 URL 出现。仍在当前
                    # frame、Cookie、代理和指纹上下文内扫描，并继续使用严格尺寸校验。
                    if not network_image:
                        for frame in frames:
                            page_values = _extract_qr_from_page_state(frame)
                            page_state_values_scanned += len(page_values)
                            for value in page_values:
                                image = _extract_qr_data_image(
                                    str(value),
                                    source_hint=str(frame.url or ""),
                                    allow_unmarked=True,
                                )
                                if image:
                                    network_image = image
                                    network_image_url = str(frame.url or "")
                                    break
                            if network_image:
                                break
                    if network_image:
                        if not keep_alive:
                            log("resolve", "run", "已从 GCash 网络响应捕获 Base64 二维码图片：" + _safe_page_label(network_image_url))
                            return network_image
                        image_changed = network_image != last_image
                        # _reload_qr_page 会清空 last_image 以强制"刷新后新图必变"；
                        # 因此刷新后第一张新图落入 not last_image 分支。该分支必须
                        # 一并复位 awaiting_refresh_image，否则 25 秒后再触发"刷新重试"
                        # 死循环（用户看到二维码一直刷新）。
                        if not last_image:
                            last_image = network_image
                            awaiting_refresh_image = False
                            refresh_started_at = 0.0
                            if callable(on_image):
                                on_image(network_image)
                            handled_image = True
                            if image_changed:
                                log("resolve", "run", "二维码刷新完成，已从网络响应回调新图片")
                            else:
                                log("resolve", "run", "已从 GCash 网络响应捕获 Base64 二维码图片，保持当前会话等待用户手动刷新")
                        elif awaiting_refresh_image and image_changed:
                            if callable(on_image):
                                on_image(network_image)
                            last_image = network_image
                            awaiting_refresh_image = False
                            refresh_started_at = 0.0
                            handled_image = True
                            log("resolve", "run", "二维码刷新完成，已从网络响应回调新图片")

                    # GCash 页面通常先渲染 #qrcode 容器，再由 JS 把 data:image
                    # 写入其中的 img。通用选择器必须放在标记选择器之后。
                    selectors = (
                        "#qrcode img, #qrcode canvas, #qrcode svg",
                        "#qrcode",
                        "[id*='qrcode'] img, [id*='qrcode'] canvas, [id*='qrcode'] svg",
                        "[id*='qrcode']",
                        "[class*='qrcode'] img, [class*='qrcode'] canvas, [class*='qrcode'] svg",
                        "[class*='qrcode']",
                        "canvas, img, svg, [role='img'], [style*='background']",
                    )
                    for frame in frames:
                        if handled_image:
                            break
                        for selector in selectors:
                            if handled_image:
                                break
                            try:
                                locator = frame.locator(selector)
                                count = min(locator.count(), 120)
                            except Exception:
                                continue
                            selector_marked = bool(re.search(r"qrcode|qr.?code", selector, re.I))
                            for index in range(count):
                                element = locator.nth(index)
                                try:
                                    if not element.is_visible():
                                        continue
                                    visible_elements += 1
                                    info = element.evaluate(
                                        """el => {
                                          const rect = el.getBoundingClientRect();
                                          const style = getComputedStyle(el);
                                          const tag = el.tagName.toLowerCase();
                                          const image = tag === 'img' ? el : el.querySelector('img');
                                          const className = typeof el.className === 'string' ? el.className : '';
                                          const src = image && typeof image.currentSrc === 'string' ? image.currentSrc : '';
                                          return {
                                            tag,
                                            id: el.id || '',
                                            className,
                                            alt: el.getAttribute('alt') || '',
                                            aria: el.getAttribute('aria-label') || '',
                                            testid: el.getAttribute('data-testid') || '',
                                            src,
                                            width: rect.width || 0,
                                            height: rect.height || 0,
                                            naturalWidth: image ? image.naturalWidth || 0 : 0,
                                            naturalHeight: image ? image.naturalHeight || 0 : 0,
                                            background: style.backgroundImage && style.backgroundImage !== 'none',
                                            complete: image ? !!image.complete : false
                                          };
                                        }"""
                                    ) or {}
                                    width = float(info.get("width") or 0)
                                    height = float(info.get("height") or 0)
                                    if width <= 0 or height <= 0:
                                        continue
                                    attrs = " ".join(
                                        str(info.get(name) or "")
                                        for name in ("id", "className", "alt", "aria", "testid", "src")
                                    ).lower()
                                    marked = selector_marked or bool(re.search(r"qr|qr.?code|二维码", attrs, re.I))
                                    if marked:
                                        marked_elements += 1
                                    if re.search(r"option\d|ins_photo\d|gcat_loader", attrs, re.I):
                                        continue
                                    rendered_elements += 1
                                    tag = str(info.get("tag") or "").lower()
                                    min_dimension = 32 if marked else 80
                                    if width < min_dimension or height < min_dimension:
                                        continue
                                    ratio = width / height if height else 0
                                    # 标记为二维码的元素允许非标准容器比例；未标记元素
                                    # 仍需接近方形，避免把说明图误当成二维码。
                                    if not marked and (ratio < 0.45 or ratio > 2.2):
                                        continue
                                    key = (
                                        str(frame.url or ""),
                                        str(info.get("tag") or ""),
                                        str(info.get("id") or ""),
                                        str(info.get("className") or ""),
                                        round(width, 1),
                                        round(height, 1),
                                    )
                                    if key in seen_elements:
                                        continue
                                    seen_elements.add(key)
                                    score = 1000 if marked else 0
                                    if tag == "canvas":
                                        score += 70
                                    elif tag == "svg":
                                        score += 25
                                    elif tag == "img":
                                        score += 35 if int(info.get("naturalWidth") or 0) > 0 else 15
                                    elif info.get("background"):
                                        score += 20
                                    else:
                                        score += 10
                                    candidates.append((score, width * height, element))
                                    # 诊断样本：记录元素显示尺寸/自然尺寸/类型，
                                    # 用于"提链成功但二维码识别不到"时定位格式与缩放差异。
                                    if len(element_diag) < 12 and (marked or tag in ("canvas", "img")):
                                        element_diag.append({
                                            "tag": tag,
                                            "w": round(width, 0), "h": round(height, 0),
                                            "nw": int(info.get("naturalWidth") or 0),
                                            "nh": int(info.get("naturalHeight") or 0),
                                            "marked": marked,
                                        })
                                except Exception:
                                    continue
                    # 服务器可工作的旧版使用 bounding_box() 扫描通用元素；
                    # 某些 Chromium 页面在 evaluate(getComputedStyle) 路径下
                    # 会漏掉这些元素。两套候选都参与首轮扫描，避免现代扫描器
                    # 找到说明图后就跳过服务器版候选发现；最终仍由
                    # _as_data_image(require_qr_size=True) 严格过滤结果。
                    legacy_candidates = []
                    if not handled_image:
                        legacy_locator_selector = "canvas, img, svg, [style*='background']"
                        for frame in frames:
                            try:
                                locator = frame.locator(legacy_locator_selector)
                                count = min(locator.count(), 120)
                            except Exception:
                                continue
                            for index in range(count):
                                element = locator.nth(index)
                                try:
                                    if not element.is_visible():
                                        continue
                                    box = element.bounding_box()
                                    if not box:
                                        continue
                                    width = float(box.get("width") or 0)
                                    height = float(box.get("height") or 0)
                                    if width < 100 or height < 100:
                                        continue
                                    ratio = width / height if height else 0
                                    if ratio < 0.72 or ratio > 1.38:
                                        continue
                                    attrs = " ".join(
                                        str(element.get_attribute(name) or "")
                                        for name in ("id", "class", "alt", "aria-label", "data-testid", "src")
                                    ).lower()
                                    if re.search(r"option\d|ins_photo\d|gcat_loader", attrs, re.I):
                                        continue
                                    tag = str(element.evaluate("el => el.tagName.toLowerCase()") or "")
                                    score = 100 if re.search(r"qr|qr.?code|二维码", attrs, re.I) else 0
                                    score += {"canvas": 70, "svg": 25, "img": 15}.get(tag, 10)
                                    legacy_candidates.append((score, width * height, element))
                                except Exception:
                                    continue
                    candidates.extend(legacy_candidates)

                    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
                    for _, _, element in candidates:
                        try:
                            # GCash 成功页明确把二维码写成 #qrcode img 的 data URL。
                            # 直接回调该图片，避免页面容器/动画导致 screenshot 截到空白；
                            # 这里只传输图片，不解析二维码内容。
                            # 元素取值优先级：data URL 直取 → 非 PNG data URL 浏览器 canvas
                            # 重采样 → img http(s) 原始色块在页面内重绘 → 元素截图。
                            # 所有结果都必须通过 350x350 PNG 严格校验（尺寸门槛不放宽）。
                            direct_image = element.evaluate(
                                """el => {
                                  const image = el.tagName.toLowerCase() === 'img' ? el : el.querySelector('img');
                                  const src = image && (image.currentSrc || image.src || '');
                                  return typeof src === 'string' && src.startsWith('data:image/') ? src : '';
                                }"""
                            ) or ""
                            data_url = ""
                            if direct_image and len(direct_image) <= _MAX_QR_IMAGE_BYTES * 2:
                                if _looks_like_qr_image(str(direct_image)):
                                    data_url = str(direct_image)
                                else:
                                    png = _canvasify_via_browser(page, str(direct_image))
                                    if _looks_like_qr_image(png):
                                        data_url = png
                            if not data_url:
                                # img 的 src 是 http(s) 资源（CDN 返回 WebP/JPEG 等情况）：
                                # 在页面内重绘为 data URL 再走同一套 350 校验。
                                remote_src = element.evaluate(
                                    """el => {
                                      const image = el.tagName.toLowerCase() === 'img' ? el : el.querySelector('img');
                                      const src = image && (image.currentSrc || image.src || '');
                                      return (typeof src === 'string' && (src.startsWith('http://') || src.startsWith('https://'))) ? src : '';
                                    }"""
                                ) or ""
                                if remote_src:
                                    canvas_url = element.evaluate(
                                        """async (url) => {
                                          try {
                                            const img = new Image();
                                            const loaded = await new Promise((resolve) => {
                                              img.onload = () => resolve(true);
                                              img.onerror = () => resolve(false);
                                              img.src = url;
                                            });
                                            if (!loaded) return '';
                                            const c = document.createElement('canvas');
                                            c.width = img.naturalWidth || img.width;
                                            c.height = img.naturalHeight || img.height;
                                            c.getContext('2d').drawImage(img, 0, 0);
                                            return c.toDataURL('image/png');
                                          } catch (e) { return ''; }
                                        }""",
                                        remote_src,
                                    ) or ""
                                    if _looks_like_qr_image(str(canvas_url)):
                                        data_url = str(canvas_url)
                                    else:
                                        # 跨域/协商图片无法在页面 canvas 重绘时，用本引擎
                                        # requests 会话（同一 Cookie/代理/指纹）直接下载原始
                                        # 资源，再做 350x350 PNG 校验——与提链全程同一身份。
                                        try:
                                            raw = session.get(remote_src, timeout=timeout)
                                            if raw.status_code < 400:
                                                fetched = _as_data_image(
                                                    raw.content,
                                                    str(raw.headers.get("Content-Type") or "image/png"),
                                                    require_qr_size=True,
                                                )
                                                if fetched:
                                                    data_url = fetched
                                        except Exception:
                                            pass
                            if not data_url:
                                image = element.screenshot(type="png")
                                data_url = _as_data_image(image, "image/png", require_qr_size=True)
                            if data_url:
                                if not keep_alive:
                                    return data_url
                                image_changed = data_url != last_image
                                # 首次图片只回传给提链结果；后续只有收到手动刷新指令
                                # 并且截图确实变化时，才回调新图片。
                                if not last_image:
                                    last_image = data_url
                                    # 刷新后第一张图（_reload_qr_page 清空了 last_image）同样
                                    # 必须复位 awaiting_refresh_image，否则 25 秒后再次触发
                                    # "刷新重试"死循环（用户看到二维码一直刷新）。
                                    awaiting_refresh_image = False
                                    refresh_started_at = 0.0
                                    if callable(on_image):
                                        on_image(data_url)
                                    handled_image = True
                                    if image_changed:
                                        log("resolve", "run", "二维码刷新完成，已回调新图片")
                                    else:
                                        log("resolve", "run", "二维码已获取，保持当前会话等待用户手动刷新")
                                    break
                                if awaiting_refresh_image:
                                    if not image_changed:
                                        continue
                                    if callable(on_image):
                                        on_image(data_url)
                                    last_image = data_url
                                    awaiting_refresh_image = False
                                    refresh_started_at = 0.0
                                    handled_image = True
                                    log("resolve", "run", "二维码刷新完成，已回调新图片")
                                    break
                                # 页面自身的变化不触发前端回调；刷新动作必须来自按钮。
                                handled_image = False
                                break
                        except Exception:
                            continue
                    if handled_image:
                        if keep_alive and not awaiting_refresh_image:
                            # 支付完成检测（同窗口同登录态）：等待扫码支付期间轮询
                            # 页面状态 —— 成功文案 / 跳转离开授权页 / 二维码区域消失。
                            paid_wait_start = time.monotonic()
                            auto_refresh_seconds = max(60.0, float(
                                str(os.environ.get("GCASH_QR_REFRESH_SECONDS") or _QR_AUTO_REFRESH_SECONDS).strip()
                                or _QR_AUTO_REFRESH_SECONDS
                            ))
                            qr_refresh_deadline = paid_wait_start + auto_refresh_seconds
                            refresh_reason = ""
                            log("resolve", "run", "二维码已就绪，保持生成该二维码的原浏览器会话等待扫码和回调…")
                            return_started_at = 0.0
                            continue_observed = False
                            subscription_poll_attempted = False
                            fallback_attempted = False
                            callback_evidence_at = 0.0
                            scan_confirmed_hint = {"value": False}

                            def _probe_scan_confirmed() -> None:
                                """扫码确认迹象探测（保守版）：只有在"二维码曾经真实显示过
                                （last_image 非空）之后容器消失/确认文案出现"才视为扫码确认。
                                防止页面普通文案含 success/confirmed 等词时误判为已扫码，
                                把自动刷新永久冻结（用户反馈"自动刷新一直等待、刷不出来"）。
                                """
                                try:
                                    hint = page.evaluate(
                                        """() => {
                                          const body = (document.body ? document.body.innerText : '') || '';
                                          const qrBox = document.querySelector('#qrcode, [id*="qrcode" i], [class*="qrcode" i]');
                                          const qrVisible = !!qrBox && qrBox.getBoundingClientRect().width > 0 && qrBox.getBoundingClientRect().height > 0;
                                          const conf = /(已成功|已授权|已确认|授权成功|扫码成功|支付成功|已验证|payment\\s+successful|authorised|authorized)/i.test(body);
                                          return { qrVisible: qrVisible, conf: conf };
                                        }"""
                                    ) or {}
                                    if isinstance(hint, dict):
                                        if not last_image:
                                            return
                                        if hint.get("conf") and not hint.get("qrVisible"):
                                            scan_confirmed_hint["value"] = True
                                            log("continue", "run", "检测到扫码确认迹象（二维码区已消失/显示确认态），视同支付回调已开始，停止自动刷新")
                                except Exception:
                                    pass

                            def _payment_callback_started() -> bool:
                                """支付回调是否已开始（强证据）：页面已跳转离开授权页 / continue 已成功 /
                                页面已显示扫码确认态。checkout_return_urls 可能被页面预加载误捕获，
                                不作为强证据（否则自动刷新会被永久冻结）。"""
                                return bool(return_started_at or continue_observed
                                            or scan_confirmed_hint["value"])
                            while True:
                                if refresh_event and refresh_event.is_set():
                                    refresh_event.clear()
                                    # 手动刷新是用户明确意图：即使已捕获回调迹象也执行
                                    # （同一会话 goto 重取新码；用户既然点刷新说明旧码已弃用，
                                    # 此刻等待回调无意义）。自动刷新仍受回调保护。
                                    refresh_reason = "手动（用户强制）"
                                    break
                                if stop_event and stop_event.is_set():
                                    return ""
                                if session_deadline and time.monotonic() >= session_deadline:
                                    log("resolve", "warn", "二维码会话已达到 20 分钟上限，浏览器已自动回收；如仍需付款请重新提链")
                                    if stop_event:
                                        stop_event.set()
                                    return ""
                                if time.monotonic() >= qr_refresh_deadline:
                                    if _payment_callback_started():
                                        # 回调证据出现后停止自动刷新，专心等待支付确认（每人扫码
                                        # 只需一次，二维码已无关紧要；继续刷新反而中断回调链路）。
                                        qr_refresh_deadline = time.monotonic() + 180.0
                                    else:
                                        refresh_reason = "5 分钟自动"
                                        break
                                # 首次观察到支付回调证据时记录时间（用于 fallback 计时）
                                if _payment_callback_started() and not callback_evidence_at:
                                    callback_evidence_at = time.monotonic()
                                # 探测扫码确认态（二维码容器消失/确认文案）——覆盖跳转前的窗口期
                                _probe_scan_confirmed()
                                # 每 3 秒检测一次支付完成信号
                                page.wait_for_timeout(3000)
                                try:
                                    _cur = str(page.url or "")
                                    if "m.gcash.com" not in _cur.lower() and _cur.startswith("http"):
                                        if not return_started_at:
                                            return_started_at = time.monotonic()
                                            log("continue", "run", "已收到 GCash/Adyen 浏览器返回，等待 OpenAI continue 与订阅生效")

                                    while pending_responses:
                                        observed = pending_responses.pop(0)
                                        observed_url = str(observed.url or "")
                                        observed_status = int(observed.status or 0)
                                        if "/custom_payment_method/continue" in observed_url and 200 <= observed_status < 300:
                                            continue_observed = True
                                            log("continue", "ok", "浏览器 continue 请求成功，等待 subscriptions 确认 Plus")
                                        if "/backend-api/subscriptions" in observed_url and 200 <= observed_status < 300:
                                            try:
                                                if _has_active_plus(observed.json()):
                                                    log("subscriptions", "ok", "Plus 订阅已生效（浏览器返回 plan_type=plus）")
                                                    if callable(on_paid):
                                                        on_paid("plus_active")
                                                    return ""
                                            except Exception:
                                                pass

                                    if continue_observed and not subscription_poll_attempted:
                                        subscription_poll_attempted = True
                                        if _verify_plus_subscription(
                                            session,
                                            access_token,
                                            device_id,
                                            session_id,
                                            fp,
                                            timeout,
                                            log,
                                        ):
                                            if callable(on_paid):
                                                on_paid("plus_active")
                                            return ""

                                    # 提交 continue 不依赖顶层 URL 离开 m.gcash.com：
                                    # GCash/Adyen 的回调跳转可能发生在 iframe 或新窗口（顶层 URL 不动），
                                    # 只要从 302 Location / 请求 / 导航里捕获到 redirectResult 就应提交。
                                    if (return_started_at or checkout_return_urls) and not continue_observed \
                                            and not fallback_attempted and callback_evidence_at \
                                            and time.monotonic() - callback_evidence_at >= 12.0:
                                        fallback_attempted = True
                                        verify_url = next(
                                            (item for item in reversed(checkout_return_urls + navigation_urls + [_cur])
                                             if _checkout_verify_return_url("", item)),
                                            "",
                                        )
                                        action_result = _action_result_from_verify_url(verify_url)
                                        if not action_result:
                                            log("continue", "fail", "未捕获到 Adyen 302 Location 中的 redirectResult，不能提交空 action_result")
                                        if _server_finish_and_verify(
                                            session,
                                            access_token,
                                            checkout_session_id,
                                            action_result,
                                            device_id,
                                            session_id,
                                            fp,
                                            timeout,
                                            log,
                                        ):
                                            if callable(on_paid):
                                                on_paid("plus_active")
                                            return ""
                                except Exception:
                                    continue
                            if stop_event and stop_event.is_set():
                                return ""
                            if stop_event and stop_event.is_set():
                                return ""
                            # 手动/自动刷新统一走同一套 goto 路径（同一 page/上下文/指纹/代理，
                            # 不新建窗口、不新建会话）：与外层"刷新重试/错误页恢复"完全一致。
                            _reload_qr_page(refresh_reason or "自动")
                        # 进入下一轮扫描；同一浏览器上下文、Cookie、代理和指纹保持不变。
                        continue
                    now = time.monotonic()
                    if now - last_diag >= 10:
                        title = ""
                        try:
                            title = str(page.title() or "")[:80]
                        except Exception:
                            pass
                        page_diag = ""
                        try:
                            summaries = []
                            for frame in frames[:8]:
                                summary = frame.evaluate(
                                    """() => {
                                      const body = document.body;
                                      const qr = [...document.querySelectorAll('#qrcode, [id*="qrcode" i], [class*="qrcode" i]')];
                                      const images = [...document.images];
                                      return {
                                        url: location.href,
                                        ready: document.readyState,
                                        elements: document.querySelectorAll('*').length,
                                        bodyText: body ? (body.innerText || '').replace(/\\s+/g, ' ').slice(0, 180) : '',
                                        qr: qr.map(el => ({
                                          tag: el.tagName.toLowerCase(),
                                          id: el.id || '',
                                          className: typeof el.className === 'string' ? el.className : '',
                                          width: el.getBoundingClientRect().width || 0,
                                          height: el.getBoundingClientRect().height || 0,
                                          images: el.querySelectorAll('img').length,
                                          canvases: el.querySelectorAll('canvas').length,
                                          dataImages: [...el.querySelectorAll('img')].filter(img => (img.currentSrc || img.src || '').startsWith('data:image/')).length
                                        })),
                                        images: images.map(img => ({
                                          width: img.getBoundingClientRect().width || 0,
                                          height: img.getBoundingClientRect().height || 0,
                                          naturalWidth: img.naturalWidth || 0,
                                          naturalHeight: img.naturalHeight || 0,
                                          dataImage: (img.currentSrc || img.src || '').startsWith('data:image/')
                                        })).slice(0, 20)
                                      };
                                    }"""
                                ) or {}
                                summary["url"] = _safe_page_label(summary.get("url") or "")
                                summaries.append(summary)
                            page_diag = json.dumps(summaries, ensure_ascii=False, separators=(",", ":"))[:1600]
                        except Exception as exc:
                            page_diag = "diag_error=" + type(exc).__name__
                        recent_events = ""
                        if browser_events:
                            recent_events = "，浏览器事件=" + json.dumps(browser_events[-4:], ensure_ascii=False, separators=(",", ":"))[:900]
                        log(
                            "resolve",
                            "run",
                            "二维码等待中：可见元素=" + str(visible_elements) +
                            "，二维码标记=" + str(marked_elements) +
                            "，已渲染=" + str(rendered_elements) +
                            "，候选=" + str(len(candidates)) +
                            "，frames=" + str(len(frames)) +
                            "，网络响应已扫描=" + str(network_responses_scanned) +
                            "，页面 Base64 候选=" + str(page_state_values_scanned) +
                            ("，标题=" + title if title else "") +
                            ("，元素样本=" + json.dumps(element_diag[:8], ensure_ascii=False, separators=(",", ":")) if element_diag else ""),
                        )
                        if page_diag:
                            log("resolve", "run", "二维码页面诊断：" + page_diag + recent_events)
                        last_diag = now
                    # 页面脚本可能稍后才把二维码绘制到 canvas/img；持续等待并重新扫描。
                    page.wait_for_timeout(1000)
            finally:
                browser.close()
                try:
                    if _socks_bridge_holder and _socks_bridge_holder[0]:
                        _socks_bridge_holder[0].close()
                except Exception:
                    pass
    except Exception as exc:
        log("resolve", "warn", "二维码浏览器阶段失败（" + phase + "）: " + type(exc).__name__ + "：" + str(exc)[:140])
        return ""
    return ""


def _paid_event_callback(emit):
    """支付完成回调：通过 stdout 输出 [paid] 事件，Node 侧作为 stage 事件转发前端。"""
    def _cb(url: str) -> None:
        try:
            emit("paid", "ok", "支付完成（同窗口同登录态）：" + str(url)[:120])
        except Exception:
            pass
        # 附加一行机器可读事件（extractor 的 onStage 会转发给前端）
        try:
            print("[paid] [ok] " + str(url)[:200], flush=True)
        except Exception:
            pass
    return _cb


def _start_qr_refresh_monitor(
    url: str,
    session,
    proxy: str,
    timeout: float,
    referer: str = "",
    fingerprint: Optional[dict] = None,
    emit: Optional[Callable[[str, str, str], None]] = None,
    on_image: Optional[Callable[[str], None]] = None,
    refresh_event: Optional[threading.Event] = None,
    access_token: str = "",
    on_paid: Optional[Callable[[str], None]] = None,
    checkout_session_id: str = "",
    device_id: str = "",
    session_id: str = "",
    wait_for_first_image: bool = False,
) -> str:
    """首次回显后保持二维码浏览器进程，每 5 分钟或按手动指令刷新。"""
    if not callable(on_image):
        return
    if refresh_event is None:
        refresh_event = threading.Event()
    stop_event = threading.Event()
    first_image_event = threading.Event()
    first_image = {"value": ""}
    log = emit or (lambda stage, status, msg: None)
    max_session_seconds = max(300.0, float(
        str(os.environ.get("GCASH_QR_SESSION_SECONDS") or _QR_SESSION_MAX_SECONDS).strip()
        or _QR_SESSION_MAX_SECONDS
    ))
    session_deadline = time.monotonic() + max_session_seconds

    def relay_image(image: str) -> None:
        if image and not first_image["value"]:
            first_image["value"] = image
            # 先完整输出首次二维码事件，再释放主线程输出最终 JSON，避免两个
            # 超长 Base64 行并发写 stdout 后互相穿插。
            try:
                on_image(image)
            finally:
                first_image_event.set()
            return
        on_image(image)

    def relay_paid(status: str) -> None:
        stop_event.set()
        if callable(on_paid):
            on_paid(status)

    def worker() -> None:
        restart_failures = 0
        while not stop_event.is_set():
            if time.monotonic() >= session_deadline:
                log("resolve", "warn", "二维码会话已达到 20 分钟上限，浏览器已自动回收；如仍需付款请重新提链")
                stop_event.set()
                break
            try:
                _capture_qr_with_browser(
                    url,
                    session,
                    proxy,
                    timeout,
                    referer=referer,
                    fingerprint=fingerprint,
                    emit=log,
                    keep_alive=True,
                    on_image=relay_image,
                    stop_event=stop_event,
                    refresh_event=refresh_event,
                    access_token=access_token,
                    on_paid=relay_paid,
                    checkout_session_id=checkout_session_id,
                    device_id=device_id,
                    session_id=session_id,
                    session_deadline=session_deadline,
                )
            except Exception as exc:
                log("resolve", "warn", "二维码刷新监控异常，将重新建立同身份会话：" + type(exc).__name__ + "：" + str(exc)[:120])
            if stop_event.is_set():
                break
            restart_failures += 1
            if restart_failures >= 3:
                log("resolve", "fail", "二维码浏览器连续 3 次异常退出，已停止该会话以释放系统资源")
                stop_event.set()
                first_image_event.set()
                break
            if stop_event.wait(_QR_BROWSER_RESTART_DELAY_SECONDS):
                break

    # 非 daemon 线程让 gcash_service 在首次 JSON 输出后继续运行，直到
    # Node 侧结束子进程；这样刷新期间 Cookie/浏览器上下文不会被提前销毁。
    try:
        threading.Thread(target=worker, name="gcash-qr-refresh", daemon=False).start()
    except RuntimeError as exc:
        log("resolve", "fail", "二维码浏览器线程启动失败：" + str(exc)[:120])
        first_image_event.set()
        return ""
    if wait_for_first_image:
        # 150 秒等待（浏览器会话内二维码可能慢渲染）；环境变量可覆盖
        try:
            first_image_timeout = float(
                str(os.environ.get("GCASH_QR_WAIT_SECONDS") or _QR_FIRST_IMAGE_TIMEOUT).strip()
                or _QR_FIRST_IMAGE_TIMEOUT
            )
        except Exception:
            first_image_timeout = _QR_FIRST_IMAGE_TIMEOUT
        first_image_timeout = max(30.0, first_image_timeout)
        if not first_image_event.wait(first_image_timeout):
            stop_event.set()
            log("resolve", "fail", "等待首张二维码超过 " + str(int(first_image_timeout)) + " 秒，已终止浏览器会话并交由调度器重试")
            return ""
    return first_image["value"]


def _extract_qr_image(
    body_text: str,
    current_url: str,
    session,
    proxy: str,
    timeout: float,
    referer: str = "",
    fingerprint: Optional[dict] = None,
    emit: Optional[Callable[[str, str, str], None]] = None,
    browser_url: str = "",
    access_token: str = "",
) -> str:
    """优先读页面二维码图片资源，失败后使用带当前 Cookie/代理的浏览器截图。"""
    data_image = _extract_qr_data_image(body_text)
    if data_image:
        return data_image
    src_image = _extract_qr_src_image(body_text, current_url, session, timeout)
    if src_image:
        return src_image
    return _capture_qr_with_browser(
        browser_url or current_url,
        session,
        proxy,
        timeout,
        referer=referer,
        fingerprint=fingerprint,
        emit=emit,
        access_token=access_token,
    )


def _sentinel_fingerprint_kwargs(fp: Optional[dict]) -> dict:
    """把指纹档案的屏幕/平台/内核参数提取为哨兵桥入参（与请求指纹一致）。

    指纹档案无对应字段时回退默认（Win32/1920x1080），与 curl_cffi chrome136 默认一致。
    """
    fp = fp if isinstance(fp, dict) else {}
    platform = str(fp.get("platform") or "Win32")
    platform = re.sub(r"\s+", "", platform)
    if "Mac" in str(fp.get("platform_os") or "") or platform in ("MacIntel", "Mac"):
        platform = "MacIntel"
        platform_label = "macOS"
    elif "win" in str(fp.get("platform_os") or "").lower():
        platform = "Win32"
        platform_label = "Windows"
    else:
        platform_label = str(fp.get("platform_os") or "Windows")
    screen = str(fp.get("screen") or "1920x1080")
    try:
        sw, sh = screen.lower().split("x", 1)
        screen_w, screen_h = int(sw), int(sh)
    except Exception:
        screen_w, screen_h = 1920, 1080
    return {
        "platform": platform,
        "platform_label": platform_label,
        "screen_w": screen_w,
        "screen_h": screen_h,
        "max_touch_points": int(fp.get("max_touch_points") or 0),
    }


def _mint_checkout_risk_context(
    session_get,
    proxy: str,
    device_id: str,
    session_id: str,
    fp: dict,
    promo_campaign_id: str,
    cookie_header: str,
    timeout: float,
    log,
) -> dict:
    """统一为 checkout 生成风控上下文建单头（哨兵 + attestation），任何重试/回退后都必须重新调用。

    返回 dict（可直接作为 extra），保证 proxy/device/UA/平台与请求完全一致。
    """
    sentinel_headers: dict = {}
    try:
        from sentinel_token import mint_sentinel_sync, sentinel_header_value as _shv
        _sn = mint_sentinel_sync(
            flow="chatgpt_checkout",
            device_id=device_id,
            user_agent=str(fp.get("ua") or _CHATGPT_UA),
            proxy=proxy,
            cores=int(fp.get("hardware_concurrency") or 16),
            page_url="https://chatgpt.com/?promo_campaign=plus-1-month-free" if promo_campaign_id else "https://chatgpt.com/",
            language=str(fp.get("oai_language") or "en-US"),
            timezone=str(fp.get("timezone") or "Asia/Manila"),
            cookie_header=cookie_header,
            timeout_s=float(max(timeout, 60) or 60),
            emit=log,
            **_sentinel_fingerprint_kwargs(fp),
        )
        _sv = _shv(_sn)
        if _sv:
            sentinel_headers["openai-sentinel-token"] = _sv
    except Exception as exc:
        log("sentinel", "warn", "哨兵生成异常（按无哨兵降级）：" + type(exc).__name__ + "：" + str(exc)[:150])
    try:
        from attestation import fetch_attestation as _fa
        _av = _fa(
            session_get,
            url="https://chatgpt.com/?promo_campaign=plus-1-month-free" if promo_campaign_id else "https://chatgpt.com/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": str(fp.get("accept_language") or "en-US,en;q=0.9"),
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "User-Agent": str(fp.get("ua") or _CHATGPT_UA),
                "oai-device-id": device_id,
                "oai-session-id": session_id,
                "oai-client-version": _CHATGPT_CLIENT_VERSION,
                "oai-client-build-number": _CHATGPT_CLIENT_BUILD,
                "Cookie": cookie_header,
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
            },
            timeout=timeout,
            emit=log,
        )
        if _av:
            sentinel_headers["oai-web-deployment-attestation"] = _av
    except Exception as exc:
        log("attestation", "warn", "部署证明获取异常（按无 attestation 降级）：" + type(exc).__name__)
    return sentinel_headers


def _approval_risk_headers(
    proxy: str,
    device_id: str,
    session_id: str,
    fingerprint: Optional[dict],
    cookie_header: str,
    *,
    access_token: str,
    entity: str,
    checkout_session_id: str,
    timeout: float,
    emit,
    sentinel_cache: Optional[dict] = None,
) -> dict:
    """confirm/start 阶段所需的风控头（对齐 pay153：flow=checkout_session_approval）。

    建单阶段已领取的哨兵一次使用；这里为新流程重新领取。
    sentinel_cache 可传入共享缓存 dict（"sentinel"/"attestation" 键），失败不阻断。
    """
    log = emit or (lambda stage, status, msg: None)
    out: dict = {}
    if sentinel_cache is not None:
        cached = sentinel_cache.get("sentinel")
        if cached:
            out["openai-sentinel-token"] = cached
            cached_so = sentinel_cache.get("so")
            if cached_so:
                out["OpenAI-Sentinel-SO-Token"] = cached_so
            if sentinel_cache.get("attestation"):
                out["oai-web-deployment-attestation"] = sentinel_cache["attestation"]
            return out
    try:
        from sentinel_token import mint_sentinel_sync, sentinel_header_value as _shv, so_header_value as _sohv
    except Exception as exc:
        log("sentinel", "warn", "确认阶段哨兵模块不可用：" + type(exc).__name__)
        return out
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    token = mint_sentinel_sync(
        flow="checkout_session_approval",
        device_id=device_id,
        user_agent=str(fp.get("ua") or _CHATGPT_UA),
        proxy=proxy,
        cores=int(fp.get("hardware_concurrency") or 16),
        page_url="https://chatgpt.com/checkout/" + str(entity) + "/" + str(checkout_session_id),
        language=str(fp.get("oai_language") or "en-US"),
        timezone=str(fp.get("timezone") or "Asia/Manila"),
        cookie_header=cookie_header,
        timeout_s=float(max(timeout, 60) or 60),
        emit=log,
        **_sentinel_fingerprint_kwargs(fp),
    )
    value = _shv(token)
    if value:
        out["openai-sentinel-token"] = value
        if sentinel_cache is not None:
            sentinel_cache["sentinel"] = value
    # SO token（session-observer 挑战；pay153 GCash confirm 强依赖）
    so_value = _sohv(token.get("_so") if isinstance(token, dict) else None)
    if so_value:
        out["OpenAI-Sentinel-SO-Token"] = so_value
        if sentinel_cache is not None:
            sentinel_cache["so"] = so_value
    # attestation 复用建单阶段抓到的值（如已有缓存）
    if sentinel_cache is not None and sentinel_cache.get("attestation"):
        out["oai-web-deployment-attestation"] = sentinel_cache["attestation"]
    return out


def _fail(stage: str, message: str, reason_code: str, t0: float, extra: Optional[dict] = None) -> dict:
    out = {
        "ok": False, "link": "", "error": message, "reason_code": reason_code,
        "extra": {"payment_method": "gcash", "link_type": "", "error_stage": stage,
                  "retryable": reason_code in ("checkout_rate_limited", "wallet_transport_error", "no_redirect", "start_failed",
                                              "promo_not_applied", "wallet_checkout_not_zero_due", "wallet_method_unavailable_env"),
                  "elapsed_seconds": round(time.monotonic() - t0, 1)},
    }
    if extra:
        out["extra"].update(extra)
    return out


def extract_gcash_custom_link(
    access_token: str,
    proxy: str,
    country: str = "PH",
    currency: str = "PHP",
    promo_campaign_id: str = "plus-1-month-free",
    require_zero: bool = True,
    emit: Optional[Callable[[str, str, str], None]] = None,
    timeout: float = 45.0,
    fingerprint: Optional[dict] = None,
    sticky_proxy: bool = False,
    on_qr_update: Optional[Callable[[str], None]] = None,
    qr_refresh_event: Optional[threading.Event] = None,
    on_paid: Optional[Callable[[str], None]] = None,
) -> dict:
    """GCash custom 完整流程，返回 m.gcash.com Adyen 授权链接。"""
    t0 = time.monotonic()
    log = emit or (lambda stage, status, msg: None)
    force_zero = _force_zero_checkout()
    if force_zero and not promo_campaign_id:
        promo_campaign_id = "plus-1-month-free"
        log("promotion", "warn", "GCASH_FORCE_ZERO=1：已补提交 plus-1-month-free 促销参数")
    requested_cc = str(country or "PH").strip().upper()
    requested_cur = str(currency or "PHP").strip().upper()
    # 用户可自选国家和币种；未指定时默认 PH / PHP。
    cc = requested_cc or "PH"
    cur = requested_cur or "PHP"
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return _fail("validation", "未提供用户代理，GCash 提链必须使用代理池中的代理", "proxy_missing", t0)
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    session = _new_session(proxy, fingerprint=fp)
    # 账号指纹档案的 device_id：同一账号+同建单国家永久稳定，全程身份一致
    device_id = str(fp.get("device_id") or uuid.uuid4())
    # oai-session-id 与设备绑定（指纹档案提供）：单账号单次及跨次提链均不变化
    session_id = str(fp.get("oai_session_id") or str(uuid.uuid4()))
    if sticky_proxy:
        log("config", "run", "粘性代理已启用：建单/出口 IP 全程固定（不轮换）")
    if fp:
        log("fingerprint", "run",
            "账号指纹 " + str(fp.get("kernel_label") or fp.get("kernel") or "chrome") + "/" + str(device_id)[:8]
            + " TLS=" + str(fp.get("impersonate") or "?") + " 语言=" + str(fp.get("oai_language") or "?")
            + " 时区=" + str(fp.get("timezone") or "?") + " 建单国=" + str(fp.get("country") or cc))

    # ── Step 0: 手动页面在建单前读取 PH 定价配置（HAR entry #2）──
    pricing_path = "/backend-api/checkout_pricing_config/configs/" + cc
    log("pricing_config", "run", "读取 " + cc + " 定价配置（对齐浏览器 HAR）")
    try:
        pricing = session.get(
            _PRICING_CONFIG_URL + "/" + cc,
            headers=_chatgpt_headers(access_token, pricing_path, device_id=device_id, session_id=session_id, fingerprint=fp),
            timeout=timeout,
        )
    except Exception as exc:
        return _fail("pricing_config", "定价配置网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
    if pricing.status_code >= 400:
        detail = _response_detail(pricing)
        return _fail(
            "pricing_config",
            "定价配置被上游拒绝 HTTP " + str(pricing.status_code) + ": " + detail,
            "checkout_request_rejected",
            t0,
            # 保留建单失败重试：下层调度会按既有退避策略重新提交。
            extra={"retryable": True, "upstream_status": int(pricing.status_code), "upstream_detail": detail},
        )
    log("pricing_config", "ok", "PH 定价配置已读取")

    # ── Step 1: 创建 Checkout 会话（严格采用 HAR 里的请求体）──
    # GCash 不是这里的 payment_method_types；它由上游在 custom_payment_methods 中返回。
    checkout_body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": cc, "currency": cur},
        "checkout_ui_mode": "custom",
    }
    if promo_campaign_id:
        checkout_body["promo_campaign"] = {"promo_campaign_id": promo_campaign_id, "is_coupon_from_query_param": False}
    # ── Step 1a: warmup chatgpt.com 页面（同会话种 __cf_bm/_cfuvid，对齐 HAR/免费版链路）──
    _session_cookie_header = "oai-did=" + device_id
    try:
        warm_headers = _chatgpt_headers(
            access_token, "", referer="https://chatgpt.com/",
            device_id=device_id, session_id=session_id, fingerprint=fp,
            cookie_header="oai-did=" + device_id,
        )
        warm_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        warm_headers["sec-fetch-dest"] = "document"
        warm_headers["sec-fetch-mode"] = "navigate"
        warm_headers["sec-fetch-site"] = "same-origin"
        warm_headers["upgrade-insecure-requests"] = "1"
        warm = session.get("https://chatgpt.com/?promo_campaign=plus-1-month-free" if promo_campaign_id else "https://chatgpt.com/", headers=warm_headers, timeout=timeout)
        # 把会话 cookie 合并进后续 Cookie 头（oai-did 保持一致）
        try:
            jar_parts = []
            for cookie in getattr(session, "cookies", lambda: [])():
                if str(getattr(cookie, "domain", "") or "").endswith("chatgpt.com"):
                    jar_parts.append(str(cookie.name) + "=" + str(cookie.value))
            if jar_parts:
                _session_cookie_header = "; ".join(jar_parts)
                if "oai-did" not in _session_cookie_header:
                    _session_cookie_header += "; oai-did=" + device_id
        except Exception:
            pass
        log("warmup", "ok", "ChatGPT 页面已预热（HTTP " + str(warm.status_code) + "），会话 cookie 已合并")
    except Exception as exc:
        log("warmup", "warn", "ChatGPT 页面预热失败（不阻断）：" + type(exc).__name__ + "：" + str(exc)[:120])

    # ── Step 1b: Sentinel 风控令牌 + 部署证明（统一链路，与建单同代理/同 device_id/同 cookie/同指纹）──
    sentinel_headers = _mint_checkout_risk_context(
        session.get, proxy, device_id, session_id, fp, promo_campaign_id,
        _session_cookie_header, timeout, log,
    )
    _attestation_value = str(sentinel_headers.get("oai-web-deployment-attestation") or "")

    log("checkout", "run", "建单（custom；请求体与手动 HAR 对齐；风控头=" + str(sorted(sentinel_headers.keys())) + "）")
    checkout_headers = _chatgpt_headers(
        access_token, "/backend-api/payments/checkout",
        referer="https://chatgpt.com/?promo_campaign=plus-1-month-free" if promo_campaign_id else "https://chatgpt.com/",
        device_id=device_id, session_id=session_id, fingerprint=fp,
        cookie_header=_session_cookie_header,
        extra=sentinel_headers,
    )
    try:
        r = session.post(_CHECKOUT_URL, json=checkout_body, headers=checkout_headers, timeout=timeout)
    except Exception as exc:
        # TLS 指纹目标不被当前 curl_cffi 支持（如 firefox136）：回退默认 chrome136 重建会话重试一次
        if "impersonat" in type(exc).__name__.lower() or "impersonat" in str(exc).lower():
            log("checkout", "run", "当前 curl_cffi 不支持账号指纹内核（" + str(exc)[:80] + "），回退默认 chrome136 重试")
            fallback_fp = _chrome_fallback_fingerprint(fp)
            session = _new_session(proxy, fingerprint=fallback_fp)
            fp = fallback_fp
            # 指纹变了 → 哨兵/attestation 必须与回退后的指纹重新对齐，否则身份分裂
            sentinel_headers = _mint_checkout_risk_context(
                session.get, proxy, device_id, session_id, fp, promo_campaign_id,
                _session_cookie_header, timeout, log,
            )
            _attestation_value = str(sentinel_headers.get("oai-web-deployment-attestation") or "")
            log("fingerprint", "run", "TLS 回退完成：统一切换到 " + str(fp.get("kernel") or "chrome") + "/" + str(fp.get("ua") or "")[-30:])
            try:
                r = session.post(_CHECKOUT_URL, json=checkout_body, headers=_chatgpt_headers(access_token, "/backend-api/payments/checkout", device_id=device_id, session_id=session_id, fingerprint=fp, cookie_header=_session_cookie_header, extra=sentinel_headers), timeout=timeout)
            except Exception as exc2:
                return _fail("checkout", "建单网络失败：" + type(exc2).__name__ + ": " + str(exc2)[:150], "wallet_transport_error", t0)
        else:
            return _fail("checkout", "建单网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
    if r.status_code >= 400:
        detail = _response_detail(r)
        error_extra = {
            "billing_country": cc,
            "upstream_status": int(r.status_code),
            "upstream_detail": detail,
        }
        if r.status_code == 401:
            error_extra["retryable"] = False
            return _fail(
                "checkout",
                "账号登录凭证已失效（HTTP 401），请重新获取 AT；系统未重试该请求。",
                "access_token_invalid_or_expired", t0, extra=error_extra,
            )
        if _is_unusual_activity(detail):
            # 用户要求保留 HTTP 400 的失败重试；调度层仍会使用指数退避和总次数上限。
            error_extra["retryable"] = True
            return _fail(
                "checkout",
                "上游异常活动风控拒绝了本次 GCash 建单；将按任务的失败重试策略再次尝试。",
                "checkout_risk_blocked", t0, extra=error_extra,
            )
        if "billing country must match request country" in detail.lower():
            error_extra["retryable"] = False
            return _fail(
                "checkout",
                "上游拒绝建单：账单国家与当前请求国家不一致。系统未自动重试。",
                "checkout_billing_mismatch", t0, extra=error_extra,
            )
        if r.status_code == 429 or "rate_limited" in detail.lower():
            return _fail("checkout", "建单被限流 HTTP 429: " + detail, "checkout_rate_limited", t0, extra=error_extra)
        # 400/403 保留原始详情，并交给调度层按既有失败重试策略重试。
        error_extra["retryable"] = True
        return _fail(
            "checkout",
            "建单请求被上游拒绝 HTTP " + str(r.status_code) + ": " + detail,
            "checkout_request_rejected", t0, extra=error_extra,
        )
    try:
        data = r.json() or {}
    except Exception:
        return _fail("checkout", "建单响应非 JSON", "checkout_failed", t0)
    cs = str(data.get("checkout_session_id") or data.get("session_id") or data.get("id") or "")
    if not cs:
        return _fail("checkout", "建单响应无会话 ID", "checkout_session_missing", t0)
    entity = str(data.get("processor_entity") or ("openai_llc" if cc == "US" else "openai_ie"))
    pk = str(data.get("publishable_key") or "")
    # 初始 checkout 可能只返回 card/link，GCash 会在 OAICS checkout state
    # 的嵌套 custom_payment_methods 中下发。与检测项目保持同一套读取顺序：
    # 先读初始响应，缺少 GCash 时再拉取 state，并合并两份数据以保留密钥。
    payment_types = _oaics.oaics_payment_method_types(data)
    cpm_raw = _oaics.oaics_custom_payment_methods(data)
    cpm_id = _gcash_custom_payment_method_id(cpm_raw)
    state_refresh_error = ""
    log("checkout", "run", "OAICS 初始方式：payment_method_types=" +
        json.dumps(payment_types, ensure_ascii=False)[:180] +
        " custom_payment_methods=" + json.dumps(cpm_raw, ensure_ascii=False)[:300])
    if not cpm_id and cs.lower().startswith("oaics_"):
        try:
            state = _oaics.fetch_oaics_checkout_state(
                proxy, access_token, session_token, cs, entity,
                country=cc, device_id=device_id, fingerprint=fp,
            )
            if isinstance(state, dict):
                merged = dict(data)
                merged.update(state)
                data = merged
                payment_types = _oaics.oaics_payment_method_types(state)
                cpm_raw = _oaics.oaics_custom_payment_methods(state)
                cpm_id = _gcash_custom_payment_method_id(cpm_raw)
                log("checkout", "ok", "OAICS state 已刷新：payment_method_types=" +
                    json.dumps(_oaics.oaics_payment_method_types(state), ensure_ascii=False)[:180] +
                    " custom_payment_methods=" + json.dumps(cpm_raw, ensure_ascii=False)[:300])
        except Exception as exc:
            state_refresh_error = type(exc).__name__ + ": " + str(exc)[:160]
            log("checkout", "warn", "OAICS state 刷新失败：" + type(exc).__name__ + ": " + str(exc)[:160])
    if not cpm_id:
        if _force_gcash_custom_method():
            cpm_id = _GCASH_CPMT_ID
            log("checkout", "warn",
                "Checkout 未下发 GCash custom_payment_method，已按 GCASH_FORCE_CPMT=1 强制使用已知 cpmt")
        else:
            return _fail("checkout",
                "账号没有 GCash 通道资格，或上游 Checkout 响应契约已变化："
                "未返回已知 GCash custom payment method。当前 custom_payment_methods="
                + json.dumps(cpm_raw, ensure_ascii=False)[:300],
                "wallet_method_unavailable", t0,
                extra={"checkout_session_id": cs, "payment_method_types": payment_types,
                       "custom_payment_methods": cpm_raw, "state_refresh_error": state_refresh_error,
                       "retryable": False})
    cuss = str(_deep_first(data, ("customer_session_client_secret", "customerSessionClientSecret", "client_secret")) or "")
    log("checkout", "ok", "建单成功 checkout_session_id=" + cs[:16] + "… GCash通道=" + cpm_id[:16] + "…")

    # ── 优惠资格检测：有资格自动应用优惠；无资格明确报告（不阻断原价提链）──
    promo_applied = False
    promo_error = ""
    promo_echoed = data.get("promo_campaign") if isinstance(data.get("promo_campaign"), dict) else None
    scheduled = data.get("scheduled_discount_preview")
    immediate = data.get("immediate_discount_settings")
    discount_offer = data.get("promo_credit_grant") or data.get("credit_discount_offer")
    if promo_campaign_id:
        nested_zero = _checkout_has_zero_discount(data)
        if promo_echoed or scheduled or immediate or discount_offer or nested_zero:
            promo_applied = True
            log("promotion", "ok", "优惠已自动应用（promo_campaign 回显" +
                ("，scheduled_discount=" + json.dumps(scheduled, ensure_ascii=False)[:120] if scheduled else "") +
                ("，immediate=" + json.dumps(immediate, ensure_ascii=False)[:120] if immediate else "") + "）")
        else:
            promo_error = "no_trial_eligibility"
            log("promotion", "warn", "上游未返回促销生效字段：top_promo=" +
                ("yes" if promo_echoed else "no") + " discount_offer=" +
                ("yes" if discount_offer else "no") + " nested_zero=" +
                ("yes" if nested_zero else "no"))
            if require_zero and not force_zero:
                # 默认要求 0 元（与 PayPal 一致）：当前代理出口无 0 元优惠资格 → 换代理重试
                log("promotion", "fail", "当前代理出口无优惠资格（促销未生效），将换代理重试")
                return _fail("promotion",
                    "当前代理出口无优惠资格（plus-1-month-free 促销未生效），无法产出 0 元链接。"
                    "将换代理重试；如需原价链接可在附加参数设置 requireZero=false",
                    "promo_not_applied", t0, extra={"checkout_session_id": cs, "retryable": True})
            if require_zero and force_zero:
                log("promotion", "warn", "未确认服务端 0 元，按 GCASH_FORCE_ZERO=1 继续调用 confirm/start（实际金额以服务端为准）")
            log("promotion", "fail",
                "账号无优惠资格（建单响应 promo_campaign 为空/未回显），继续按原价提链；"
                "如需 0 元需使用有 plus-1-month-free 资格的账号")
    else:
        if require_zero:
            return _fail("promotion",
                "未启用促销（promo_campaign_id 为空）且要求 0 元，无法产出 0 元链接。"
                "请在附加参数设置 requireZero=false 输出原价链接",
                "promo_not_applied", t0, extra={"checkout_session_id": cs, "retryable": False})
        log("promotion", "run", "未启用促销（promo_campaign_id 为空），按原价提链")
    if not pk:
        return _fail("checkout", "建单响应缺少 publishable_key", "checkout_publishable_key_missing", t0)
    if not cuss:
        return _fail("checkout", "建单响应缺少 customer_session_client_secret", "no_secret", t0)

    # ── Step 2b: 提交 PH 账单地址（POST /backend-api/payments/checkout/taxes）──
    # 对齐 freepp S2 段：用真实公开 PH 地址（酒店库）提交，避免假地址导致拒付/风控；
    # 同账号同次提链内账单保持一致（seed=账号名+device）。
    _taxes_url = "https://chatgpt.com/backend-api/payments/checkout/taxes"
    _billing = {}
    try:
        from ph_billing import generate_ph_billing as _gen_billing
        # 每次提链（含重试）都重新随机一套地址：seed=None → 全随机；
        # 环境变量 MIN_GCASH_BILLING_ADDRESS 注入时仍保持优先；
        # 同时用建单代理探测出口城市（可选，失败静默），账单优先同城 → 更真实。
        _geo_city = str(fp.get("proxy_city") or "")
        if not _geo_city:
            try:
                from ph_billing import probe_exit_city as _probe_city
                _geo_city = _probe_city(proxy, timeout=min(max(timeout, 5.0), 8.0))
            except Exception:
                _geo_city = ""
        _billing = _gen_billing(
            str(fp.get("account_email") or ""),
            country=cc,
            currency=cur,
            seed=None,
            geo_city=_geo_city,
        )
    except Exception as exc:
        # 签名/数据失败不阻断：用最简结构（引擎仍需提交，空字段由上游校验）
        log("taxes", "warn", "PH 账单生成器异常（使用最小账单）：" + type(exc).__name__ + "：" + str(exc)[:100])
        _billing = {
            "name": "Jose Santos",
            "email": str(fp.get("account_email") or "") or "buyer@example.com",
            "phone": "",
            "tax_id": "",
            "address": {"country": cc, "line1": "One Rizal Park, Ermita", "line2": "", "city": "Manila", "state": "Metro Manila", "postal_code": "0913"},
            "_place_name": "The Manila Hotel",
            "_source": "fallback",
        }
    _taxes_body = {
        "checkout_session_id": cs,
        "checkout_email": str(_billing.get("email") or ""),
        "billing_country": cc,
        "billing_name": str(_billing.get("name") or ""),
        "currency": cur,
        "tax_id": str(_billing.get("tax_id") or "") or None,
        "processor_entity": entity,
        "billing_address": {
            "country": cc,
            "line1": str(_billing["address"].get("line1") or ""),
            "line2": str(_billing["address"].get("line2") or ""),
            "city": str(_billing["address"].get("city") or ""),
            "state": str(_billing["address"].get("state") or ""),
            "postal_code": str(_billing["address"].get("postal_code") or ""),
        },
    }
    log("taxes", "run", "提交 PH 账单（" + str(_billing["address"]["line1"]) + "，" + str(_billing["address"]["city"]) + " " + str(_billing["address"]["postal_code"]) + "）")
    try:
        tx = session.post(
            _taxes_url,
            json=_taxes_body,
            headers=_chatgpt_headers(
                access_token, "/backend-api/payments/checkout/taxes",
                referer="https://chatgpt.com/checkout/" + entity + "/" + cs,
                device_id=device_id, session_id=session_id, fingerprint=fp,
                cookie_header=_session_cookie_header,
                extra={"oai-web-deployment-attestation": _attestation_value} if _attestation_value else None,
            ),
            timeout=timeout,
        )
    except Exception as exc:
        return _fail("taxes", "PH 账单提交网络失败：" + type(exc).__name__ + "：" + str(exc)[:150], "wallet_transport_error", t0)
    if tx.status_code >= 400:
        # 账单被拒 → 直接失败，不进入 cpmt（避免后续拒付）
        return _fail("taxes", "PH 账单提交被上游拒绝 HTTP " + str(tx.status_code) + "：" + (tx.text or "")[:300], "checkout_request_rejected", t0,
                     extra={"retryable": True, "upstream_status": int(tx.status_code)})
    log("taxes", "ok", "PH 账单已接受（地址来源=" + str(_billing.get("_source")) + "）")

    # ── Step 2 省略：GCash 为 custom_payment_method（cpmt_）直接提链 ──
    # freepp 生产链路不创建 Stripe elements/sessions / PaymentMethod / confirmation_token；
    # 那些仅用于标准 paypal/card 路径。GCash 直接走 Step 4/5 的
    # custom_payment_method(start) → custom_payment_method/continue → redirect。
    # （保留 publishable_key / customer_session_client_secret 备查，不再向 Stripe 发请求）

    # ── Step 4/5: GCash 自定义支付方式确认+提链（对齐 pay153 生产链路，★ 关键）──
    # pay153 GCash（已验证可绕过 blk）链路：
    #   POST /checkout/confirm  body={"checkout_session_id","selected_payment_method_type": cpmt}
    #   POST /checkout/custom_payment_method/start  body={"checkout_session_id","custom_payment_method_type_id": cpmt}
    #   → start 响应 status=requires_action + next_action.url = Adyen 跳转 → 直接产出链接；
    #   blocked 时重领 SEN/SO 立即重试一次。
    _risk_cache: dict = {"sentinel": "", "so": "", "attestation": _attestation_value if _attestation_value else ""}
    adyen_url = ""
    for _cpmt_attempt in range(2):
        cpmt_risk = _approval_risk_headers(
            proxy, device_id, session_id, fp, _session_cookie_header,
            access_token=access_token, entity=entity, checkout_session_id=cs,
            timeout=timeout, emit=log, sentinel_cache=_risk_cache,
        )
        _obs = {**cpmt_risk, "x-oai-is-client-observation": "true"} if cpmt_risk else {"x-oai-is-client-observation": "true"}

        # ── 4a: confirm（POST /backend-api/payments/checkout/confirm）──
        confirm_headers = _chatgpt_headers(access_token, "/backend-api/payments/checkout/confirm",
                                           referer="https://chatgpt.com/checkout/" + entity + "/" + cs,
                                           device_id=device_id, session_id=session_id, fingerprint=fp,
                                           cookie_header=_session_cookie_header, extra=_obs)
        confirm_body = {
            "checkout_session_id": cs,
            "selected_payment_method_type": cpm_id,
        }
        try:
            cr = session.post(_CONFIRM_URL, json=confirm_body, headers=confirm_headers, timeout=timeout)
        except Exception as exc:
            return _fail("confirm", "OpenAI confirm 网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
        if cr.status_code >= 400:
            return _fail("confirm", "OpenAI confirm HTTP " + str(cr.status_code) + ": " + cr.text[:250], "confirm_failed", t0)
        confirm_data = cr.json() or {}
        cstatus = str(confirm_data.get("status") or "").strip().lower()
        if cstatus == "blocked":
            if _cpmt_attempt == 0:
                log("confirm", "run", "confirm 首次被拦截（blocked），重领 SEN/SO 立即重试…")
                _risk_cache = {"sentinel": "", "so": "", "attestation": _attestation_value if _attestation_value else ""}
                continue
            return _fail("confirm",
                "OpenAI confirm blocked（更新 SEN/SO 后仍被拦截，账号级风控）",
                "confirm_blocked", t0, extra={"checkout_session_id": cs, "upstream_detail": json.dumps(confirm_data, ensure_ascii=False)[:300]})
        if cstatus not in {"success", "ok", ""}:
            return _fail("confirm", "OpenAI confirm status=" + cstatus + "：" + json.dumps(confirm_data, ensure_ascii=False)[:250], "confirm_failed", t0)
        log("confirm", "ok", "OpenAI confirm success")

        # ── 4b: start（POST /backend-api/payments/checkout/custom_payment_method/start）──
        start_headers = _chatgpt_headers(access_token, "/backend-api/payments/checkout/custom_payment_method/start",
                                         referer="https://chatgpt.com/checkout/" + entity + "/" + cs,
                                         device_id=device_id, session_id=session_id, fingerprint=fp,
                                         cookie_header=_session_cookie_header, extra=_obs)
        start_body = {
            "checkout_session_id": cs,
            "custom_payment_method_type_id": cpm_id,
        }
        try:
            sr = session.post(_CPMT_START_URL, json=start_body, headers=start_headers, timeout=timeout)
        except Exception as exc:
            return _fail("extract_link", "custom_payment_method/start 网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
        if sr.status_code >= 400:
            return _fail("extract_link", "custom_payment_method/start HTTP " + str(sr.status_code) + ": " + sr.text[:300], "start_failed", t0)
        start_data = sr.json() or {}
        sstatus = str(start_data.get("status") or "").strip().lower()
        text_all = json.dumps(start_data, ensure_ascii=False, default=str)
        if sstatus == "blocked":
            if _cpmt_attempt == 0:
                log("extract_link", "run", "start 首次被拦截（blocked），重领 SEN/SO 立即重试…")
                _risk_cache = {"sentinel": "", "so": "", "attestation": _attestation_value if _attestation_value else ""}
                continue
            return _fail("extract_link",
                "OpenAI confirm blocked（start 更新 SEN/SO 后仍被拦截，账号级风控）",
                "confirm_blocked", t0, extra={"checkout_session_id": cs, "upstream_detail": text_all[:300]})
        # 提取跳转：next_action.url（requires_action）优先，其次正则搜 Adyen/GCash URL
        next_action = start_data.get("next_action") if isinstance(start_data.get("next_action"), dict) else {}
        adyen_url = str(next_action.get("url") or "")
        if not adyen_url:
            m = _ADYEN_REDIRECT_RE.search(text_all)
            adyen_url = m.group(0) if m else ""
        if not adyen_url:
            m = re.search(r"https?://[^\s\"'<>]*(?:adyen|gcash)[^\s\"'<>]*", text_all)
            adyen_url = m.group(0) if m else ""
        if adyen_url:
            log("extract_link", "ok", "start 返回跳转 URL：" + adyen_url[:90] + "…（status=" + (sstatus or "requires_action") + "）")
            break
        if _cpmt_attempt == 0:
            log("extract_link", "run", "start 未返回跳转地址（status=" + (sstatus or "?") + "），重试一次")
            continue
        return _fail("extract_link", "custom_payment_method/start 未返回跳转地址（status=" + (sstatus or "?") + "），响应: " + text_all[:250], "no_redirect", t0, extra={"checkout_session_id": cs})
    if not adyen_url:
        return _fail("extract_link", "cpmt 提链未取得 Adyen 跳转地址", "no_redirect", t0, extra={"checkout_session_id": cs})

    # ── Step 6: follow Adyen → m.gcash.com 授权页 ──
    current = adyen_url
    final_url = ""
    final_body_text = ""
    qr_image = ""
    for _hop in range(8):
        try:
            fr = session.get(current, timeout=timeout, allow_redirects=False)
        except Exception:
            break
        loc = str(fr.headers.get("Location") or "")
        if loc:
            current = urljoin(current, loc)
            continue
        body_text = fr.text or ""
        # 1) 页面/URL 中直接命中授权页
        m = _GCASH_AUTH_PAGE_RE.search(body_text)
        if m:
            final_url = m.group(0)
            final_body_text = body_text
            break
        m = _GCASH_AUTH_PAGE_RE.search(current)
        if m:
            final_url = current
            final_body_text = body_text
            break
        # 2) meta refresh / JS location 跳转
        meta = _META_REFRESH_RE.search(body_text)
        if meta:
            target = urljoin(current, meta.group(1).strip())
            if "gcash" in target or "adyen" in target:
                current = target
                continue
        # 3) 当前已是 m.gcash.com（排除静态资源）
        if "m.gcash.com" in current:
            if not re.search(r"\.(js|css|png|jpg|svg|ico|woff2?)(\?|$)", current):
                final_url = current
                final_body_text = body_text
                break
            # 静态资源页 → 向前跳一步找真正页面
            final_url = current
            final_body_text = body_text
            break
        # 4) 页面 200 且含 m.gcash.com 授权 URL（排除静态资源）
        any_m = _GCASH_ANY_RE.search(body_text)
        if any_m:
            candidate = any_m.group(0)
            if not re.search(r"\.(js|css|png|jpg|svg|ico|woff2?)(\?|$)", candidate):
                final_url = candidate
                final_body_text = body_text
                break
        final_url = current
        final_body_text = body_text
        break
    if not final_url or "gcash" not in final_url.lower():
        final_url = adyen_url  # 兜底：Adyen URL 本身可交付（浏览器打开自动跳转 GCash）
    if final_url:
        # 只有确定最终页面后才进入无限等待二维码分支。此前在 Adyen 中间页
        # 就启动浏览器扫描，会让跳转解析永远无法完成。
        log("resolve", "run", "链接跳转解析完成，开始在最终 GCash 页面获取二维码")
        if callable(on_qr_update):
            # 生成二维码和等待 GCash→Adyen→ChatGPT 返回必须是同一个
            # BrowserContext，不能关闭首个浏览器再新建监听会话。
            # wait_for_first_image=False：链接结果立即返回，二维码在后台线程继续抓取，
            # 抓到后通过 on_image 回调（qr_refresh 事件）实时推给前端展示。
            qr_image = _start_qr_refresh_monitor(
                final_url,
                session,
                proxy,
                timeout,
                referer=adyen_url,
                fingerprint=fp,
                emit=log,
                on_image=on_qr_update,
                refresh_event=qr_refresh_event,
                access_token=access_token,
                on_paid=on_paid,
                checkout_session_id=cs,
                device_id=device_id,
                session_id=session_id,
                wait_for_first_image=False,
            )
        else:
            qr_image = _extract_qr_image(
                final_body_text,
                final_url,
                session,
                proxy,
                timeout,
                referer=adyen_url,
                fingerprint=fp,
                emit=log,
                browser_url=final_url,
                access_token=access_token,
            )
    log("resolve", "ok", "GCash 授权链接：" + final_url[:110] + "…")
    if qr_image:
        log("resolve", "ok", "GCash 二维码图片已获取（仅显示图片，不读取二维码内容）")
    elif callable(on_qr_update):
        log("resolve", "run", "二维码仍在后台生成中（浏览器会话保持），链接已先行返回；前端收到 qr_refresh 事件后展示二维码")
    else:
        log("resolve", "ok", "二维码未获取，继续返回授权链接")
    # 无论二维码当前是否已抓到：链接一定返回成功（用户可复制链接）；
    # 刷新模式下后台线程继续等待，抓到二维码后通过 on_image(qr_refresh) 事件推给前端。
    return {
        "ok": True,
        "link": final_url,
        "adyen_url": adyen_url,
        "qr_image": qr_image,
        "error": "",
        "reason_code": "",
        "extra": {
            "payment_method": "gcash",
            "link_type": "gcash_protocol",
            "checkout_session_id": cs,
            "billing_country": cc,
            "currency": cur,
            "checkout_session_type": ("oaics" if str(cs).startswith("oaics_")
                                     else "cs_live" if str(cs).startswith("cs_live_")
                                     else "custom"),
            "amount_minor": None,
            "promotion_applied": promo_applied,
            "promotion_error": promo_error,
            "elapsed_seconds": round(time.monotonic() - t0, 1),
            "qr_image": qr_image,
            "qr_refresh_active": bool(callable(on_qr_update)),
            "qr_pending": bool(callable(on_qr_update) and not qr_image),
            "paid": False,
            "paid_note": "等待扫码支付（同窗口同登录态，支付回调在本窗口完成）",
        },
    }
