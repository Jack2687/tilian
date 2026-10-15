import pytest

from app.worker import result_retryable


@pytest.mark.parametrize("flags", [
    {},
    {"retryable": False},
    {"retryable": True},
    {"retryable": False, "extra": {"retryable": True}},
])
def test_invalid_upi_billing_never_retries(flags):
    result = {"ok": False, "reason_code": "upi_billing_invalid", **flags}
    assert result_retryable(result) is False


@pytest.mark.parametrize("flags", [
    {},
    {"retryable": False},
    {"retryable": True},
    {"retryable": False, "extra": {"retryable": True}},
])
def test_upi_local_ca_configuration_error_never_retries(flags):
    result = {"ok": False, "reason_code": "upi_tls_configuration_error", **flags}
    assert result_retryable(result) is False


def test_upi_network_failure_can_still_retry():
    assert result_retryable({
        "ok": False,
        "reason_code": "upi_proxy_network_error",
        "retryable": True,
    }) is True


@pytest.mark.parametrize("reason", [
    "upi_payment_setup_declined", "upi_payment_canceled", "upi_checkout_expired",
])
def test_rejected_upi_delivery_never_automatically_retries(reason):
    assert result_retryable({
        "ok": False, "reason_code": reason, "retryable": True,
        "extra": {"retryable": True},
    }) is False
