# -*- coding: utf-8 -*-
"""GoPay custom 流程 —— 纯协议 Stripe 标准 PM 链路（对齐真实浏览器 HAR）。

GoPay 与 GCash 的底层不同：GCash 走 custom_payment_method(cpmt) + Adyen；
GoPay 走 Stripe 标准 PaymentMethod + pm-redirects + Midtrans。本模块严格按浏览器
HAR 的请求序列实现（不抓二维码、不启动 Playwright）：

  Step 0  pricing_config   GET  /backend-api/checkout_pricing_config/configs/ID
  Step 1  checkout         POST /backend-api/payments/checkout（plus + promo 压0）
  Step 2  stripe_init      POST api.stripe.com/v1/payment_pages/{cs}/init
                            → init_checksum / payment_method_types(gopay) / amount
  Step 3  taxes            POST /backend-api/payments/checkout/taxes（印尼账单）
  Step 4  snapshot         POST /backend-api/payments/checkout/snapshot
  Step 5  confirm          POST api.stripe.com/v1/payment_pages/{cs}/confirm
                            （内嵌 payment_method_data[type]=gopay）
  Step 6  approve          POST /backend-api/payments/checkout/approve（result=approved）
  Step 7  poll             GET  api.stripe.com/v1/payment_pages/{cs}
                            → url 字段出现 pm-redirects.stripe.com/authorize/...
  Step 8  follow           pm-redirects → app.midtrans.com/snap/v4/redirection/<uuid>

风控上下文：openai-sentinel-token + oai-web-deployment-attestation（对齐 HAR 成功链路），
失败时自动降级为无风控证明直连。国家/货币/账单硬锁印尼（ID / IDR）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from typing import Callable, Optional

from sms_tool.paypal_proxy import normalize_proxy_url  # noqa: E402
from sentinel_token import mint_sentinel_sync, sentinel_header_value  # noqa: E402
from attestation import fetch_attestation  # noqa: E402

# 跳转链识别
_PM_REDIRECT_RE = re.compile(r"https?://pm-redirects\.stripe\.com/authorize/[^\s\"'<>]+")
_MIDTRANS_REDIRECT_RE = re.compile(
    r"https?://app\.midtrans\.com/snap/v[34]/redirection/[0-9a-fA-F-]{36}"
)
_MIDTRANS_ANY_RE = re.compile(r"https?://app\.midtrans\.com/[^\s\"'<>]+")
_META_REFRESH_RE = re.compile(r"(?i)url\s*=\s*['\"]?([^'\"\s>]+)")

_PRICING_CONFIG_URL = "https://chatgpt.com/backend-api/checkout_pricing_config/configs"
_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
_TAXES_URL = "https://chatgpt.com/backend-api/payments/checkout/taxes"
_SNAPSHOT_URL = "https://chatgpt.com/backend-api/payments/checkout/snapshot"
_APPROVE_URL = "https://chatgpt.com/backend-api/payments/checkout/approve"
_STRIPE_PAGE_URL = "https://api.stripe.com/v1/payment_pages/{checkout_session_id}"

_CHATGPT_CLIENT_VERSION = "prod-180ca8b8699a733aef330b7026892aee9bf85fbe"
_CHATGPT_CLIENT_BUILD = "9758774"


def _alternate_proxy_scheme(proxy: str) -> str:
    """Return a one-shot HTTP/SOCKS5 scheme fallback for gateways that expose both."""
    try:
        parsed = urlsplit(str(proxy or ""))
        scheme = (parsed.scheme or "").lower()
        if scheme in ("socks5", "socks5h"):
            target = "http"
        elif scheme in ("http", "https"):
            target = "socks5h"
        else:
            return ""
        return urlunsplit((target, parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return ""
_CHATGPT_SEC_CH_UA = '"Google Chrome";v="136", "Not.A/Brand";v="8", "Chromium";v="136"'
_CHATGPT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
_STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
# 对齐最新成功 HAR（gopay.har 2026-08-29）的 Stripe.js 运行时 hash。
_STRIPE_RUNTIME_VERSION = "b0f5e7abe5"
_OAICS_GOPAY_CUSTOM_METHOD_ID = "gopay"


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
    cookie_header: str = "",
    extra: Optional[dict] = None,
):
    """OpenAI 前端请求头（对齐 HAR 浏览器上下文）。"""
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    ua_value = str(fp.get("ua") or _CHATGPT_UA)
    sec_ch_ua = str(fp.get("sec_ch_ua") or "").strip()
    if not sec_ch_ua:
        m = re.search(r"Chrome/(\d+)\.", ua_value)
        if m:
            ver = m.group(1)
            sec_ch_ua = '\"Google Chrome\";v=\"' + ver + '\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"' + ver + '\"'
        else:
            sec_ch_ua = _CHATGPT_SEC_CH_UA
    stable_device_id = device_id or str(uuid.uuid4())
    headers = {
        "User-Agent": ua_value,
        "Accept": "*/*",
        "Accept-Language": str(fp.get("accept_language") or "id-ID,id;q=0.9,en;q=0.8"),
        "Authorization": "Bearer " + str(access_token or ""),
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "Content-Type": "application/json",
        "oai-device-id": stable_device_id,
        "oai-language": str(fp.get("oai_language") or "id-ID"),
        "oai-session-id": session_id or str(uuid.uuid4()),
        "oai-client-version": _CHATGPT_CLIENT_VERSION,
        "oai-client-build-number": _CHATGPT_CLIENT_BUILD,
        "Cookie": cookie_header or ("oai-did=" + stable_device_id),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if sec_ch_ua:
        headers["sec-ch-ua"] = sec_ch_ua
        headers["sec-ch-ua-mobile"] = str(fp.get("sec_ch_ua_mobile") or "?0")
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
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    if attestation:
        headers["oai-web-deployment-attestation"] = attestation
    if client_observation:
        headers["x-oai-is-client-observation"] = client_observation
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


def _stripe_headers(checkout_session_id: str, fingerprint: Optional[dict] = None) -> dict:
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": str(fp.get("ua") or _CHATGPT_UA),
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
    fp = dict(fingerprint) if isinstance(fingerprint, dict) else {}
    fp.update({
        "impersonate": "chrome136",
        "kernel": "chrome",
        "kernel_label": "Chrome",
        "ua": _CHATGPT_UA,
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


def _jwt_account_id(access_token: str) -> str:
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


def _sentinel_fingerprint_kwargs(fp: Optional[dict]) -> dict:
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
    """统一为 checkout 生成风控上下文建单头（哨兵 + attestation）。"""
    sentinel_headers: dict = {}
    try:
        _sn = mint_sentinel_sync(
            flow="chatgpt_checkout",
            device_id=device_id,
            user_agent=str(fp.get("ua") or _CHATGPT_UA),
            proxy=proxy,
            cores=int(fp.get("hardware_concurrency") or 16),
            page_url="https://chatgpt.com/?promo_campaign=plus-1-month-free" if promo_campaign_id else "https://chatgpt.com/",
            language=str(fp.get("oai_language") or "id-ID"),
            timezone=str(fp.get("timezone") or "Asia/Jakarta"),
            cookie_header=cookie_header,
            timeout_s=float(max(timeout, 60) or 60),
            emit=log,
            **_sentinel_fingerprint_kwargs(fp),
        )
        _sv = sentinel_header_value(_sn)
        if not _sv:
            alternate = _alternate_proxy_scheme(proxy)
            if alternate:
                log("sentinel", "run", "当前代理协议未生成风控令牌，使用备用协议重试一次")
                _sn = mint_sentinel_sync(
                    flow="chatgpt_checkout",
                    device_id=device_id,
                    user_agent=str(fp.get("ua") or _CHATGPT_UA),
                    proxy=alternate,
                    cores=int(fp.get("hardware_concurrency") or 16),
                    page_url="https://chatgpt.com/?promo_campaign=plus-1-month-free" if promo_campaign_id else "https://chatgpt.com/",
                    language=str(fp.get("oai_language") or "id-ID"),
                    timezone=str(fp.get("timezone") or "Asia/Jakarta"),
                    cookie_header=cookie_header,
                    timeout_s=float(max(timeout, 60) or 60),
                    emit=log,
                    **_sentinel_fingerprint_kwargs(fp),
                )
                _sv = sentinel_header_value(_sn)
        if _sv:
            sentinel_headers["openai-sentinel-token"] = _sv
            log("sentinel", "ok", "建单风控令牌已写入请求头")
        else:
            log("sentinel", "fail", "两种代理协议均未生成建单风控令牌")
    except Exception as exc:
        log("sentinel", "warn", "哨兵生成异常（按无哨兵降级）：" + type(exc).__name__ + "：" + str(exc)[:150])
    try:
        _av = fetch_attestation(
            session_get,
            url="https://chatgpt.com/?promo_campaign=plus-1-month-free" if promo_campaign_id else "https://chatgpt.com/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": str(fp.get("accept_language") or "id-ID,id;q=0.9,en;q=0.8"),
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
    """approve 阶段所需的风控头（flow=checkout_session_approval）。"""
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
        from sentinel_token import so_header_value as _sohv
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
        language=str(fp.get("oai_language") or "id-ID"),
        timezone=str(fp.get("timezone") or "Asia/Jakarta"),
        cookie_header=cookie_header,
        timeout_s=float(max(timeout, 60) or 60),
        emit=log,
        **_sentinel_fingerprint_kwargs(fp),
    )
    value = sentinel_header_value(token)
    if value:
        out["openai-sentinel-token"] = value
        if sentinel_cache is not None:
            sentinel_cache["sentinel"] = value
    so_value = _sohv(token.get("_so") if isinstance(token, dict) else None)
    if so_value:
        out["OpenAI-Sentinel-SO-Token"] = so_value
        if sentinel_cache is not None:
            sentinel_cache["so"] = so_value
    if sentinel_cache is not None and sentinel_cache.get("attestation"):
        out["oai-web-deployment-attestation"] = sentinel_cache["attestation"]
    return out


def _gopay_billing_reverse_enabled() -> bool:
    """是否按代理出口坐标反查账单（默认开启；设为 0/false 退回内置街道表）。"""
    return str(os.environ.get("GOPAY_BILLING_REVERSE") or "1").strip().lower() not in {"0", "false", "no", "off"}


def _reverse_gopay_billing(fingerprint: Optional[dict], country: str) -> dict:
    """按代理出口坐标反查一条印尼账单；失败返回空字典让调用方回退内置街道表。

    只在出口国家与账单国家一致时才反查：出口不在印尼却用印尼地址，等于把账单
    国家和出口位置割裂开。反查结果只提供地址，姓名仍由本地姓名库生成。
    """
    fp = fingerprint or {}
    if not _gopay_billing_reverse_enabled():
        return {}
    lat = str(fp.get("proxy_latitude") or "").strip()
    lon = str(fp.get("proxy_longitude") or "").strip()
    if not lat or not lon:
        return {}
    if str(fp.get("proxy_country") or "").strip().upper() != str(country or "").strip().upper():
        return {}
    try:
        from id_billing import resolve_reverse_address
        resolved = resolve_reverse_address(lat, lon, str(country or "").strip().upper())
    except Exception:
        return {}
    return resolved if isinstance(resolved, dict) else {}


def _fail(stage: str, message: str, reason_code: str, t0: float, extra: Optional[dict] = None) -> dict:
    out = {
        "ok": False, "link": "", "error": message, "reason_code": reason_code,
        "extra": {"payment_method": "gopay", "link_type": "", "error_stage": stage,
                  "retryable": reason_code in ("checkout_rate_limited", "wallet_transport_error", "no_redirect", "start_failed", "approve_failed",
                                              "promo_not_applied", "wallet_checkout_not_zero_due", "wallet_method_unavailable_env"),
                  "elapsed_seconds": round(time.monotonic() - t0, 1)},
    }
    if extra:
        out["extra"].update(extra)
    return out


def _gopay_return_url(init_payload: dict, checkout_session_id: str) -> str:
    hosted = str(init_payload.get("stripe_hosted_url") or "")
    if hosted.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + hosted[len("https://checkout.stripe.com"):]
    if hosted.startswith("https://pay.openai.com"):
        return hosted
    return (
        "https://pay.openai.com/c/pay/" + checkout_session_id
        + "?returned_from_redirect=true&ui_mode=custom"
    )


def _amount_minor_from_init(init_payload: dict) -> Optional[int]:
    """从 Stripe init 响应提取实付金额（minor units），优先 total_summary.due。"""
    total = init_payload.get("total_summary") if isinstance(init_payload, dict) else None
    if isinstance(total, dict) and total.get("due") is not None:
        try:
            return int(total["due"])
        except (TypeError, ValueError):
            pass
    invoice = init_payload.get("invoice") if isinstance(init_payload, dict) else None
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        try:
            return int(invoice["amount_due"])
        except (TypeError, ValueError):
            pass
    return None


def _payload_currency(payload: object) -> str:
    """Extract a currency code without retaining the upstream response."""
    if isinstance(payload, dict):
        for key in ("currency", "checkout_currency"):
            value = str(payload.get(key) or "").strip().upper()
            if value:
                return value
        for value in payload.values():
            found = _payload_currency(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _payload_currency(value)
            if found:
                return found
    return ""


def _payload_payment_methods(payload: object) -> set[str]:
    """Collect payment method types for a bounded consistency check."""
    found: set[str] = set()
    if isinstance(payload, dict):
        for key in ("payment_method_types", "ordered_payment_method_types", "ordered_payment_method_types_and_wallets"):
            value = payload.get(key)
            if isinstance(value, list):
                found.update(str(item).strip().lower() for item in value if str(item).strip())
        for value in payload.values():
            found.update(_payload_payment_methods(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(_payload_payment_methods(value))
    return found


def _payment_failure_summary(submission_error: object) -> dict[str, str]:
    """Return only safe Stripe payment-error fields, never the full payload."""
    result = {"code": "", "decline_code": "", "message": ""}
    if not isinstance(submission_error, dict):
        return result
    holders = [submission_error]
    nested = submission_error.get("payment_error")
    if isinstance(nested, dict):
        holders.insert(0, nested)
    for holder in holders:
        for key in result:
            if not result[key]:
                result[key] = str(holder.get(key) or "").strip()[:240]
    if not result["code"]:
        result["code"] = str(submission_error.get("type") or "").strip()[:240]
    return result


def _extract_gopay_redirect(payload: object) -> str:
    """Find a GoPay handoff URL in an OAICS custom-payment response."""
    try:
        text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    except Exception:
        text = str(payload or "")
    match = _PM_REDIRECT_RE.search(text) or _MIDTRANS_REDIRECT_RE.search(text) or _MIDTRANS_ANY_RE.search(text)
    return match.group(0) if match else ""


def _start_oaics_gopay(
    session, access_token: str, checkout_session_id: str, processor_entity: str,
    *, device_id: str, session_id: str, fingerprint: dict, cookie_header: str,
    sentinel_headers: dict, timeout: float, gopay_cpmt_id: str = "",
) -> tuple[dict, str]:
    """Start GoPay on an OAICS checkout without touching Stripe payment_pages.

    OAICS sessions do not expose ``payment_pages/{id}/init`` (it returns 404).
    Their custom-payment endpoint is the source of truth and must be started
    with GoPay and a zero-due expectation.
    """
    path = "/backend-api/payments/checkout/custom_payment_method/start"
    # OAICS custom sessions do not use Stripe's standard payment method IDs.
    # Keep this value fixed: accepting a caller-supplied cpmt ID can turn the
    # request into an unsupported method and is the source of intermittent
    # "payment method not selected"/400 responses.
    body = {
        "checkout_session_id": checkout_session_id,
        "processor_entity": processor_entity,
        "payment_method_type": "gopay",
        "custom_payment_method_type_id": _OAICS_GOPAY_CUSTOM_METHOD_ID,
        "amount": 0,
        "amount_minor": 0,
        "expected_amount": 0,
        "currency": "idr",
    }
    headers = _chatgpt_headers(
        access_token, path,
        referer=f"https://chatgpt.com/checkout/{processor_entity}/{checkout_session_id}",
        device_id=device_id, session_id=session_id, fingerprint=fingerprint,
        cookie_header=cookie_header, extra=sentinel_headers,
    )
    response = session.post("https://chatgpt.com" + path, json=body, headers=headers, timeout=timeout)
    try:
        payload = response.json() if response.text else {}
    except Exception:
        payload = {"raw": (response.text or "")[:500]}
    if response.status_code >= 400:
        raise RuntimeError(f"custom_payment_method/start HTTP {response.status_code}: {(response.text or '')[:250]}")
    # Never allow an OAICS response to silently continue at full price.
    due = _amount_minor_from_init(payload if isinstance(payload, dict) else {})
    if due not in (None, 0):
        raise RuntimeError(f"custom_payment_method/start returned non-zero amount: {due}")
    return (payload if isinstance(payload, dict) else {"raw": str(payload)}), _extract_gopay_redirect(payload)


def extract_gopay_custom_link(
    access_token: str,
    proxy: str,
    country: str = "ID",
    currency: str = "IDR",
    promo_campaign_id: str = "plus-1-month-free",
    require_zero: bool = True,
    emit: Optional[Callable[[str, str, str], None]] = None,
    timeout: float = 45.0,
    fingerprint: Optional[dict] = None,
    sticky_proxy: bool = False,
    gopay_cpmt_id: str = "",
    # 下面两个参数为「兼容 gopay_service.py 的调用签名」而保留。
    # 该调用方无条件下传它们，而本函数此前没有这两个形参，导致每次调用都抛
    #   TypeError: extract_gopay_custom_link() got an unexpected keyword argument
    # 且异常未被捕获 → 进程直接退出 → 上层只看到「引擎无 JSON 输出」。
    # custom 流程本身不使用这两个开关：0 元约束由 require_zero 控制。
    force_payment_method: bool = False,
    force_zero_checkout: bool = False,
) -> dict:
    """GoPay custom 完整流程（Stripe 标准 PM），返回 Midtrans / pm-redirects 授权链接。

    gopay_cpmt_id 为兼容 gopay_service.py 调用保留；GoPay 不走 cpmt 路径，忽略该参数。
    force_payment_method / force_zero_checkout 同样仅为兼容调用签名保留，本流程不使用。
    """
    t0 = time.monotonic()
    log = emit or (lambda stage, status, msg: None)
    requested_cc = str(country or "ID").strip().upper()
    requested_cur = str(currency or "IDR").strip().upper()
    # 用户可自选国家和币种；未指定时默认 ID / IDR。
    cc = requested_cc or "ID"
    cur = requested_cur or "IDR"
    proxy = normalize_proxy_url(proxy)
    if not proxy:
        return _fail("validation", "未提供用户代理，GoPay 提链必须使用代理池中的代理", "proxy_missing", t0)
    fp = fingerprint if isinstance(fingerprint, dict) else {}
    session = _new_session(proxy, fingerprint=fp)
    device_id = str(fp.get("device_id") or uuid.uuid4())
    session_id = str(fp.get("oai_session_id") or str(uuid.uuid4()))
    if sticky_proxy:
        log("config", "run", "粘性代理已启用：建单/出口 IP 全程固定（不轮换）")
    if fp:
        log("fingerprint", "run",
            "账号指纹 " + str(fp.get("kernel_label") or fp.get("kernel") or "chrome") + "/" + str(device_id)[:8]
            + " TLS=" + str(fp.get("impersonate") or "?") + " 语言=" + str(fp.get("oai_language") or "?")
            + " 时区=" + str(fp.get("timezone") or "?") + " 建单国=" + str(fp.get("country") or cc))

    # ── Step 0: 定价配置 ──
    pricing_path = "/backend-api/checkout_pricing_config/configs/" + cc
    log("pricing_config", "run", "读取 " + cc + " 定价配置（对齐浏览器 HAR）")
    try:
        pricing = session.get(
            _PRICING_CONFIG_URL + "/" + cc,
            headers=_chatgpt_headers(access_token, pricing_path, device_id=device_id, session_id=session_id, fingerprint=fp),
            timeout=timeout,
        )
    except Exception as exc:
        alternate = _alternate_proxy_scheme(proxy)
        if alternate:
            try:
                alt_session = _new_session(alternate, fingerprint=fp)
                pricing = alt_session.get(
                    _PRICING_CONFIG_URL + "/" + cc,
                    headers=_chatgpt_headers(access_token, pricing_path, device_id=device_id, session_id=session_id, fingerprint=fp),
                    timeout=timeout,
                )
                session = alt_session
                proxy = alternate
                log("pricing_config", "run", "原协议连接失败，已使用备用代理协议重试一次")
            except Exception:
                return _fail("pricing_config", "定价配置网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
        else:
            return _fail("pricing_config", "定价配置网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
    if pricing.status_code >= 400:
        detail = _response_detail(pricing)
        return _fail("pricing_config", "定价配置被上游拒绝 HTTP " + str(pricing.status_code) + ": " + detail,
                     "checkout_request_rejected", t0,
                     extra={"retryable": True, "upstream_status": int(pricing.status_code), "upstream_detail": detail})
    log("pricing_config", "ok", "ID 定价配置已读取")

    # ── Step 1: 创建 Checkout 会话（plus + promo 压0）──
    checkout_body = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": cc, "currency": cur},
        "checkout_ui_mode": "custom",
    }
    if promo_campaign_id:
        checkout_body["promo_campaign"] = {"promo_campaign_id": promo_campaign_id, "is_coupon_from_query_param": False}
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

    sentinel_headers = _mint_checkout_risk_context(
        session.get, proxy, device_id, session_id, fp, promo_campaign_id,
        _session_cookie_header, timeout, log,
    )
    _attestation_value = str(sentinel_headers.get("oai-web-deployment-attestation") or "")
    if not sentinel_headers.get("openai-sentinel-token"):
        return _fail(
            "sentinel",
            "GoPay 建单风控令牌生成失败：代理基础检测虽然可用，但无法通过该代理完成 Sentinel 请求；已停止无风控头建单",
            "checkout_risk_context_missing",
            t0,
            extra={"retryable": True},
        )

    log("checkout", "run", "建单（custom；风控头=" + str(sorted(sentinel_headers.keys())) + "）")
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
        if "impersonat" in type(exc).__name__.lower() or "impersonat" in str(exc).lower():
            log("checkout", "run", "当前 curl_cffi 不支持账号指纹内核（" + str(exc)[:80] + "），回退默认 chrome136 重试")
            fallback_fp = _chrome_fallback_fingerprint(fp)
            session = _new_session(proxy, fingerprint=fallback_fp)
            fp = fallback_fp
            sentinel_headers = _mint_checkout_risk_context(
                session.get, proxy, device_id, session_id, fp, promo_campaign_id,
                _session_cookie_header, timeout, log,
            )
            _attestation_value = str(sentinel_headers.get("oai-web-deployment-attestation") or "")
            try:
                r = session.post(_CHECKOUT_URL, json=checkout_body, headers=_chatgpt_headers(access_token, "/backend-api/payments/checkout", device_id=device_id, session_id=session_id, fingerprint=fp, cookie_header=_session_cookie_header, extra=sentinel_headers), timeout=timeout)
            except Exception as exc2:
                return _fail("checkout", "建单网络失败：" + type(exc2).__name__ + ": " + str(exc2)[:150], "wallet_transport_error", t0)
        else:
            return _fail("checkout", "建单网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
    if r.status_code >= 400:
        detail = _response_detail(r)
        error_extra = {"billing_country": cc, "upstream_status": int(r.status_code), "upstream_detail": detail}
        if r.status_code == 401:
            error_extra["retryable"] = False
            return _fail("checkout", "账号登录凭证已失效（HTTP 401），请重新获取 AT；系统未重试该请求。",
                         "access_token_invalid_or_expired", t0, extra=error_extra)
        if _is_unusual_activity(detail):
            error_extra["retryable"] = True
            return _fail("checkout", "上游异常活动风控拒绝了本次 GoPay 建单；将按任务的失败重试策略再次尝试。",
                         "checkout_risk_blocked", t0, extra=error_extra)
        if "billing country must match request country" in detail.lower():
            error_extra["retryable"] = False
            return _fail("checkout", "上游拒绝建单：账单国家与当前请求国家不一致。系统未自动重试。",
                         "checkout_billing_mismatch", t0, extra=error_extra)
        if r.status_code == 429 or "rate_limited" in detail.lower():
            return _fail("checkout", "建单被限流 HTTP 429: " + detail, "checkout_rate_limited", t0, extra=error_extra)
        error_extra["retryable"] = True
        return _fail("checkout", "建单请求被上游拒绝 HTTP " + str(r.status_code) + ": " + detail,
                     "checkout_request_rejected", t0, extra=error_extra)
    try:
        data = r.json() or {}
    except Exception:
        return _fail("checkout", "建单响应非 JSON", "checkout_failed", t0)
    cs = str(data.get("checkout_session_id") or data.get("session_id") or data.get("id") or "")
    if not cs:
        return _fail("checkout", "建单响应无会话 ID", "checkout_session_missing", t0)
    entity = str(data.get("processor_entity") or ("openai_llc" if cc == "US" else "openai_ie"))
    pk = str(data.get("publishable_key") or "")
    if not pk:
        return _fail("checkout", "建单响应缺少 publishable_key", "checkout_publishable_key_missing", t0)
    log("checkout", "ok", "建单成功 cs=" + cs[:16] + "… entity=" + entity)

    # ── Step 2: Stripe init（拿 init_checksum / gopay 渠道 / amount）──
    # OAICS custom sessions do not implement Stripe payment_pages/{id}/init.
    if cs.startswith("oaics_"):
        log("custom_payment_method", "run", "OAICS custom_payment_method/start (gopay, zero)")
        try:
            started, oaics_redirect = _start_oaics_gopay(
                session, access_token, cs, entity, device_id=device_id,
                session_id=session_id, fingerprint=fp,
                cookie_header=_session_cookie_header,
                sentinel_headers=sentinel_headers, timeout=timeout,
                gopay_cpmt_id=gopay_cpmt_id)
        except Exception as exc:
            return _fail("custom_payment_method", "OAICS GoPay start failed: " + str(exc)[:250], "start_failed", t0)
        if not oaics_redirect:
            return _fail("custom_payment_method", "OAICS GoPay start returned no redirect", "no_redirect", t0)
        return {"ok": True, "link": oaics_redirect, "url": oaics_redirect,
                "checkout_session_id": cs, "processor_entity": entity,
                "publishable_key": pk, "payment_method": "gopay",
                "link_type": "gopay_oaics_custom_link", "reason_code": "",
                "extra": {"payment_method": "gopay", "checkout_session_type": "oaics", "amount_minor": 0}}

    stripe_js_id = str(uuid.uuid4())
    elements_session_id = "elements_session_" + str(uuid.uuid4()).replace("-", "")[:11]
    elements_session_config_id = str(uuid.uuid4())
    _init_started_at = time.monotonic()  # 用于 confirm 的 time_on_page 动态计算
    init_body = {
        "browser_locale": str(fp.get("locale") or "id-ID"),
        "browser_timezone": str(fp.get("timezone") or "Asia/Jakarta"),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": str((fp.get("locale") or "id-ID").split("-")[0] or "id"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": pk,
        "_stripe_version": _STRIPE_VERSION,
    }
    log("stripe_init", "run", "初始化 Stripe Elements（payment_pages/init）")
    try:
        ir = session.post(_STRIPE_PAGE_URL.format(checkout_session_id=cs) + "/init",
                          data=init_body, headers=_stripe_headers(cs, fp), timeout=timeout)
    except Exception as exc:
        return _fail("stripe_init", "Stripe init 网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
    if ir.status_code >= 400:
        return _fail("stripe_init", "Stripe init HTTP " + str(ir.status_code) + ": " + (ir.text or "")[:250],
                     "stripe_init_failed", t0, extra={"retryable": True, "upstream_status": int(ir.status_code)})
    try:
        init_data = ir.json() or {}
    except Exception:
        return _fail("stripe_init", "Stripe init 响应非 JSON", "stripe_init_failed", t0)
    init_checksum = str(init_data.get("init_checksum") or "")
    pm_types = [str(x).lower() for x in (init_data.get("payment_method_types") or [])]
    amount_minor = _amount_minor_from_init(init_data)
    if "gopay" not in pm_types:
        return _fail("stripe_init",
                     "账号没有 GoPay 通道资格：Stripe init 返回 payment_method_types=" + json.dumps(pm_types, ensure_ascii=False),
                     "wallet_method_unavailable", t0,
                     extra={"checkout_session_id": cs, "payment_method_types": pm_types, "retryable": False})
    if require_zero and amount_minor is not None and amount_minor != 0:
        return _fail("stripe_init",
                     "促销压0未生效：当前金额为 " + str(amount_minor) + " IDR，当前代理出口可能无优惠资格，将换代理重试。"
                     "请换有优惠资格的账号，或关闭 require-zero 输出原价链接",
                     "wallet_checkout_not_zero_due", t0,
                     extra={"checkout_session_id": cs, "amount_minor": amount_minor, "currency": cur, "retryable": True})
    log("stripe_init", "ok", "Stripe init 成功 gopay=" + str("gopay" in pm_types)
        + " amount=" + (str(amount_minor) if amount_minor is not None else "?") + " IDR")

    # ── Step 2b: Stripe Elements Session（对齐 HAR，confirm 前必须）──
    # 拿到真实的 elements session_id / config_id / payment_method_configuration，供 confirm
    # 的 attribution 分层使用；缺失时 confirm 可能因 config attribution 不完整被拒。
    elements_session_id = "elements_session_" + str(uuid.uuid4()).replace("-", "")[:11]
    elements_session_config_id = str(init_data.get("config_id") or uuid.uuid4())
    _es_data = {}
    try:
        _elements_params = {
            "type": "deferred_intent",
            "deferred_intent[mode]": "subscription",
            "deferred_intent[amount]": str(0 if amount_minor is None else int(amount_minor)),
            "deferred_intent[currency]": cur.lower(),
            "deferred_intent[setup_future_usage]": "off_session",
            "deferred_intent[payment_method_types][0]": "card",
            "deferred_intent[payment_method_types][1]": "gopay",
            "checkout_session_id": cs,
            "stripe_js_id": stripe_js_id,
            "key": pk,
            "_stripe_version": _STRIPE_VERSION,
            "elements_init_source": "stripe.elements",
            "referrer_host": "chatgpt.com",
            "locale": str((fp.get("locale") or "id-ID").split("-")[0] or "id"),
            "browser_timezone": str(fp.get("timezone") or "Asia/Jakarta"),
        }
        _es = session.get("https://api.stripe.com/v1/elements/sessions",
                          params=_elements_params, headers=_stripe_headers(cs, fp), timeout=timeout)
        if _es.status_code == 200:
            _es_data = _es.json() or {}
            if isinstance(_es_data, dict):
                if _es_data.get("session_id"):
                    elements_session_id = str(_es_data["session_id"])
                if _es_data.get("config_id"):
                    elements_session_config_id = str(_es_data["config_id"])
                log("elements_session", "ok", "Elements Session 已建立 session=" + elements_session_id[:18]
                    + " config=" + elements_session_config_id[:12])
        else:
            log("elements_session", "warn", "Elements Session HTTP " + str(_es.status_code) + "（回退本地 uuid）")
    except Exception as exc:
        log("elements_session", "warn", "Elements Session 失败（回退本地 uuid）：" + type(exc).__name__)

    # 会话一致性校验：confirm 只能使用当前 Checkout/init 的金额、币种、支付方式
    # 和 Elements session，避免旧响应或默认值混入后由 Stripe 在异步阶段拒绝。
    elements_amount = _amount_minor_from_init(_es_data) if isinstance(_es_data, dict) else None
    elements_currency = _payload_currency(_es_data).lower() if isinstance(_es_data, dict) else ""
    elements_methods = _payload_payment_methods(_es_data) if isinstance(_es_data, dict) else set()
    if elements_amount is not None and amount_minor is not None and elements_amount != amount_minor:
        return _fail("elements_session", "Elements 金额与 Checkout 金额不一致", "session_mismatch", t0,
                     extra={"checkout_session_id": cs, "amount_minor": amount_minor,
                            "elements_amount_minor": elements_amount, "currency": cur,
                            "payment_method": "gopay"})
    if elements_currency and elements_currency != cur.lower():
        return _fail("elements_session", "Elements 币种与 Checkout 币种不一致", "session_mismatch", t0,
                     extra={"checkout_session_id": cs, "amount_minor": amount_minor,
                            "currency": cur, "elements_currency": elements_currency,
                            "payment_method": "gopay"})
    if elements_methods and "gopay" not in elements_methods:
        return _fail("elements_session", "Elements 会话未返回 GoPay 支付方式", "wallet_method_unavailable", t0,
                     extra={"checkout_session_id": cs, "amount_minor": amount_minor,
                            "currency": cur, "payment_methods": sorted(elements_methods),
                            "payment_method": "gopay"})

    # ── Step 3: taxes（印尼账单）──
    _billing = {}
    try:
        from id_billing import generate_id_billing as _gen_billing
        # 坐标反查优先：出口城市名只能匹配到内置街道表里的整座城市，坐标能把
        # 街道落到出口所在的具体位置。反查失败或无坐标时回退同城匹配。
        _reverse = _reverse_gopay_billing(fp, cc)
        if _reverse:
            log("config", "run", "代理坐标反查命中账单地址（" + str(_reverse.get("city") or "?") + "）")
        _geo_city = str(fp.get("proxy_city") or "")
        if not _geo_city and not _reverse:
            try:
                from id_billing import probe_exit_city as _probe_city
                _geo_city = _probe_city(proxy, timeout=min(max(timeout, 5.0), 8.0))
            except Exception:
                _geo_city = ""
        _billing = _gen_billing(str(fp.get("account_email") or ""), country=cc, currency=cur,
                                seed=None, geo_city=_geo_city, reverse_address=_reverse or None)
    except Exception as exc:
        log("taxes", "warn", "ID 账单生成器异常（使用最小账单）：" + type(exc).__name__ + "：" + str(exc)[:100])
        _billing = {
            "name": "Budi Santoso",
            "email": str(fp.get("account_email") or "") or "buyer@example.com",
            "address": {"country": cc, "line1": "Jl. Jend. Sudirman No. 20", "line2": "", "city": "Jakarta", "state": "DKI Jakarta", "postal_code": "12190"},
            "_source": "fallback",
        }
    _baddr = _billing.get("address") or {}
    taxes_body = {
        "checkout_session_id": cs,
        "checkout_email": str(_billing.get("email") or ""),
        "billing_country": cc,
        "billing_name": str(_billing.get("name") or ""),
        "currency": cur.lower(),
        "processor_entity": entity,
        "billing_address": {
            "country": cc,
            "line1": str(_baddr.get("line1") or ""),
            "line2": str(_baddr.get("line2") or ""),
            "city": str(_baddr.get("city") or ""),
            "state": str(_baddr.get("state") or ""),
            "postal_code": str(_baddr.get("postal_code") or ""),
        },
    }
    log("taxes", "run", "提交 ID 账单（" + str(_baddr.get("line1") or "") + "，" + str(_baddr.get("city") or "") + "）")
    try:
        tx = session.post(_TAXES_URL, json=taxes_body,
                          headers=_chatgpt_headers(access_token, "/backend-api/payments/checkout/taxes",
                                                   referer="https://chatgpt.com/checkout/" + entity + "/" + cs,
                                                   device_id=device_id, session_id=session_id, fingerprint=fp,
                                                   cookie_header=_session_cookie_header,
                                                   extra={"oai-web-deployment-attestation": _attestation_value} if _attestation_value else None),
                          timeout=timeout)
    except Exception as exc:
        return _fail("taxes", "ID 账单提交网络失败：" + type(exc).__name__ + "：" + str(exc)[:150], "wallet_transport_error", t0)
    if tx.status_code >= 400:
        return _fail("taxes", "ID 账单提交被上游拒绝 HTTP " + str(tx.status_code) + "：" + (tx.text or "")[:300],
                     "checkout_request_rejected", t0, extra={"retryable": True, "upstream_status": int(tx.status_code)})
    log("taxes", "ok", "ID 账单已接受（地址来源=" + str(_billing.get("_source")) + "）")

    # ── Step 4: snapshot（账单快照，对齐 HAR）──
    snapshot_body = {"snapshot": {"billing_address": {
        "name": str(_billing.get("name") or ""),
        "address": {
            "line1": str(_baddr.get("line1") or ""),
            "line2": str(_baddr.get("line2") or ""),
            "city": str(_baddr.get("city") or ""),
            "country": cc,
            "postal_code": str(_baddr.get("postal_code") or ""),
            "state": str(_baddr.get("state") or ""),
        },
    }}}
    log("snapshot", "run", "提交账单快照（checkout/snapshot）")
    try:
        sn = session.post(_SNAPSHOT_URL, json=snapshot_body,
                          headers=_chatgpt_headers(access_token, "/backend-api/payments/checkout/snapshot",
                                                   referer="https://chatgpt.com/checkout/" + entity + "/" + cs,
                                                   device_id=device_id, session_id=session_id, fingerprint=fp,
                                                   cookie_header=_session_cookie_header,
                                                   extra={"oai-web-deployment-attestation": _attestation_value} if _attestation_value else None),
                          timeout=timeout)
    except Exception as exc:
        log("snapshot", "warn", "账单快照提交失败（不阻断）：" + type(exc).__name__ + "：" + str(exc)[:100])
        sn = None
    if sn is not None and sn.status_code >= 400:
        log("snapshot", "warn", "账单快照被拒 HTTP " + str(sn.status_code) + "（不阻断）")
    else:
        log("snapshot", "ok", "账单快照已提交")

    # ── Step 4b: Stripe tax_region 地址同步（对齐 HAR，confirm 前必须做）──
    # 浏览器在 init 后按字段渐进 POST payment_pages/{cs} 同步 tax_region 账单地址；
    # 缺少这一步会令 confirm 后 Stripe session 状态异常，approve 后 poll 报
    # checkout_not_active_session。这里一次性把完整地址同步过去。
    tax_region_body = {
        "tax_region[country]": cc,
        "tax_region[line1]": str(_baddr.get("line1") or ""),
        "tax_region[city]": str(_baddr.get("city") or ""),
        "tax_region[postal_code]": str(_baddr.get("postal_code") or ""),
        "tax_region[state]": str(_baddr.get("state") or ""),
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": str((fp.get("locale") or "id-ID").split("-")[0] or "id"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "key": pk,
        "_stripe_version": _STRIPE_VERSION,
    }
    log("tax_region", "run", "同步 Stripe tax_region 账单地址（payment_pages POST）")
    _top_checkout_config_id = str(init_data.get("config_id") or "")
    try:
        tr = session.post(_STRIPE_PAGE_URL.format(checkout_session_id=cs),
                          data=tax_region_body, headers=_stripe_headers(cs, fp), timeout=timeout)
        if tr.status_code == 200:
            try:
                _tr_data = tr.json() or {}
                if isinstance(_tr_data, dict) and _tr_data.get("config_id"):
                    # 成功 HAR：payment_pages POST 响应回传新的 config_id，
                    # 顶层 client_attribution_metadata[checkout_config_id] 用这个最新值。
                    _top_checkout_config_id = str(_tr_data["config_id"])
            except Exception:
                pass
    except Exception as exc:
        log("tax_region", "warn", "tax_region 同步网络失败（不阻断）：" + type(exc).__name__ + "：" + str(exc)[:100])
        tr = None
    if tr is not None and tr.status_code >= 400:
        log("tax_region", "warn", "tax_region 同步被拒 HTTP " + str(tr.status_code) + "（不阻断）")
    else:
        log("tax_region", "ok", "tax_region 账单地址已同步")

    # ── Step 5: Stripe confirm（内嵌 payment_method_data[type]=gopay）──
    # 纯协议路径：confirm 需到 requires_approval；approve 后若 generic_decline，
    # 为代理信誉/账号状态问题，通过换代理重试碰运气。
    guid = uuid.uuid4().hex
    muid = uuid.uuid4().hex
    sid = uuid.uuid4().hex
    expected_amount = str(amount_minor if amount_minor is not None else 0)
    return_url = _gopay_return_url(init_data, cs)
    # time_on_page：从 init 到 confirm 的真实耗时（毫秒），对齐 HAR 而非固定 30000。
    _time_on_page = max(20000, int((time.monotonic() - _init_started_at) * 1000))
    # checkout_config_id：payment-method 级用 init 的 config_id，顶层用 tax_region 回传的最新值。
    _checkout_config_id = str(init_data.get("config_id") or "")

    confirm_body = {
        "guid": guid, "muid": muid, "sid": sid,
        "payment_method_data[billing_details][name]": str(_billing.get("name") or ""),
        "payment_method_data[billing_details][email]": str(_billing.get("email") or ""),
        "payment_method_data[billing_details][address][line1]": str(_baddr.get("line1") or ""),
        "payment_method_data[billing_details][address][city]": str(_baddr.get("city") or ""),
        "payment_method_data[billing_details][address][postal_code]": str(_baddr.get("postal_code") or ""),
        "payment_method_data[billing_details][address][state]": str(_baddr.get("state") or ""),
        "payment_method_data[billing_details][address][country]": cc,
        "payment_method_data[type]": "gopay",
        "payment_method_data[payment_user_agent]": f"stripe.js/{_STRIPE_RUNTIME_VERSION}; stripe-js-v3/{_STRIPE_RUNTIME_VERSION}; payment-element; deferred-intent",
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(_time_on_page),
        "payment_method_data[client_attribution_metadata][client_session_id]": stripe_js_id,
        "payment_method_data[client_attribution_metadata][checkout_session_id]": cs,
        "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
        "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
        "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
        "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
        "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "automatic",
        "payment_method_data[client_attribution_metadata][elements_session_id]": elements_session_id,
        "payment_method_data[client_attribution_metadata][elements_session_config_id]": elements_session_config_id,
        "payment_method_data[client_attribution_metadata][checkout_config_id]": _checkout_config_id,
        "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "expressCheckout",
        "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "payment",
        "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]": "address",
        "init_checksum": init_checksum,
        "version": _STRIPE_RUNTIME_VERSION,
        "expected_amount": expected_amount,
        "expected_payment_method_type": "gopay",
        "return_url": return_url,
        "link_brand": "link",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": str((fp.get("locale") or "id-ID").split("-")[0] or "id"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": cs,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": elements_session_config_id,
        "client_attribution_metadata[checkout_config_id]": _top_checkout_config_id,
        "key": pk,
        "_stripe_version": _STRIPE_VERSION,
    }
    log("confirm", "run", "Stripe confirm（内嵌 payment_method_data[type]=gopay，expected_amount=" + expected_amount + "）")
    try:
        cr = session.post(_STRIPE_PAGE_URL.format(checkout_session_id=cs) + "/confirm",
                          data=confirm_body, headers=_stripe_headers(cs, fp), timeout=timeout)
    except Exception as exc:
        return _fail("confirm", "Stripe confirm 网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
    if cr.status_code >= 400:
        return _fail("confirm", "Stripe confirm HTTP " + str(cr.status_code) + ": " + (cr.text or "")[:300],
                     "confirm_failed", t0, extra={"retryable": True, "upstream_status": int(cr.status_code)})
    try:
        confirm_data = cr.json() or {}
    except Exception:
        confirm_data = {}
    # 诊断：confirm 后 submission_attempt 必须进入 requires_approval，否则支付意图未建立
    _sa = confirm_data.get("submission_attempt") if isinstance(confirm_data.get("submission_attempt"), dict) else {}
    _sa_state = str(_sa.get("state") or "none")
    _sa_err = _sa.get("error")
    _has_pi = isinstance(confirm_data.get("payment_intent"), dict)
    log("confirm", "ok", "Stripe confirm 成功（submission_attempt=" + _sa_state
        + " payment_intent=" + ("有" if _has_pi else "无")
        + (" error=" + json.dumps(_sa_err, ensure_ascii=False)[:200] if _sa_err else "") + "）")
    if _sa_state in ("failed", "declined"):
        return _fail("confirm", "Stripe confirm 未建立支付意图（submission_attempt=" + _sa_state
                     + ("，error=" + json.dumps(_sa_err, ensure_ascii=False)[:300] if _sa_err else "") + "）",
                     "confirm_failed", t0, extra={"checkout_session_id": cs, "retryable": True})

    # ── Step 6: approve（ChatGPT 审核 → result=approved）──
    _risk_cache: dict = {"sentinel": "", "so": "", "attestation": _attestation_value if _attestation_value else ""}
    approved = False
    log("approve", "run", "ChatGPT checkout approve（result=approved）")
    for _attempt in range(2):
        cpmt_risk = _approval_risk_headers(
            proxy, device_id, session_id, fp, _session_cookie_header,
            access_token=access_token, entity=entity, checkout_session_id=cs,
            timeout=timeout, emit=log, sentinel_cache=_risk_cache,
        )
        approve_headers = _chatgpt_headers(access_token, "/backend-api/payments/checkout/approve",
                                           referer="https://chatgpt.com/checkout/" + entity + "/" + cs,
                                           device_id=device_id, session_id=session_id, fingerprint=fp,
                                           cookie_header=_session_cookie_header,
                                           extra={**cpmt_risk, "x-oai-is-client-observation": "true"} if cpmt_risk else {"x-oai-is-client-observation": "true"})
        try:
            ar = session.post(_APPROVE_URL, json={"checkout_session_id": cs, "processor_entity": entity},
                              headers=approve_headers, timeout=timeout)
        except Exception as exc:
            return _fail("approve", "ChatGPT approve 网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
        if ar.status_code >= 400:
            if _attempt == 0:
                log("approve", "run", "approve 首次被拒（HTTP " + str(ar.status_code) + "），重领 SEN/SO 重试…")
                _risk_cache = {"sentinel": "", "so": "", "attestation": _attestation_value if _attestation_value else ""}
                continue
            return _fail("approve", "ChatGPT approve HTTP " + str(ar.status_code) + ": " + (ar.text or "")[:250],
                         "approve_failed", t0, extra={"retryable": True, "upstream_status": int(ar.status_code)})
        try:
            approve_data = ar.json() or {}
        except Exception:
            approve_data = {}
        state = str(approve_data.get("result") or approve_data.get("status") or "").strip().lower()
        if state == "approved":
            approved = True
            log("approve", "ok", "ChatGPT approve 已批准")
            break
        if _attempt == 0:
            log("approve", "run", "approve 状态=" + (state or "?") + "，重试一次…")
            _risk_cache = {"sentinel": "", "so": "", "attestation": _attestation_value if _attestation_value else ""}
            continue
        return _fail("approve", "ChatGPT approve 未批准（state=" + (state or "?") + "）",
                     "approve_failed", t0, extra={"checkout_session_id": cs})
    if not approved:
        return _fail("approve", "ChatGPT approve 未批准", "approve_failed", t0, extra={"checkout_session_id": cs})

    # ── Step 7: poll（等 pm-redirects URL 出现）──
    # HAR 里 pm-redirects 位于深层字段 payment_intent.next_action.redirect_to_url.url，
    # 且只在 submission_attempt 从 None → processing 后的轮询里出现；poll 必须带与
    # init/confirm 一致的 elements_session_client 上下文，Stripe 才会推进 submission_attempt。
    redirect_url = ""
    poll_params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": str((fp.get("locale") or "id-ID").split("-")[0] or "id"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": pk,
        "_stripe_version": _STRIPE_VERSION,
    }
    log("poll", "run", "轮询 Stripe payment_pages，等待 pm-redirects 跳转")
    _last_poll_err = ""
    _last_poll_state = ""
    _last_poll_signature = ""
    for _attempt in range(20):
        try:
            pr = session.get(_STRIPE_PAGE_URL.format(checkout_session_id=cs), params=poll_params,
                             headers=_stripe_headers(cs, fp), timeout=timeout)
        except Exception as exc:
            return _fail("poll", "Stripe poll 网络失败：" + type(exc).__name__ + ": " + str(exc)[:150], "wallet_transport_error", t0)
        if pr.status_code >= 400:
            return _fail(
                "poll",
                "Stripe poll 请求失败 HTTP " + str(pr.status_code),
                "poll_failed",
                t0,
                extra={"retryable": True, "http_status": int(pr.status_code),
                       "amount_minor": amount_minor, "currency": cur,
                       "payment_method": "gopay", "checkout_session_id": cs},
            )
        try:
            poll_data = pr.json() or {}
        except Exception:
            poll_data = {}
        # 诊断：submission_attempt 状态 + 是否已有 payment_intent / pm-redirects
        _sub = poll_data.get("submission_attempt") if isinstance(poll_data.get("submission_attempt"), dict) else {}
        _sub_state = str(_sub.get("state") or "none")
        _sub_err = _sub.get("error")
        _has_pi = isinstance(poll_data.get("payment_intent"), dict)
        _pi_na = ""
        if _has_pi:
            _na = poll_data["payment_intent"].get("next_action") if isinstance(poll_data["payment_intent"].get("next_action"), dict) else None
            _pi_na = "next_action=" + ("有" if _na else "无")
        _payment_status = ""
        if _has_pi:
            _payment_status = str(poll_data["payment_intent"].get("status") or "")
        _poll_signature = "submission_attempt=" + _sub_state + " payment_intent=" + ("有" if _has_pi else "无")
        if _payment_status:
            _poll_signature += " payment_status=" + _payment_status
        if _pi_na:
            _poll_signature += " " + _pi_na
        # 正常轮询不刷屏，只在状态变化时记录。no_redirect 时可据此判断是
        # PaymentIntent 未建立、卡在 processing，还是被 Stripe 拒绝。
        if _poll_signature != _last_poll_signature:
            log("poll", "run", "poll #" + str(_attempt + 1) + " " + _poll_signature)
            _last_poll_signature = _poll_signature
        _last_poll_state = _poll_signature
        # payment_error 只保留 code/decline_code/message，避免把完整 Stripe 响应写入日志或 extra。
        if _sub_state in {"failed", "declined"}:
            failure = _payment_failure_summary(_sub_err)
            _last_poll_err = "payment_error=" + json.dumps(failure, ensure_ascii=False, separators=(",", ":"))
            failure_detail = "; ".join(
                name + "=" + value for name, value in (
                    ("code", failure["code"]),
                    ("decline_code", failure["decline_code"]),
                    ("message", failure["message"]),
                ) if value
            ) or "unknown"
            log("poll", "fail", "Stripe payment attempt failed: " + _last_poll_err)
            return _fail(
                "poll",
                "Stripe 支付尝试失败（" + failure_detail + "）",
                "payment_attempt_failed",
                t0,
                extra={
                    "checkout_session_id": cs,
                    "last_poll_state": _poll_signature[:300],
                    "payment_error": failure,
                    "poll_attempts": _attempt + 1,
                    "http_status": int(pr.status_code),
                    "amount_minor": amount_minor,
                    "currency": cur,
                    "payment_method": "gopay",
                },
            )
        # 优先从深层字段 payment_intent.next_action.redirect_to_url.url 提取（HAR 实测位置）
        text_all = json.dumps(poll_data, ensure_ascii=False, default=str)
        m = _PM_REDIRECT_RE.search(text_all)
        if m:
            redirect_url = m.group(0)
            break
        m = _MIDTRANS_REDIRECT_RE.search(text_all)
        if m:
            redirect_url = m.group(0)
            break
        # 顶层 url 字段兜底
        url_field = str(poll_data.get("url") or "")
        if _PM_REDIRECT_RE.search(url_field):
            redirect_url = _PM_REDIRECT_RE.search(url_field).group(0)
            break
        if _attempt == 0 or _attempt % 5 == 4:
            _err_txt = ""
            _decline = ""
            if isinstance(_sub_err, dict):
                _code = str(_sub_err.get("code") or "")
                _decline = str(_sub_err.get("decline_code") or "")
                _msg = str(_sub_err.get("message") or "")[:120]
                _err_txt = " error_code=" + _code
                if _decline:
                    _decline = " decline_code=" + _decline
                if _msg:
                    _err_txt += " message=" + _msg
                # 嵌套 payment_error 才是真正的支付失败原因（approve 通过后 Stripe 建 PaymentIntent 被拒）
                _pay_err = _sub_err.get("payment_error")
                if isinstance(_pay_err, dict):
                    _pe_code = str(_pay_err.get("code") or _pay_err.get("type") or "")
                    _pe_decline = str(_pay_err.get("decline_code") or "")
                    _pe_msg = str(_pay_err.get("message") or "")[:160]
                    if _pe_code:
                        _err_txt += " payment_error.code=" + _pe_code
                    if _pe_decline:
                        _err_txt += " payment_error.decline_code=" + _pe_decline
                    if _pe_msg:
                        _err_txt += " payment_error.message=" + _pe_msg
                # 若 payment_error 是字符串，直接带上
                elif isinstance(_pay_err, str) and _pay_err:
                    _err_txt += " payment_error=" + _pay_err[:200]
            elif _sub_err:
                _err_txt = " error=" + str(_sub_err)[:300]
            # 仅在支付授权被拒时输出诊断，正常轮询静默，避免刷屏。
            if _sub_state == "failed":
                _last_poll_err = "sub_attempt=failed" + _err_txt + _decline
                log("poll", "run", f"poll #{_attempt+1} " + _last_poll_err)
        time.sleep(2.0)
    if not redirect_url:
        return _fail("poll", "轮询结束未取得 pm-redirects / Midtrans 跳转地址", "no_redirect", t0,
                     extra={"checkout_session_id": cs, "last_poll_err": _last_poll_err[:500],
                            "last_poll_state": _last_poll_state[:300], "poll_attempts": 20})
    log("poll", "ok", "已取得跳转：" + redirect_url[:90] + "…")

    # ── Step 8: follow → app.midtrans.com ──
    current = redirect_url
    final_url = ""
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
        m = _MIDTRANS_REDIRECT_RE.search(body_text)
        if m:
            final_url = m.group(0)
            break
        m = _MIDTRANS_REDIRECT_RE.search(current)
        if m:
            final_url = current
            break
        meta = _META_REFRESH_RE.search(body_text)
        if meta:
            target = urljoin(current, meta.group(1).strip())
            if "midtrans" in target or "pm-redirects" in target or "stripe" in target:
                current = target
                continue
        if "midtrans.com" in current or "pm-redirects.stripe.com" in current:
            if not re.search(r"\.(js|css|png|jpg|svg|ico|woff2?)(\?|$)", current):
                final_url = current
                break
            final_url = current
            break
        any_m = _MIDTRANS_ANY_RE.search(body_text)
        if any_m:
            candidate = any_m.group(0)
            if not re.search(r"\.(js|css|png|jpg|svg|ico|woff2?)(\?|$)", candidate):
                final_url = candidate
                break
        any_pm = _PM_REDIRECT_RE.search(body_text)
        if any_pm:
            final_url = any_pm.group(0)
            break
        final_url = current
        break
    if not final_url or ("midtrans" not in final_url.lower() and "pm-redirects" not in final_url.lower()):
        final_url = redirect_url

    log("resolve", "ok", "GoPay 授权链接：" + final_url[:110] + "…")
    return {
        "ok": True,
        "link": final_url,
        "redirect_url": redirect_url,
        "error": "",
        "reason_code": "",
        "extra": {
            "payment_method": "gopay",
            "link_type": "gopay_protocol",
            "checkout_session_id": cs,
            "billing_country": cc,
            "currency": cur,
            "checkout_session_type": ("oaics" if str(cs).startswith("oaics_")
                                     else "cs_live" if str(cs).startswith("cs_live_")
                                     else "custom"),
            "amount_minor": amount_minor,
            "elapsed_seconds": round(time.monotonic() - t0, 1),
        },
    }


__all__ = ["extract_gopay_custom_link"]
