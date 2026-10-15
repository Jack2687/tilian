"""Offline checks for UPI payment material at the API delivery boundary."""

from copy import deepcopy

import pytest

from app.engine_adapter import EngineAdapter


HOSTED_URL = "https://payments.stripe.com/upi/instructions/delivery-fixture"
QR_URL = "https://qr.stripe.com/delivery-fixture.png"
ADAPTER = EngineAdapter()
PAYMENT_MATERIAL_FIELDS = (
    "link", "long_url", "longUrl", "url", "checkout_url", "checkoutUrl",
    "provider_redirect_url", "providerRedirectUrl",
    "hosted_instructions_url", "hostedInstructionsUrl",
    "upi_deep_link", "upiDeepLink", "qr_url", "qrUrl",
    "qr_image", "qrImage", "qr_image_url", "qrImageUrl",
    "qr_image_url_png", "qr_image_url_svg", "image_url_png", "image_url_svg",
    "qr_data", "qrData",
)


def valid_result(**updates):
    result = {
        "ok": True,
        "link": HOSTED_URL,
        "hosted_instructions_url": HOSTED_URL,
        "checkout_amount": 0,
        "promo_applied": True,
        "setup_intent_status": "requires_action",
        "payment_status": "requires_action",
    }
    result.update(updates)
    return result


def assert_no_payment_material(result):
    assert result["ok"] is False
    assert result["link"] == ""
    assert result["reason_code"]
    assert result["error"]
    for mapping in (result, result.get("extra", {})):
        for field in PAYMENT_MATERIAL_FIELDS:
            assert not mapping.get(field), field


@pytest.mark.parametrize(
    "field",
    ("setup_intent_status", "payment_intent_status", "payment_status", "payment_page_status"),
)
@pytest.mark.parametrize(
    "status",
    ("requires_payment_method", "failed", "canceled", "cancelled", "expired", "declined", "rejected", "blocked"),
)
def test_explicit_failure_overrides_positive_status_and_valid_amount(field, status):
    raw = valid_result(**{field: status})
    result = ADAPTER._normalize_result("upi", raw)
    assert_no_payment_material(result)


@pytest.mark.parametrize("status", ("requires_action", "processing", "succeeded", "plus_active"))
def test_existing_nonfailure_status_contract_is_preserved(status):
    raw = valid_result(setup_intent_status=status, payment_status=status)
    result = ADAPTER._normalize_result("upi", raw)
    assert result["ok"] is True
    assert result["link"] == HOSTED_URL


@pytest.mark.parametrize("status", (None, "", "unrecognized_future_status"))
def test_absent_or_unknown_status_keeps_existing_url_and_amount_contract(status):
    raw = valid_result()
    raw.pop("setup_intent_status")
    raw.pop("payment_status")
    if status is not None:
        raw["payment_status"] = status
    result = ADAPTER._normalize_result("upi", raw)
    assert result["ok"] is True
    assert result["link"] == HOSTED_URL


@pytest.mark.parametrize("expires_at", (1, "1"))
def test_expired_payment_material_is_not_delivered(expires_at):
    result = ADAPTER._normalize_result("upi", valid_result(expires_at=expires_at))
    assert_no_payment_material(result)


@pytest.mark.parametrize("expires_at", (None, 0, "0", 4_000_000_000))
def test_unknown_or_future_expiration_preserves_delivery(expires_at):
    result = ADAPTER._normalize_result("upi", valid_result(expires_at=expires_at))
    assert result["ok"] is True
    assert result["link"] == HOSTED_URL


@pytest.mark.parametrize(
    "url",
    (
        "http://payments.stripe.com/upi/instructions/delivery-fixture",
        "https://payments.stripe.com/upi/instructions/",
        "https://payments.stripe.com/upi/instructions/?token=fixture",
        "https://payments.stripe.com/upi/instructions/#fixture",
        "https://payments.stripe.com.example.invalid/upi/instructions/fixture",
        "https://example.invalid/upi/instructions/fixture",
        "https://[invalid/upi/instructions/fixture",
    ),
)
def test_hosted_url_must_be_valid_https_stripe_instructions_with_a_token(url):
    result = ADAPTER._normalize_result(
        "upi", valid_result(link=url, hosted_instructions_url=url)
    )
    assert_no_payment_material(result)


@pytest.mark.parametrize("failure", ("engine", "amount", "status", "expiry", "url"))
def test_failure_removes_all_public_payment_material_aliases(failure):
    raw = valid_result()
    for field in PAYMENT_MATERIAL_FIELDS:
        raw[field] = QR_URL if "qr" in field.lower() or "image" in field.lower() else HOSTED_URL
    raw["extra"] = {field: raw[field] for field in PAYMENT_MATERIAL_FIELDS}
    if failure == "engine":
        raw["ok"] = False
    elif failure == "amount":
        raw["checkout_amount"] = 10
    elif failure == "status":
        raw["setup_intent_status"] = "canceled"
    elif failure == "expiry":
        raw["expires_at"] = 1
    else:
        raw["hosted_instructions_url"] = "http://payments.stripe.com/upi/instructions/fixture"
    original = deepcopy(raw)
    result = ADAPTER._normalize_result("upi", raw)
    assert_no_payment_material(result)
    assert raw == original


def test_failed_result_keeps_diagnostics_without_payment_material():
    result = ADAPTER._normalize_result(
        "upi",
        valid_result(
            ok=False,
            error="Provider declined the payment setup",
            reason_code="upi_payment_setup_declined",
            retryable=False,
            extra={"requestId": "req_delivery_fixture", "qrImage": QR_URL},
        ),
    )
    assert_no_payment_material(result)
    assert result["reason_code"] == "upi_payment_setup_declined"
    assert result["error"] == "Provider declined the payment setup"
    assert result["retryable"] is False
    assert result["extra"]["requestId"] == "req_delivery_fixture"


@pytest.mark.parametrize("extra", [
    {"setupIntentStatus": "canceled"},
    {"paymentStatus": "failed"},
    {"payment_intent": {"status": "requires_payment_method"}},
    {"submission_attempt": {"state": "failed"}},
    {"expiresAt": 1},
])
def test_nested_negative_evidence_is_terminal_even_on_engine_failure(extra):
    raw = valid_result(ok=False, retryable=True, extra={**extra, "retryable": True})
    result = ADAPTER._normalize_result("upi", raw)
    assert_no_payment_material(result)
    assert result["retryable"] is False


@pytest.mark.asyncio
async def test_invalid_pending_upi_result_does_not_wait_for_or_publish_qr(monkeypatch):
    import json
    import sys

    adapter = EngineAdapter(timeout_seconds=2)
    raw = valid_result(setup_intent_status="canceled", qr_pending=True, qr_image="")
    script = "import time\nprint(" + repr(json.dumps(raw)) + ", flush=True)\ntime.sleep(30)"
    monkeypatch.setattr(adapter, "command", lambda *_: [sys.executable, "-u", "-c", script])
    messages = []

    async def on_event(message):
        messages.append(message)

    result, _ = await adapter.execute("upi", "fixture", "", "", {}, on_event=on_event)
    assert_no_payment_material(result)
    assert result["reason_code"] == "upi_payment_canceled"
    assert messages == []
