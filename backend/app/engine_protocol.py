"""Projection of engine JSON-line events shared by the API and workers."""
from __future__ import annotations

from typing import Any


CONTROL_EVENT = "task.item.control.pending.v1"


def public_engine_extra(message: dict[str, Any]) -> dict[str, Any]:
    extra = dict(message.get("extra") or {}) if isinstance(message.get("extra"), dict) else {}
    aliases = {
        "qr_image": "qrImage", "qr_image_url": "qrImage", "image_url_png": "qrImage",
        "qr_url": "qrUrl", "qr_data": "qrData", "qr_pending": "qrPending",
        "qr_refresh_active": "qrRefreshActive", "expires_at": "expiresAt",
        "payment_status": "paymentStatus", "paid": "paid", "amount": "amount",
        "checkout_amount": "checkoutAmount", "promo_applied": "promoApplied",
        "hosted_instructions_url": "hostedInstructionsUrl",
        "provider_redirect_url": "providerRedirectUrl", "checkout_session_id": "checkoutSessionId",
    }
    for source, target in aliases.items():
        value = message.get(source, extra.get(source))
        if value is not None:
            extra[target] = value
    return extra


def apply_engine_event(item: Any, message: dict[str, Any]) -> None:
    """Apply only public engine fields; never expose control codes or credentials."""
    event = str(message.get("event") or "")
    if event == "stage":
        item.stage = str(message.get("stage") or "")[:64]
        item.stage_detail = str(message.get("detail") or "")[:500]
        return
    item.result = {**(item.result or {}), **public_engine_extra(message)}
    if event.startswith("blik_"):
        item.result = {
            **item.result,
            "blikAttempt": message.get("attempt", message.get("next_attempt", 1)),
            "blikMaxAttempts": message.get("max_attempts", 3),
        }
        if event in {"blik_waiting", "blik_retry", "blik_rejected"}:
            item.status = "waiting_blik"
            item.stage = "waiting_blik"
            item.stage_detail = "等待输入 6 位 BLIK 动态码"
            item.result["blikSubmitted"] = False
        elif event in {"blik_processing", "blik_bank_authorization"}:
            item.status = "running"
            item.stage = "bank_authorization" if event == "blik_bank_authorization" else "blik_submitted"
            item.stage_detail = "请在银行 App 中确认 BLIK 支付" if event == "blik_bank_authorization" else "动态码已送达支付进程"
    elif event == "qr_refresh":
        item.stage = "gateway"
        item.stage_detail = "支付二维码已更新"
    elif event == "result_pending":
        item.stage = "gateway"
        item.stage_detail = "支付链接已生成，正在获取二维码"
    elif event == "payment_status":
        item.stage_detail = "已确认支付到账" if message.get("paid") else "支付状态已更新"
