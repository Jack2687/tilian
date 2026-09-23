from __future__ import annotations

from typing import Any


def wallet_extra(mode: str, source: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Keep wallet engine fields while exposing the existing task/UI contract."""
    raw = source.get("extra")
    extra = dict(raw) if isinstance(raw, dict) else {}
    if mode == "paypal":
        extra.update({
            "amount": source.get("amount"),
            "orderType": source.get("order_type") or source.get("chain") or source.get("session_type") or "unknown",
            "sessionType": source.get("session_type") or "",
            "combo": source.get("combo") or "",
            "country": source.get("exit_country") or source.get("billing_country") or "",
            "reasonCode": source.get("reason_code") or "",
        })
        return extra
    if mode not in {"gopay", "gcash"}:
        return extra

    aliases = {
        "amount": "amount_minor", "amountMinor": "amount_minor",
        "orderType": "link_type", "sessionType": "checkout_session_type",
        "promotionApplied": "promotion_applied", "promotionError": "promotion_error",
        "offerState": "offer_state", "checkoutSessionHint": "checkout_session_id_hint",
        "checkoutSessionId": "checkout_session_id", "errorStage": "error_stage",
        "elapsedSeconds": "elapsed_seconds", "errorRaw": "error_raw",
        "billingCountry": "billing_country", "actualExitCountry": "actual_exit_country",
        "upstreamStatus": "upstream_status", "upstreamDetail": "upstream_detail",
    }
    for alias, name in aliases.items():
        if name in extra:
            extra[alias] = extra[name]
    extra.setdefault("country", extra.get("billing_country") or params.get("country") or "")
    extra.setdefault("currency", params.get("currency") or "")
    extra["reasonCode"] = source.get("reason_code") or extra.get("error_stage") or ""
    if mode == "gcash":
        image = source.get("qr_image") or extra.get("qr_image") or extra.get("qrImage") or ""
        extra["qrImage"] = image
        extra["qr_image"] = image
        extra["qrRefreshActive"] = extra.get("qr_refresh_active") is True
        extra["qrPending"] = extra.get("qr_pending") is True or (extra["qrRefreshActive"] and not image)
        extra["paymentStatus"] = source.get("payment_status") or extra.get("payment_status") or ""
        extra["paid"] = source.get("paid") is True or extra.get("paid") is True
    return extra
