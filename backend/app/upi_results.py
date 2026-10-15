"""Local checks on UPI delivery results; never fetch payment pages."""
from __future__ import annotations

import math
import time
from typing import Any


_STATUS_FIELDS = (
    "setup_intent_status", "setupIntentStatus",
    "payment_intent_status", "paymentIntentStatus",
    "payment_status", "paymentStatus", "payment_page_status", "paymentPageStatus",
    "intent_state", "intentState",
)
_REJECTED_STATES = {"requires_payment_method", "failed", "declined", "rejected", "blocked"}
_MATERIAL_FIELDS = {
    "link", "url", "long_url", "longUrl", "checkout_url", "checkoutUrl",
    "hosted_instructions_url", "hostedInstructionsUrl",
    "provider_redirect_url", "providerRedirectUrl", "redirect_url", "redirectUrl",
    "upi_deep_link", "upiDeepLink", "upi_uri", "upiUri", "mobile_auth_url", "mobileAuthUrl",
    "qr_url", "qrUrl", "qr_image", "qrImage", "qr_image_url", "qrImageUrl",
    "qr_image_url_png", "qr_image_url_svg", "qrImageUrlPng", "qrImageUrlSvg",
    "image_url", "image_url_png", "image_url_svg", "qr_data", "qrData",
    "next_action", "nextAction", "qr_code", "qrCode",
}
_ACTIVITY_FIELDS = {"qr_pending", "qrPending", "qr_refresh_active", "qrRefreshActive"}


def upi_delivery_failure(source: dict[str, Any]) -> tuple[str, str] | None:
    """A known negative state overrides ok/URL/amount in every result envelope.

    Older engines can omit intent state. Missing or unfamiliar states retain
    the existing URL and amount checks; they are not proof of payment success.
    """
    extra = source.get("extra")
    envelopes = [source, extra] if isinstance(extra, dict) else [source]
    states: set[str] = set()
    for envelope in envelopes:
        states.update(str(envelope.get(key) or "").strip().lower() for key in _STATUS_FIELDS)
        for key in ("setup_intent", "payment_intent"):
            intent = envelope.get(key)
            if isinstance(intent, dict):
                states.add(str(intent.get("status") or "").strip().lower())
        submission = envelope.get("submission_attempt")
        if isinstance(submission, dict):
            states.add(str(submission.get("state") or "").strip().lower())

    if states & _REJECTED_STATES:
        return "upi_payment_setup_declined", "UPI 支付请求已被拒绝或尚未具备支付条件，已停止交付链接"
    if states & {"canceled", "cancelled"}:
        return "upi_payment_canceled", "UPI 支付请求已取消，已停止交付链接"
    if "expired" in states:
        return "upi_checkout_expired", "UPI 支付链接已过期，请重新提交任务"

    # The engine emits Unix seconds, with 0 meaning no known expiry.
    now = time.time()
    for envelope in envelopes:
        for key in ("expires_at", "expiresAt"):
            value = envelope.get(key)
            if value is None or isinstance(value, bool):
                continue
            try:
                expires = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(expires) and 0 < expires <= now:
                return "upi_checkout_expired", "UPI 支付链接已过期，请重新提交任务"
    return None


def clear_upi_payment_material(source: dict[str, Any]) -> dict[str, Any]:
    """Copy a failed result without any link or QR that the UI can still use."""
    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        return {
            key: "" if key in _MATERIAL_FIELDS else False if key in _ACTIVITY_FIELDS else clean(item)
            for key, item in value.items()
        }

    result = clean(source)
    result["link"] = ""
    if "success" in result:
        result["success"] = False
    extra = result.get("extra")
    if isinstance(extra, dict):
        extra.update(qrPending=False, qrRefreshActive=False)
    return result
