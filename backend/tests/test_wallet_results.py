from app.engine_adapter import EngineAdapter


def test_gcash_qr_reaches_task_extra_and_retains_session_state():
    raw = {
        "ok": True, "link": "https://m.gcash.com/authorize/test",
        "qr_image": "data:image/png;base64,test",
        "extra": {"qr_refresh_active": True, "qr_pending": False, "amount_minor": 0,
                  "promotion_applied": True, "billing_country": "PH", "checkout_session_id": "oaics_test"},
    }
    result = EngineAdapter()._normalize_result("gcash", raw)
    assert result["ok"] is True
    assert result["extra"]["qrImage"] == raw["qr_image"]
    assert result["extra"]["qrRefreshActive"] is True
    assert result["extra"]["amountMinor"] == 0
    assert result["extra"]["promotionApplied"] is True
    assert result["extra"]["checkoutSessionId"] == "oaics_test"


def test_gcash_pending_qr_is_not_an_empty_finished_session():
    result = EngineAdapter()._normalize_result("gcash", {
        "ok": True, "link": "https://m.gcash.com/authorize/test", "extra": {"qr_refresh_active": True},
    })
    assert result["extra"]["qrPending"] is True
    assert result["extra"]["qrImage"] == ""


def test_paypal_top_level_metadata_survives_worker_extra_only_persistence():
    result = EngineAdapter()._normalize_result("paypal", {
        "ok": True, "ba_url": "https://www.paypal.com/agreements/approve?ba_token=test",
        "amount": 0, "chain": "oaics", "billing_country": "DE", "combo": "TH-DE",
    })
    assert result["extra"]["amount"] == 0
    assert result["extra"]["orderType"] == "oaics"
    assert result["extra"]["country"] == "DE"
    assert result["extra"]["combo"] == "TH-DE"


def test_gopay_failure_metadata_and_retryability_survive_normalization():
    result = EngineAdapter()._normalize_result("gopay", {
        "ok": False, "error": "代理连接失败", "reason_code": "network_error",
        "extra": {"retryable": True, "error_stage": "checkout", "amount_minor": None},
    })
    assert result["retryable"] is True
    assert result["extra"]["reasonCode"] == "network_error"
    assert result["extra"]["errorStage"] == "checkout"
    assert result["extra"]["amountMinor"] is None
