#!/usr/bin/env python3
"""HAR-backed iDEAL link extractor.

The iDEAL path shares the GCash login/session primitives: ``oai-did`` is
minted once, the ChatGPT page is warmed up, and the same curl session,
cookies, proxy and device headers are used for ChatGPT, Stripe and
``pay.ideal.nl``. Only payment requests observed in the supplied HAR are
implemented; the encrypted browser ``PUT`` payload is never hard-coded.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

_ROOT = Path(__file__).resolve().parents[1]
_IDEAL_ROOT = Path(__file__).resolve().parent
_GCASH_ROOT = _ROOT / "gcash_engine"
_PROVIDER_ROOT = _ROOT / "engines" / "pay153-checkout-link-main"
for _path in (_IDEAL_ROOT, _PROVIDER_ROOT, _GCASH_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sms_tool.paypal_proxy import normalize_proxy_url, redact_proxy_url  # noqa: E402
import gcash_custom_flow as _gcash  # noqa: E402
from nl_billing import generate_nl_billing  # noqa: E402
import provider_checkout as _provider  # noqa: E402
import stripe_checkout as _stripe  # noqa: E402

DEFAULT_COUNTRY = "NL"
DEFAULT_CURRENCY = "EUR"
PROVIDER = "ideal"
_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
_STDOUT_LOCK = threading.Lock()
_ACTIVE_RUNTIME: "_IdealRuntime | None" = None


def _print_stdout(value: str) -> None:
    with _STDOUT_LOCK:
        print(value, flush=True)


def _redact(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(authorization|cookie|sig|client_secret|secret)[\s:=]+[^\s,;]+", r"\1=<redacted>", text)
    text = re.sub(r"(?i)(access_token|\bat\b)[\s:=]+[^\s,;]+", r"\1=<redacted>", text)
    return text[:limit]


def emit_stage(stage: str, status: str, message: str, on_stage: Callable[..., Any] | None = None) -> None:
    safe_message = _redact(message)
    if callable(on_stage):
        on_stage(stage, status, safe_message)
        return
    _print_stdout(f"[{stage}] [{status}] {safe_message}")


def _failure(*, country: str, currency: str, elapsed: float, reason_code: str,
             error: str, error_stage: str = "resolve", retryable: bool = True,
             extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    details = {
        "payment_method": PROVIDER,
        "country": country,
        "billing_country": country,
        "currency": currency,
        "error_stage": error_stage,
        "implementation": "har_ideal",
        "elapsed_seconds": round(max(0.0, elapsed), 3),
        "retryable": bool(retryable),
    }
    if isinstance(extra, dict):
        details.update(extra)
    return {"ok": False, "link": "", "provider": PROVIDER, "error": str(error),
            "reason_code": reason_code, "retryable": bool(retryable), "extra": details}


def _response_detail(response, limit: int = 300) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("detail") or payload.get("error") or payload
        return _redact(json.dumps(payload, ensure_ascii=False, default=str), limit)
    except Exception:
        return _redact(getattr(response, "text", ""), limit)


def _classify_ideal_error(exc: Exception, default_reason_code: str) -> tuple[str, str, bool, str]:
    """Map explicit upstream iDEAL failures to stable UI/retry semantics.

    A non-zero amount is not proof that the account is ineligible: the
    promotion can depend on the checkout proxy.  Only explicit promotion
    eligibility responses and explicit absence of iDEAL are terminal.
    """
    raw = _redact(str(exc), 500)
    text = raw.casefold()

    if re.search(
        r"access[_ -]?token|token[^\n]{0,40}(?:expired|invalid|revoked)|"
        r"unauthori[sz]ed|authentication[^\n]{0,40}(?:failed|invalid)|\b401\b",
        text,
    ):
        return (
            "ideal_auth_invalid",
            "iDEAL 账号登录凭证已失效，请重新获取 AT 后再试",
            False,
            raw,
        )

    # These markers are emitted by checkout/update when the account cannot
    # use the campaign.  Do not classify a plain HTTP 403/400 as this error.
    if re.search(
        r"already[_ -]?redeemed|promotion[^\n]{0,80}(?:not[_ -]?eligible|ineligible|"
        r"unavailable|not available|invalid|expired|rejected)|"
        r"(?:promo|coupon|discount|offer)[^\n]{0,80}(?:not[_ -]?eligible|ineligible|"
        r"unavailable|not available|invalid|expired|redeemed|rejected)|"
        r"invalid[_ -]?promotion|promotion[_ -]?not[_ -]?eligible|"
        r"(?:not[_ -]?eligible|ineligible)[^\n]{0,120}(?:promotion|promo|coupon|discount|offer)|"
        r"优惠(?:资格|已使用|不可用|无效)",
        text,
    ):
        if re.search(r"already[_ -]?redeemed|redeemed|优惠已使用", text):
            return (
                "ideal_promo_ineligible",
                "iDEAL 优惠资格不可用：该账号已使用过此优惠，不具备再次领取资格",
                False,
                raw,
            )
        return (
            "ideal_promo_ineligible",
            "iDEAL 优惠压0失败：账号没有优惠资格",
            False,
            raw,
        )

    # Checkout can omit iDEAL before Stripe confirm, or return a setup attempt
    # that still requires a payment method.  Both mean this account/session
    # cannot produce an iDEAL link, rather than a transient network failure.
    if re.search(
        r"当前 checkout 未开放 ideal|应用优惠后 checkout 未开放 ideal|"
        r"does not offer ideal|ideal[^\n]{0,50}(?:unavailable|not available)|"
        r"(?:没有|无|未提供)[^\n]{0,30}ideal[^\n]{0,20}(?:资格|通道|支付方式)|"
        r"(?:ideal 支付通道拒绝|ideal 未返回银行跳转地址)[^\n]{0,180}"
        r"(?:submission_state=requires_payment_method|"
        r"setup_status=requires_payment_method)",
        text,
    ):
        return (
            "ideal_payment_method_unavailable",
            "iDEAL 支付方式不可用：当前账号没有 iDEAL 提链资格",
            False,
            raw,
        )

    # A known promotion failure without an eligibility marker is usually
    # proxy/session dependent and should be retried with a fresh environment.
    if re.search(
        r"plus[^\n]{0,50}(?:优惠|promotion)[^\n]{0,40}(?:未生效|未归零)|"
        r"优惠金额校验失败|checkout taxes 未保持 0|未确认优惠后应付金额|"
        r"应用 plus 优惠 http|checkout promotion update failed",
        text,
    ):
        return (
            "ideal_promo_not_applied",
            "iDEAL 优惠未生效：Checkout 应付金额未压到 0，请更换代理重试",
            True,
            raw,
        )

    if re.search(
        r"ideal[^\n]{0,80}(?:支付通道拒绝|未返回银行跳转地址)|"
        r"generic[_ -]?decline|setup_attempt_failed|submission_state=failed",
        text,
    ):
        return (
            "ideal_payment_declined",
            "iDEAL 支付通道拒绝：上游未生成银行跳转地址，请更换代理或账号重试",
            True,
            raw,
        )

    return (
        default_reason_code,
        "iDEAL Stripe 流程失败：" + raw[:350],
        True,
        raw,
    )


def _cookie_header(session, device_id: str) -> str:
    values: list[str] = []
    try:
        jar = getattr(session, "cookies", None)
        iterator = jar() if callable(jar) else jar
        for cookie in iterator or []:
            domain = str(getattr(cookie, "domain", "") or "").lower()
            if domain and "chatgpt.com" not in domain:
                continue
            name = str(getattr(cookie, "name", "") or "").strip()
            if name:
                values.append(name + "=" + str(getattr(cookie, "value", "") or ""))
    except Exception:
        pass
    if not any(item.lower().startswith("oai-did=") for item in values):
        values.append("oai-did=" + str(device_id))
    return "; ".join(values)


def _warm_session(session, access_token: str, device_id: str, session_id: str,
                  fp: dict, timeout: float, on_stage) -> str:
    cookie_header = "oai-did=" + device_id
    try:
        headers = _gcash._chatgpt_headers(
            access_token, "", referer="https://chatgpt.com/", device_id=device_id,
            session_id=session_id, fingerprint=fp, cookie_header=cookie_header,
        )
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin", "upgrade-insecure-requests": "1",
        })
        response = session.get("https://chatgpt.com/", headers=headers, timeout=timeout)
        cookie_header = _cookie_header(session, device_id)
        emit_stage("warmup", "ok", "ChatGPT 页面已预热（HTTP %s），会话 Cookie 已合并" % response.status_code, on_stage)
    except Exception as exc:
        cookie_header = _cookie_header(session, device_id)
        emit_stage("warmup", "warn", "ChatGPT 页面预热失败（继续使用当前会话）: %s" % type(exc).__name__, on_stage)
    return cookie_header


def _checkout(session, access_token: str, country: str, currency: str,
              promo_campaign_id: str, device_id: str, session_id: str,
              fingerprint: dict, cookie_header: str, timeout: float, on_stage):
    risk: dict[str, str] = {}
    try:
        risk = _gcash._mint_checkout_risk_context(
            session.get,
            str(getattr(session, "proxies", {}).get("https") or ""),
            device_id, session_id, fingerprint, promo_campaign_id,
            cookie_header, timeout,
            lambda stage, status, msg: emit_stage(stage, status, msg, on_stage),
        ) or {}
    except Exception as exc:
        emit_stage("sentinel", "warn", "风控上下文生成失败（按无证明继续）: %s" % type(exc).__name__, on_stage)
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": "custom",
    }
    if promo_campaign_id:
        body["promo_campaign"] = {"promo_campaign_id": promo_campaign_id, "is_coupon_from_query_param": False}
    headers = _gcash._chatgpt_headers(
        access_token, "/backend-api/payments/checkout",
        referer="https://chatgpt.com/" + ("?promo_campaign=" + promo_campaign_id if promo_campaign_id else ""),
        device_id=device_id, session_id=session_id, fingerprint=fingerprint,
        cookie_header=cookie_header, extra=risk,
    )
    emit_stage("checkout", "run", "创建 NL/EUR custom Checkout（AT + oai-did 会话）", on_stage)
    try:
        response = session.post(_CHECKOUT_URL, json=body, headers=headers, timeout=timeout)
    except Exception as exc:
        raise RuntimeError("建单网络失败: %s" % type(exc).__name__) from exc
    if response.status_code >= 400:
        detail = _response_detail(response)
        if response.status_code == 401:
            raise ValueError("access_token 无效或已过期（HTTP 401）")
        raise RuntimeError("建单被上游拒绝 HTTP %s: %s" % (response.status_code, detail))
    try:
        payload = response.json() or {}
    except Exception as exc:
        raise RuntimeError("建单响应非 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("建单响应格式异常")
    emit_stage("checkout", "ok", "Checkout 建单成功，已获得 Stripe 会话", on_stage)
    return payload, risk


def _checkout_id(payload: dict[str, Any]) -> str:
    for key in ("checkout_session_id", "session_id", "id"):
        if payload.get(key):
            return str(payload[key])
    match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", json.dumps(payload, default=str))
    return match.group(0) if match else ""


def _ideal_billing(country: str, email: str, fingerprint: dict) -> dict[str, Any]:
    account_email = str(email or fingerprint.get("account_email") or "").strip()
    # iDEAL intentionally rotates a complete Dutch billing identity per
    # checkout.  The account email remains stable so it still matches the
    # Checkout session and receipt destination.
    return generate_nl_billing(account_email, country="NL", currency="EUR")


def _ideal_taxes_body(session_id: str, processor: str, billing: dict[str, Any], currency: str) -> dict[str, Any]:
    address = dict(billing.get("address") or {})
    return {
        "checkout_session_id": session_id,
        "checkout_email": str(billing.get("email") or ""),
        "billing_country": str(address.get("country") or "NL").upper(),
        "billing_name": str(billing.get("name") or ""),
        "currency": str(currency or "EUR").upper(),
        "tax_id": str(billing.get("tax_id") or "") or None,
        "processor_entity": str(processor or "openai_ie"),
        "billing_address": {
            "country": str(address.get("country") or "NL").upper(),
            "line1": str(address.get("line1") or ""),
            "line2": str(address.get("line2") or ""),
            "city": str(address.get("city") or ""),
            "state": str(address.get("state") or ""),
            "postal_code": str(address.get("postal_code") or ""),
        },
    }


def _ideal_device_info() -> dict[str, Any]:
    """Values observed in the supplied HAR initiate request."""
    return {"language": "nl-NL", "timeZone": "Europe/Amsterdam", "screenWidth": 1279,
            "screenHeight": 953, "screenAvailableWidth": 1920,
            "screenAvailableHeight": 1040, "colorDepth": 24}


def _first_amount(value: Any) -> Any:
    """Find a checkout due/total field without treating prices as due amounts."""
    if isinstance(value, dict):
        for key in ("due", "amount_due", "checkout_amount", "total_due", "amount_total"):
            if key in value and value[key] not in (None, ""):
                return value[key]
        for child in value.values():
            found = _first_amount(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_amount(child)
            if found is not None:
                return found
    return None


def _is_zero_amount(value: Any) -> bool:
    try:
        return int(str(value)) == 0
    except (TypeError, ValueError):
        return str(value or "").strip() in {"0", "0.0", "0.00"}


def _status_from_payload(value: Any) -> str:
    keys = ("status", "transactionStatus", "paymentStatus", "state", "result")
    found: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or found:
            return
        if isinstance(node, dict):
            for key in keys:
                candidate = node.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    found.append(candidate.strip())
                    return
            for child in node.values():
                walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)
                if found:
                    return

    if isinstance(value, str):
        try:
            value = json.loads(value.strip())
        except Exception:
            return value.strip()
    walk(value)
    return found[0] if found else ""


def _payment_state(raw: str) -> str:
    value = re.sub(r"[^a-z]", "", str(raw or "").lower())
    success_values = {"succeeded", "success", "paid", "completed", "complete", "settled", "authorized", "authorised"}
    failed_values = {"failed", "failure", "cancelled", "canceled", "expired", "declined", "rejected"}
    if value in success_values or any(value.endswith(item) for item in success_values):
        return "succeeded"
    if value in failed_values or any(value.endswith(item) for item in failed_values):
        return "failed"
    return "pending"


def _sse_events(response):
    event_name = "message"
    data_lines: list[str] = []
    try:
        lines = response.iter_lines()
    except Exception:
        lines = []
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = str(raw or "")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


class _IdealRuntime:
    def __init__(self, session, hosted_url: str, status_url: str, transaction: dict[str, Any], on_qr_update, on_stage, user_agent: str = ""):
        self.session, self.hosted_url, self.status_url = session, hosted_url, status_url
        self.transaction, self.on_qr_update, self.on_stage = transaction, on_qr_update, on_stage
        self.user_agent = str(user_agent or _stripe.CHROME_UA)
        self.stop = threading.Event()
        self.refresh = threading.Event()
        self.lock = threading.RLock()
        self.deadline = time.monotonic() + 20 * 60
        self.last_state = "pending"

    def emit_qr(self, data: dict[str, Any]) -> None:
        payload = {
            "event": "qr_refresh",
            "qr_image": str(data.get("qr_image_svg") or data.get("qr_image_png") or data.get("qr_image") or ""),
            "qr_url": str(data.get("qr_url") or data.get("ideal_qr_url") or data.get("ideal_transaction_url") or ""),
            "qr_refresh_active": True,
            "updated_at": time.time(),
        }
        if (payload["qr_image"] or payload["qr_url"]) and callable(self.on_qr_update):
            self.on_qr_update(payload)

    def refresh_once(self) -> None:
        with self.lock:
            hosted = self.hosted_url
        if not hosted:
            return
        try:
            emit_stage("resolve", "run", "同一会话刷新 iDEAL 二维码", self.on_stage)
            refreshed = _provider.enrich_ideal_redirect(
                self.session, hosted, lambda msg: emit_stage("resolve", "run", msg, self.on_stage),
                device_info=_ideal_device_info(), http_referrer="https://chatgpt.com/", user_agent=self.user_agent,
            )
            if refreshed:
                with self.lock:
                    self.transaction.update(refreshed)
                    self.status_url = str(refreshed.get("ideal_status_url") or self.status_url)
                self.emit_qr(refreshed)
                emit_stage("resolve", "ok", "iDEAL 二维码已刷新（复用原交易会话）", self.on_stage)
        except Exception as exc:
            emit_stage("resolve", "warn", "iDEAL 二维码刷新失败（稍后可重试）: %s" % type(exc).__name__, self.on_stage)

    def status_loop(self) -> None:
        last_status = ""
        while not self.stop.is_set() and time.monotonic() < self.deadline:
            with self.lock:
                status_url = self.status_url
            if not status_url:
                time.sleep(0.5)
                continue
            try:
                response = self.session.get(
                    status_url,
                    headers={"Accept": "text/event-stream", "Referer": self.hosted_url, "User-Agent": self.user_agent},
                    timeout=45,
                    stream=True,
                )
                if getattr(response, "status_code", 0) != 200:
                    emit_stage("poll", "warn", "iDEAL 状态流 HTTP %s，稍后重连" % getattr(response, "status_code", "?"), self.on_stage)
                    time.sleep(1)
                    continue
                emit_stage("poll", "run", "已连接 iDEAL status SSE", self.on_stage)
                for event_name, raw_data in _sse_events(response):
                    if self.stop.is_set() or event_name not in {"status", "message"}:
                        continue
                    status = _status_from_payload(raw_data)
                    state = _payment_state(status)
                    if state == self.last_state and state == "pending":
                        continue
                    self.last_state = state
                    if state == "succeeded":
                        if callable(self.on_qr_update):
                            self.on_qr_update({"event": "payment_status", "payment_status": "succeeded", "paid": True,
                                               "qr_refresh_active": False, "updated_at": time.time()})
                        emit_stage("poll", "ok", "iDEAL 支付状态已到账", self.on_stage)
                        self.stop.set()
                        return
                    if state == "failed":
                        if callable(self.on_qr_update):
                            self.on_qr_update({"event": "payment_status", "payment_status": "failed", "paid": False,
                                               "qr_refresh_active": False, "updated_at": time.time()})
                        emit_stage("poll", "fail", "iDEAL 支付状态已结束：" + _redact(status, 80), self.on_stage)
                        self.stop.set()
                        return
                    if status and status != last_status:
                        last_status = status
                        if callable(self.on_qr_update):
                            self.on_qr_update({"event": "payment_status", "payment_status": status, "paid": False,
                                               "qr_refresh_active": True, "updated_at": time.time()})
                time.sleep(0.5)
            except Exception as exc:
                emit_stage("poll", "warn", "iDEAL 状态流断开，正在重连：%s" % type(exc).__name__, self.on_stage)
                time.sleep(1)
        self.stop.set()

    def refresh_loop(self) -> None:
        while not self.stop.is_set() and time.monotonic() < self.deadline:
            if self.refresh.wait(0.5):
                self.refresh.clear()
                self.refresh_once()


def _start_runtime(runtime: _IdealRuntime) -> None:
    global _ACTIVE_RUNTIME
    _ACTIVE_RUNTIME = runtime
    threading.Thread(target=runtime.status_loop, name="ideal-status-sse", daemon=True).start()
    threading.Thread(target=runtime.refresh_loop, name="ideal-qr-refresh", daemon=True).start()


def extract_ideal_link(
    access_token: str,
    proxy: str = "",
    *,
    proxy_pool: list[str] | None = None,
    exit_proxy: str = "",
    country: str = DEFAULT_COUNTRY,
    currency: str = DEFAULT_CURRENCY,
    promo_campaign_id: str = "plus-1-month-free",
    require_zero: bool = True,
    force_payment_method: bool = False,
    force_zero_checkout: bool = False,
    ideal_bank: str = "",
    fingerprint: dict[str, Any] | None = None,
    email: str = "",
    timeout: float = 45.0,
    sticky_proxy: bool = True,
    on_stage: Callable[..., Any] | None = None,
    on_qr_update: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    del proxy_pool, force_payment_method, force_zero_checkout, ideal_bank, sticky_proxy
    started = time.monotonic()
    cc, cur = str(country or DEFAULT_COUNTRY).strip().upper(), str(currency or DEFAULT_CURRENCY).strip().upper()
    # iDEAL is intentionally zero-due only.  The HAR's initial Checkout
    # request carries the campaign; callers cannot accidentally fall back to
    # the original-price flow by passing --no-require-zero.
    require_zero = True
    promo_campaign_id = str(promo_campaign_id or "plus-1-month-free").strip() or "plus-1-month-free"
    fp = dict(fingerprint) if isinstance(fingerprint, dict) else {}
    at = str(access_token or "").strip()
    proxy_url = normalize_proxy_url(str(proxy or exit_proxy or "").strip())
    if not at:
        return _failure(country=cc, currency=cur, elapsed=time.monotonic() - started,
                        reason_code="wallet_access_token_missing", error="access_token 为空",
                        error_stage="validation", retryable=False)
    if cc != "NL" or cur != "EUR":
        return _failure(country=cc, currency=cur, elapsed=time.monotonic() - started,
                        reason_code="ideal_region_mismatch", error="iDEAL 提链要求 NL/EUR",
                        error_stage="validation", retryable=False)
    if not proxy_url:
        emit_stage("config", "warn", "未提供代理，iDEAL 将使用直连（建议使用 NL 出口）", on_stage)
    else:
        emit_stage("config", "run", "iDEAL 统一会话代理=" + redact_proxy_url(proxy_url), on_stage)
    device_id = str(fp.get("device_id") or uuid.uuid4())
    session_id = str(fp.get("oai_session_id") or uuid.uuid4())
    try:
        # A direct CLI caller may omit the Node-generated fingerprint.  Keep
        # the fallback identity consistent with the NL HAR and with Stripe's
        # browser locale rather than silently mixing en-US headers into NL.
        fp.setdefault("accept_language", "nl-NL,nl;q=0.9,en;q=0.8")
        fp.setdefault("oai_language", "nl-NL")
        fp.setdefault("locale", "nl-NL")
        fp.setdefault("timezone", "Europe/Amsterdam")
        session = _gcash._new_session(proxy_url, fingerprint=fp)
        _stripe.ACTIVE_UA = str(fp.get("ua") or getattr(_gcash, "_CHATGPT_UA", _stripe.CHROME_UA))
        cookie_header = _warm_session(session, at, device_id, session_id, fp, max(10.0, timeout), on_stage)
        try:
            # Keep the simulated browser identity in the shared cookie jar so
            # snapshot/approve requests inherit the same ``oai-did`` even
            # when their helper builds a narrower header set.
            session.cookies.set("oai-did", device_id, domain=".chatgpt.com", path="/")
            session.headers.update({"oai-device-id": device_id, "oai-session-id": session_id,
                                    "oai-language": str(fp.get("oai_language") or "nl-NL")})
        except Exception:
            pass
        checkout, risk = _checkout(session, at, cc, cur, str(promo_campaign_id or "").strip(), device_id,
                                   session_id, fp, cookie_header, max(10.0, timeout), on_stage)
        checkout_session_id = _checkout_id(checkout)
        if not checkout_session_id:
            return _failure(country=cc, currency=cur, elapsed=time.monotonic() - started,
                            reason_code="checkout_session_missing", error="建单响应缺少 Checkout 会话 ID",
                            error_stage="checkout")
        processor = str(checkout.get("processor_entity") or "openai_ie")
        billing = _ideal_billing(cc, email, fp)
        risk_cache = {"sentinel": str(risk.get("openai-sentinel-token") or ""),
                      "attestation": str(risk.get("oai-web-deployment-attestation") or "")}

        def apply_promo_callback(entity: str = processor):
            """Apply the Plus campaign with the same ChatGPT update contract as GCash."""
            campaign = str(promo_campaign_id or "plus-1-month-free").strip() or "plus-1-month-free"
            headers = _gcash._chatgpt_headers(
                at,
                "/backend-api/payments/checkout/update",
                referer="https://chatgpt.com/checkout/%s/%s" % (entity or processor, checkout_session_id),
                device_id=device_id,
                session_id=session_id,
                fingerprint=fp,
                cookie_header=_cookie_header(session, device_id),
                extra={"x-oai-is-client-observation": "true"},
            )
            body = {
                "checkout_session_id": checkout_session_id,
                "processor_entity": entity or processor,
                "plan_name": "chatgptplusplan",
                "price_interval": "month",
                "seat_quantity": 1,
                "discount_code": None,
                "promo_campaign": {"promo_campaign_id": campaign, "is_coupon_from_query_param": False},
            }
            emit_stage("promotion", "run", "应用 Plus 0 元优惠：" + campaign, on_stage)
            response = session.post(
                "https://chatgpt.com/backend-api/payments/checkout/update",
                json=body,
                headers=headers,
                timeout=max(10.0, timeout),
            )
            if response.status_code >= 400:
                raise RuntimeError("应用 Plus 优惠 HTTP %s: %s" % (response.status_code, _response_detail(response)))
            emit_stage("promotion", "ok", "Plus 优惠已提交，重新初始化 Stripe 金额", on_stage)
            return response.json() if response.text else {}

        def approval_callback(entity: str = processor):
            approval_risk = _gcash._approval_risk_headers(
                proxy_url, device_id, session_id, fp, _cookie_header(session, device_id),
                access_token=at, entity=str(entity or processor), checkout_session_id=checkout_session_id,
                timeout=max(10.0, timeout),
                emit=lambda stage, status, msg: emit_stage(stage, status, msg, on_stage),
                sentinel_cache=risk_cache,
            )
            approval_headers = _gcash._chatgpt_headers(
                at, "/backend-api/payments/checkout/approve",
                referer="https://chatgpt.com/checkout/%s/%s" % (entity or processor, checkout_session_id),
                device_id=device_id, session_id=session_id, fingerprint=fp,
                cookie_header=_cookie_header(session, device_id),
                extra={**approval_risk, "x-oai-is-client-observation": "true"},
            )
            response = session.post("https://chatgpt.com/backend-api/payments/checkout/approve",
                                    json={"checkout_session_id": checkout_session_id,
                                          "processor_entity": entity or processor},
                                    headers=approval_headers, timeout=max(10.0, timeout))
            if response.status_code >= 400:
                raise RuntimeError("ChatGPT approve HTTP %s" % response.status_code)
            emit_stage("approve", "ok", "Checkout approve 已通过", on_stage)
            return response.json() if response.text else {}

        def chatgpt_taxes_callback(entity: str = processor):
            headers = _gcash._chatgpt_headers(
                at,
                "/backend-api/payments/checkout/taxes",
                referer="https://chatgpt.com/checkout/%s/%s" % (entity or processor, checkout_session_id),
                device_id=device_id,
                session_id=session_id,
                fingerprint=fp,
                cookie_header=_cookie_header(session, device_id),
                extra={
                    "oai-web-deployment-attestation": str(risk.get("oai-web-deployment-attestation") or ""),
                    "x-oai-is-client-observation": "true",
                },
            )
            emit_stage("taxes", "run", "同步 NL 账单到 ChatGPT Checkout taxes", on_stage)
            response = session.post(
                "https://chatgpt.com/backend-api/payments/checkout/taxes",
                json=_ideal_taxes_body(checkout_session_id, entity or processor, billing, cur),
                headers=headers,
                timeout=max(10.0, timeout),
            )
            if response.status_code >= 400:
                raise RuntimeError("Checkout taxes HTTP %s: %s" % (response.status_code, _response_detail(response)))
            emit_stage("taxes", "ok", "NL 账单已同步到 Checkout taxes", on_stage)
            payload = response.json() if response.text else {}
            taxes_amount = _first_amount(payload)
            if taxes_amount is not None and not _is_zero_amount(taxes_amount):
                raise RuntimeError("Checkout taxes 未保持 0 元：amount=%s" % taxes_amount)
            return payload

        emit_stage("stripe", "run", "开始 Stripe iDEAL init/confirm", on_stage)
        try:
            stripe_result = _provider.stripe_to_provider(
                session, checkout_session_id, "ideal", billing=billing, country=cc,
                promotion_billing=billing,
                payment_billing=billing, payment_http=session, chatgpt_http=session, access_token=at,
                stage1={"publishable_key": str(checkout.get("publishable_key") or ""),
                        "processor_entity": processor}, approve_callback=approval_callback,
                apply_promo_callback=apply_promo_callback if promo_campaign_id else None,
                chatgpt_taxes_callback=chatgpt_taxes_callback,
                ideal_bank="", ideal_device_info=_ideal_device_info(), ideal_http_referrer="https://chatgpt.com/",
                ideal_user_agent=str(fp.get("ua") or _stripe.CHROME_UA),
                require_zero_due=bool(require_zero),
                log=lambda msg: emit_stage("stripe", "run", msg, on_stage),
            )
        except Exception as exc:
            reason_code, error_message, retryable, error_raw = _classify_ideal_error(
                exc, "ideal_stripe_failed"
            )
            return _failure(country=cc, currency=cur, elapsed=time.monotonic() - started,
                            reason_code=reason_code, error=error_message,
                            error_stage="stripe", retryable=retryable,
                            extra={"checkout_session_id_hint": checkout_session_id[:16],
                                   "error_raw": error_raw})
        hosted_url = str(stripe_result.get("provider_redirect_url") or "").strip()
        qr_url = str(stripe_result.get("qr_url") or stripe_result.get("ideal_qr_url") or "").strip()
        qr_image = str(stripe_result.get("qr_image_svg") or stripe_result.get("qr_image_png") or "").strip()
        status_url = str(stripe_result.get("ideal_status_url") or "").strip()
        # Never turn a payment-page/QR response into a successful result unless
        # the final Stripe Checkout amount is explicitly verified as zero. The
        # iDEAL initiate amount can represent a separate mandate transaction;
        # it is not a substitute for the discounted Checkout due amount.
        checkout_amount = stripe_result.get("checkout_amount")
        promo_applied = stripe_result.get("promo_applied")
        if checkout_amount is None or not _is_zero_amount(checkout_amount) or promo_applied is not True:
            return _failure(
                country=cc,
                currency=cur,
                elapsed=time.monotonic() - started,
                reason_code="ideal_zero_amount_unverified",
                error="iDEAL 未确认优惠后应付金额为 0，已阻止输出支付链接",
                error_stage="stripe",
                retryable=True,
                extra={
                    "checkout_session_id_hint": checkout_session_id[:16],
                    "checkout_amount": checkout_amount,
                    "promo_applied": promo_applied,
                },
            )
        if not hosted_url and not qr_url:
            return _failure(country=cc, currency=cur, elapsed=time.monotonic() - started,
                            reason_code="ideal_redirect_missing", error="iDEAL 未返回支付页或二维码地址",
                            error_stage="resolve")
        transaction = dict(stripe_result)
        transaction.update({"qr_url": qr_url, "qr_image": qr_image, "status_url": status_url})
        if qr_url or status_url:
            runtime = _IdealRuntime(session, hosted_url, status_url, transaction, on_qr_update, on_stage,
                                    user_agent=str(fp.get("ua") or _stripe.CHROME_UA))
            if qr_image or qr_url:
                runtime.emit_qr(transaction)
            _start_runtime(runtime)
        emit_stage("resolve", "ok", "iDEAL 支付页与二维码提取完成", on_stage)
        # Stripe Checkout amount is the promotional due amount.  iDEAL's
        # initiate response may report a separate one-cent transaction amount
        # used for the mandate; do not expose that as the discounted amount.
        amount = checkout_amount
        return {
            "ok": True, "link": hosted_url or qr_url, "provider": PROVIDER,
            "provider_redirect_url": hosted_url, "qr_url": qr_url, "qr_data": qr_url,
            "qr_image": qr_image, "qr_image_svg": str(stripe_result.get("qr_image_svg") or ""),
            "qr_image_png": str(stripe_result.get("qr_image_png") or ""), "ideal_status_url": status_url,
            "payment_status": "requires_action", "paid": False,
            "qr_refresh_active": bool(qr_url or status_url), "amount": amount,
            "checkout_amount": stripe_result.get("checkout_amount"),
            "checkout_currency": str(stripe_result.get("checkout_currency") or cur).upper(),
            "ideal_transaction_amount": stripe_result.get("ideal_amount"),
            "billing_country": cc, "payment_method_types": stripe_result.get("payment_method_types") or ["ideal"],
            "promo_applied": stripe_result.get("promo_applied"),
            "link_type": "ideal_qr",
            "extra": {
                "payment_method": PROVIDER, "billing_country": cc, "currency": cur, "amount_minor": amount,
                "ideal_transaction_amount": stripe_result.get("ideal_amount"),
                "promotion_applied": stripe_result.get("promo_applied"),
                "checkout_session_id_hint": checkout_session_id[:16], "checkout_session_type": "custom",
                "link_type": "ideal_qr", "qr_url": qr_url, "qr_data": qr_url, "qr_image": qr_image,
                "qr_image_svg": str(stripe_result.get("qr_image_svg") or ""),
                "qr_image_png": str(stripe_result.get("qr_image_png") or ""), "ideal_status_url": status_url,
                "qr_refresh_active": bool(qr_url or status_url), "qr_pending": not bool(qr_image),
                "implementation": "har_ideal", "elapsed_seconds": round(time.monotonic() - started, 3),
            },
        }
    except Exception as exc:
        reason_code, error_message, retryable, error_raw = _classify_ideal_error(
            exc, "ideal_checkout_failed"
        )
        return _failure(country=cc, currency=cur, elapsed=time.monotonic() - started,
                        reason_code=reason_code, error=error_message,
                        error_stage="checkout", retryable=retryable,
                        extra={"error_raw": error_raw})


def _wait_active_runtime() -> None:
    runtime = _ACTIVE_RUNTIME
    if runtime is not None:
        runtime.stop.wait(max(1.0, runtime.deadline - time.monotonic()))


def demo_result(country: str = DEFAULT_COUNTRY, currency: str = DEFAULT_CURRENCY) -> dict[str, Any]:
    return {"ok": True, "link": "https://pay.ideal.nl/transactions/demo", "provider": PROVIDER,
            "amount": 0, "checkout_currency": currency, "billing_country": country,
            "payment_method_types": [PROVIDER], "link_type": "ideal_demo",
            "extra": {"payment_method": PROVIDER, "implementation": "demo"}}


def main() -> int:
    parser = argparse.ArgumentParser(description="iDEAL HAR 提链引擎")
    parser.add_argument("--at", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--proxy-pool", default="")
    parser.add_argument("--exit-proxy", default="")
    parser.add_argument("--country", default=DEFAULT_COUNTRY)
    parser.add_argument("--currency", default=DEFAULT_CURRENCY)
    parser.add_argument("--promo-campaign-id", default="plus-1-month-free")
    parser.add_argument("--require-zero", dest="require_zero", action="store_true")
    parser.add_argument("--no-require-zero", dest="require_zero", action="store_false")
    parser.set_defaults(require_zero=True)
    parser.add_argument("--force-payment-method", action="store_true")
    parser.add_argument("--force-zero", action="store_true")
    parser.add_argument("--ideal-bank", default="")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--email", default="")
    parser.add_argument("--timeout", default="45")
    parser.add_argument("--sticky-proxy", default="1")
    parser.add_argument("--json", default="")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    cc, cur = str(args.country or DEFAULT_COUNTRY).strip().upper(), str(args.currency or DEFAULT_CURRENCY).strip().upper()
    if args.demo:
        result = demo_result(cc, cur)
    else:
        fp: dict[str, Any] = {}
        if str(args.fingerprint or "").strip():
            try:
                parsed = json.loads(str(args.fingerprint))
                if isinstance(parsed, dict):
                    fp = parsed
            except Exception:
                emit_stage("fingerprint", "warn", "--fingerprint 不是合法 JSON，使用默认指纹")

        def emit_qr(payload: dict[str, Any]) -> None:
            try:
                _print_stdout(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            except BrokenPipeError:
                raise SystemExit(0)

        try:
            timeout = max(10.0, float(args.timeout or 45))
        except (TypeError, ValueError):
            timeout = 45.0
        result = extract_ideal_link(
            args.at, args.proxy, proxy_pool=[], exit_proxy=args.exit_proxy, country=cc, currency=cur,
            promo_campaign_id=args.promo_campaign_id, require_zero=bool(args.require_zero),
            force_payment_method=args.force_payment_method, force_zero_checkout=args.force_zero,
            ideal_bank=args.ideal_bank, fingerprint=fp, email=args.email, timeout=timeout,
            sticky_proxy=str(args.sticky_proxy or "1").lower() in {"1", "true", "yes", "on"},
            on_qr_update=emit_qr,
        )
    _print_stdout(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
    if args.json:
        try:
            Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        except OSError:
            pass
    if result.get("ok") and not args.demo:
        runtime = _ACTIVE_RUNTIME
        if runtime is not None:
            def read_commands() -> None:
                while not runtime.stop.is_set():
                    line = sys.stdin.readline()
                    if not line:
                        return
                    try:
                        command = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(command, dict) and command.get("command") == "refresh_qr":
                        runtime.refresh.set()
            threading.Thread(target=read_commands, name="ideal-qr-control", daemon=True).start()
        _wait_active_runtime()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
