from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from pix_service import (
    PixHttpClient, _BR_REAL_CNPJ_POOL, _confirm_form, _custom_fields,
    _extract_pix_action, _has_active_plus, _load_billing_profiles, _normalize_billing,
    _payable_amount, _pix_qr_amount, _return_url, _stripe_headers, _stripe_init,
    classify_error, extract_pix_link, generate_valid_cnpj, generate_valid_cpf,
    is_valid_cnpj, is_valid_cpf, localize_pix_error, random_brazil_billing,
)


TOKEN = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJwaXgifQ.signature"
TAX_ID = "52998224725"  # synthetic CPF-shaped fixture; never a production identity


def _success_transport(fail_confirm: bool = False):
    calls: list[tuple[str, str]] = []
    observed_headers: list[dict[str, str]] = []
    observed_forms: list[dict[str, str]] = []
    session = "cs_live_pix_fixture"

    def response(payload, status=200):
        return status, json.dumps(payload), {"content-type": "application/json"}

    def transport(method, url, headers, json_body, form, timeout, allow_redirects, proxy):
        path = urlparse(url).path
        calls.append((method, path))
        observed_headers.append(dict(headers or {}))
        observed_forms.append(dict(form or {}))
        if path.endswith("/") and "promo_campaign" in url:
            return 200, "<html></html>", {}
        if path.endswith("/backend-api/payments/checkout"):
            return response({"checkout_session_id": session, "publishable_key": "pk_test_fixture", "client_secret": "cs_secret_fixture", "processor_entity": "openai_llc", "checkout_ui_mode": "custom"})
        if path.endswith("/init"):
            return response({"config_id": "checkout_cfg", "init_checksum": "fixture_checksum", "version": "b0f5e7abe5", "amount": 0, "payment_method_types": ["card", "link", "pix"]})
        if path == "/v1/elements/sessions":
            return response({"session_id": "elements_session_fixture", "config_id": "elements_cfg", "ordered_payment_method_types_and_wallets": ["card", "pix"]})
        if path.endswith("/backend-api/payments/checkout/taxes"):
            return response({"checkout_session": {"amount_total": 0, "currency": "brl"}})
        if path.endswith("/backend-api/payments/checkout/snapshot"):
            return 204, "", {}
        if path.endswith("/confirm"):
            return response({"submission_attempt": {"state": "requires_approval"}})
        if path.endswith("/backend-api/payments/checkout/approve"):
            return response({"result": "approved"})
        if path == f"/v1/payment_pages/{session}":
            if fail_confirm:
                return response({"submission_attempt": {"state": "failed", "error": {"code": "checkout_approval_payment_failure_with_payment_error", "payment_error": {"code": "setup_attempt_failed", "decline_code": "generic_decline"}}}})
            return response({"submission_attempt": {"state": "requires_action"}, "next_action": {"pix_display_qr_code": {"hosted_instructions_url": "https://payments.stripe.com/pix/fixture", "image_url_png": "https://files.stripe.com/pix/fixture.png", "image_url_svg": "https://files.stripe.com/pix/fixture.svg", "data": "000201fixture", "expires_at": 1780000000}}})
        raise AssertionError(f"unexpected request: {method} {url}")

    transport.observed_headers = observed_headers
    transport.observed_forms = observed_forms
    return transport, calls


def test_qr_parser():
    value = _extract_pix_action({"payment_intent": {"next_action": {"pix_display_qr_code": {"hosted_instructions_url": "https://payments.stripe.com/pix/1", "image_url_png": "https://files.stripe.com/pix/1.png", "data": "000201"}}}})
    assert value and value["qr_url"].startswith("https://files.stripe.com/")
    assert value["qr_data"] == "000201"


def test_confirm_keeps_elements_context():
    form = _confirm_form(
        cs_id="cs_fixture",
        processor="openai_llc",
        stripe_pk="pk_test_fixture",
        stripe_js_id="js_fixture",
        elements={"session_id": "elements_fixture", "config_id": "elements_cfg"},
        init_data={"config_id": "checkout_cfg", "init_checksum": "checksum", "version": "b0"},
        billing={
            "name": "Buyer",
            "email": "buyer@example.com",
            "line1": "Avenida Paulista 1000",
            "city": "Sao Paulo",
            "postal_code": "01310-100",
            "state": "SP",
            "country": "BR",
            "tax_id": TAX_ID,
        },
        amount=0,
        checkout_secret="secret",
        checkout_config_id="checkout_page_cfg",
    )
    assert form["elements_session_client[session_id]"] == "elements_fixture"
    assert form["elements_session_client[stripe_js_id]"] == "js_fixture"
    assert form["elements_options_client[saved_payment_method][enable_save]"] == "auto"
    assert form["elements_options_client[saved_payment_method][enable_redisplay]"] == "auto"
    assert form["client_attribution_metadata[merchant_integration_source]"] == "checkout"
    assert form["client_attribution_metadata[merchant_integration_version]"] == "custom"
    assert form["client_attribution_metadata[checkout_config_id]"] == "checkout_page_cfg"
    assert form["payment_method_data[client_attribution_metadata][checkout_config_id]"] == "checkout_cfg"
    assert form["payment_method_data[billing_details][tax_id]"] == TAX_ID
    assert form["passive_captcha_ekey"] == ""


def test_stripe_init_omits_empty_elements_session_id():
    fields = _custom_fields(stripe_js_id="js_fixture")
    assert "elements_session_client[session_id]" not in fields
    fields = _custom_fields(
        stripe_js_id="js_fixture",
        elements={"session_id": "elements_fixture"},
    )
    assert fields["elements_session_client[session_id]"] == "elements_fixture"


def test_fingerprint_aliases_and_stripe_headers():
    client = PixHttpClient(
        transport=lambda *args: (200, "{}", {}),
        fingerprint={
            "device_id": "device-fixture",
            "ua": "FixtureBrowser/151",
            "impersonate": "chrome151",
            "oai_session_id": "session-fixture",
            "accept_language": "pt-BR,pt;q=0.9",
            "locale": "pt-BR",
            "timezone": "America/Belem",
        },
    )
    assert client.device_id == "device-fixture"
    assert client.session_id == "session-fixture"
    assert client.ua == "FixtureBrowser/151"
    assert client.impersonate == "chrome151"
    assert client.timezone == "America/Belem"
    headers = _stripe_headers(client)
    assert headers["accept"] == "application/json"
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert headers["accept-language"] == "pt-BR,pt;q=0.9"
    assert headers["sec-fetch-site"] == "same-site"
    client.close()


def test_init_parameter_negotiation_is_narrow():
    forms = []

    def transport(method, url, headers, json_body, form, timeout, allow_redirects, proxy):
        forms.append(dict(form or {}))
        if len(forms) == 1:
            return 400, json.dumps({"error": {"code": "parameter_unknown", "param": "elements_session_client[stripe_js_id]", "message": "unknown"}}), {}
        return 200, json.dumps({"config_id": "cfg"}), {}

    client = PixHttpClient(transport=transport, fingerprint={"ua": "Fixture/1", "oai_session_id": "sid"})
    result = _stripe_init(client, "cs_fixture", "pk_fixture", "js_fixture", "")
    assert result["config_id"] == "cfg"
    assert "elements_session_client[stripe_js_id]" in forms[0]
    assert "elements_session_client[stripe_js_id]" not in forms[1]
    client.close()


def test_return_url_uses_secret_fragment():
    value = _return_url("cs_fixture", "openai_llc", "cs_fixture_secret_opaque%2Fvalue")
    assert "client_secret=" not in value
    assert value.endswith("#opaque%2Fvalue")


def test_return_url_prefers_init_hosted_url_fragment():
    hosted = "https://checkout.stripe.com/c/pay/cs_fixture#long%2Fstripe%2Ffragment"
    value = _return_url("cs_fixture", "openai_llc", "cs_fixture_secret_short", hosted)
    assert value.startswith("https://checkout.stripe.com/c/pay/cs_fixture?returned_from_redirect=true")
    assert value.endswith("#long%2Fstripe%2Ffragment")


def test_success_flow():
    transport, calls = _success_transport()
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None)
    assert result["ok"] is True
    assert result["provider"] == "pix"
    assert result["link"] == "https://payments.stripe.com/pix/fixture"
    assert result["qr_data"] == "000201fixture"
    assert any(path.endswith("/confirm") for _, path in calls)
    assert any(path.endswith("/approve") for _, path in calls)
    chat = [h for h in transport.observed_headers if h.get("origin") == "https://chatgpt.com"]
    stripe = [h for h in transport.observed_headers if h.get("origin") == "https://js.stripe.com"]
    assert chat and stripe
    assert len({h.get("oai-session-id") for h in chat}) == 1
    assert len({h.get("user-agent") for h in transport.observed_headers}) == 1
    assert all(h["accept"] == "application/json" for h in stripe)
    assert all(h["content-type"] == "application/x-www-form-urlencoded" for h in stripe)
    confirm_forms = [f for f in transport.observed_forms if f.get("expected_payment_method_type") == "pix"]
    assert confirm_forms and confirm_forms[0]["payment_method_data[billing_details][tax_id]"] == TAX_ID


def test_decline_is_terminal_and_structured():
    transport, _ = _success_transport(fail_confirm=True)
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None)
    assert result["ok"] is False
    assert result["reason_code"] == "pix_payment_setup_declined"
    assert result["retryable"] is False
    assert "generic_decline" in result["error"]
    assert classify_error("payment_failed_generic_decline") == ("pix_payment_setup_declined", False)
    assert classify_error("payment_method_provider_decline; partner_generic_decline") == ("pix_payment_setup_declined", False)
    assert classify_error("parameter_invalid_empty; param=foo") == ("pix_stripe_parameter_error", False)
    assert classify_error("risk_blocked") == ("pix_risk_blocked", False)


def test_promo_not_applied_user_message_is_account_eligibility():
    message = localize_pix_error("PIX promo_not_applied: amount=9990")
    assert "优惠未应用" in message or "优惠" in message


def test_generate_valid_cpf_and_cnpj():
    for _ in range(64):
        cpf = generate_valid_cpf()
        assert len(cpf) == 11 and cpf.isdigit()
        assert is_valid_cpf(cpf)
        cnpj = generate_valid_cnpj()
        assert len(cnpj) == 14 and cnpj.isdigit()
        assert is_valid_cnpj(cnpj)
    assert is_valid_cpf("52998224725")  # well-known valid example
    assert not is_valid_cpf("52998224724")
    assert not is_valid_cpf("11111111111")
    assert not is_valid_cnpj("11111111111111")


def test_random_brazil_billing_cnpj_uses_real_pool():
    pool_ids = {str(entry["tax_id"]) for entry in _BR_REAL_CNPJ_POOL}
    assert len(pool_ids) >= 5
    for _ in range(32):
        profile = random_brazil_billing(tax_kind="cnpj")
        assert profile["country"] == "BR"
        assert profile["tax_id"] in pool_ids
        assert is_valid_cnpj(profile["tax_id"])
        assert profile["name"] and profile["line1"] and profile["city"] and profile["state"]
        assert profile["postal_code"]
        assert "@" in profile["email"]
    cpf_profile = random_brazil_billing(tax_kind="cpf")
    assert is_valid_cpf(cpf_profile["tax_id"])
    assert len(cpf_profile["tax_id"]) == 11


def test_normalize_billing_auto_generates_real_cnpj():
    previous = os.environ.pop("PIX_TAX_ID", None)
    try:
        billing = _normalize_billing({"email": "buyer@example.com"}, "buyer@example.com")
    finally:
        if previous is not None:
            os.environ["PIX_TAX_ID"] = previous
    assert billing["country"] == "BR"
    assert billing["email"] == "buyer@example.com"
    assert billing["name"] and billing["line1"] and billing["city"] and billing["state"]
    assert billing["postal_code"]
    # default tax kind resolves to the real registered CNPJ pool
    assert billing["tax_id"] in {str(entry["tax_id"]) for entry in _BR_REAL_CNPJ_POOL}
    assert is_valid_cnpj(billing["tax_id"])


def test_normalize_billing_keeps_explicit_values():
    billing = _normalize_billing(
        {"name": "Ana Souza", "line1": "Rua Chile 15", "city": "Salvador", "state": "BA",
         "postal_code": "40020-000", "email": "ana@example.com", "tax_id": TAX_ID},
        "other@example.com",
    )
    assert billing["name"] == "Ana Souza"
    assert billing["line1"] == "Rua Chile 15"
    assert billing["state"] == "BA"
    assert billing["email"] == "ana@example.com"
    assert billing["tax_id"] == TAX_ID


def test_missing_tax_id_generates_real_cnpj_before_network():
    transport, calls = _success_transport()
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com"}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None)
    assert result["ok"] is True
    tax_id = (result.get("billing") or {}).get("tax_id") or result.get("tax_id") or ""
    assert tax_id in {str(entry["tax_id"]) for entry in _BR_REAL_CNPJ_POOL}
    assert is_valid_cnpj(tax_id)
    confirm_forms = [f for f in transport.observed_forms if f.get("expected_payment_method_type") == "pix"]
    assert confirm_forms and confirm_forms[0]["payment_method_data[billing_details][tax_id]"] == tax_id
    assert result["billing"]["country"] == "BR"


def test_has_active_plus():
    assert _has_active_plus({"plan_type": "plus", "is_delinquent": False, "seats_entitled": 1}) is True
    assert _has_active_plus({"data": [{"plan_type": "plus", "seats_entitled": 1}]}) is True
    assert _has_active_plus({"data": [{"plan_type": "free", "seats_entitled": 1}]}) is False
    assert _has_active_plus({"data": []}) is False
    assert _has_active_plus({"plan_type": "plus", "is_delinquent": True, "seats_entitled": 1}) is False


def test_wait_paid_flags_result_without_watch_when_disabled():
    # Without wait_paid the result carries no watching marker (default path).
    transport, _ = _success_transport()
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None)
    assert result["ok"] is True
    assert result.get("paid_confirmed_via", "") == ""


def _payment_pages_transport(payment_payload: dict):
    """Transport whose payment_pages poll returns a custom authoritative payload."""
    calls: list[tuple[str, str]] = []
    session = "cs_live_pix_strict"
    qr = {
        "hosted_instructions_url": "https://payments.stripe.com/pix/strict",
        "image_url_png": "https://files.stripe.com/pix/strict.png",
        "data": "000201strict",
        "expires_at": 1780000000,
    }

    def response(payload, status=200):
        return status, json.dumps(payload), {"content-type": "application/json"}

    def transport(method, url, headers, json_body, form, timeout, allow_redirects, proxy):
        path = urlparse(url).path
        calls.append((method, path))
        if path.endswith("/") and "promo_campaign" in url:
            return 200, "<html></html>", {}
        if path.endswith("/backend-api/payments/checkout"):
            return response({"checkout_session_id": session, "publishable_key": "pk_test_fixture", "client_secret": "cs_secret_fixture", "processor_entity": "openai_llc", "checkout_ui_mode": "custom"})
        if path.endswith("/init"):
            return response({"config_id": "checkout_cfg", "init_checksum": "checksum", "version": "b0f5e7abe5", "amount": 0, "payment_method_types": ["card", "link", "pix"]})
        if path == "/v1/elements/sessions":
            return response({"session_id": "elements_session_fixture", "config_id": "elements_cfg", "ordered_payment_method_types_and_wallets": ["card", "pix"]})
        if path.endswith("/backend-api/payments/checkout/taxes"):
            return response({"checkout_session": {"amount_total": 0, "currency": "brl"}})
        if path.endswith("/backend-api/payments/checkout/snapshot"):
            return 204, "", {}
        if path.endswith("/confirm"):
            return response({"submission_attempt": {"state": "requires_approval"}})
        if path.endswith("/backend-api/payments/checkout/approve"):
            return response({"result": "approved"})
        if path == f"/v1/payment_pages/{session}":
            payload = dict(payment_payload)
            payload.setdefault("submission_attempt", {"state": "requires_action"})
            payload.setdefault("next_action", {"pix_display_qr_code": qr})
            return response(payload)
        raise AssertionError(f"unexpected request: {method} {url}")

    transport.observed_forms = []
    return transport, calls


def test_strict_zero_rejects_nonzero_payment_intent():
    # 优惠未真正压到 0：Stripe 权威应付金额是 9990 → 必须拒绝输出链接
    transport, _ = _payment_pages_transport({
        "amount_total": 9990, "amount_subtotal": 9990,
        "invoice": {"amount_due": 9990, "amount_paid": 0, "total": 9990},
        "total_summary": {"due": 9990, "total": 9990},
        "payment_intent": {"amount": 9990, "amount_received": 0, "status": "requires_payment_method"},
    })
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None)
    assert result["ok"] is False
    assert result["reason_code"] == "pix_promo_not_applied"
    assert result["link"] == ""
    assert "9990" in str(result.get("extra") or {}) or "9990" in str(result.get("error") or "")


def test_strict_zero_accepts_zero_payment_intent():
    # 优惠已压到 0：amount_total 还是原价但应付金额为 0 → 允许输出
    transport, _ = _payment_pages_transport({
        "amount_total": 9990, "amount_subtotal": 9990,
        "invoice": {"amount_due": 0, "amount_paid": 0, "total": 0},
        "total_summary": {"due": 0, "total": 0},
        "payment_intent": {"amount": 0, "amount_received": 0, "status": "requires_payment_method"},
    })
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None)
    assert result["ok"] is True
    assert result["zero_verified"] is True
    assert result["payable_amount"] == 0


def test_strict_zero_rejects_qr_with_amount():
    # 二维码 tag54 编码了金额（99.90）→ 即使 Stripe 字段缺失也要拒绝
    transport, _ = _payment_pages_transport({
        "submission_attempt": {"state": "requires_action"},
    })
    # 覆盖 poll 的 qr_data 为带金额的 PIX 码（tag54=99.90）
    _guid = "0014br.gov.bcb.pix0114099888776655215"
    qr_paid = "00020126" + str(len(_guid)).zfill(2) + _guid + "520400005303986540599.905802BR5912FULANO DE TAL"
    assert _pix_qr_amount(qr_paid) == 9990

    def transport(method, url, headers, json_body, form, timeout, allow_redirects, proxy):
        path = urlparse(url).path
        if path.endswith("/") and "promo_campaign" in url:
            return 200, "<html></html>", {}
        if path.endswith("/backend-api/payments/checkout"):
            return 200, json.dumps({"checkout_session_id": "cs_qr", "publishable_key": "pk_fixture", "client_secret": "cs_secret", "processor_entity": "openai_llc", "checkout_ui_mode": "custom"}), {}
        if path.endswith("/init"):
            return 200, json.dumps({"config_id": "cfg", "amount": 0, "payment_method_types": ["card", "pix"]}), {}
        if path == "/v1/elements/sessions":
            return 200, json.dumps({"session_id": "es", "config_id": "ecfg", "ordered_payment_method_types_and_wallets": ["card", "pix"]}), {}
        if path.endswith("/backend-api/payments/checkout/taxes"):
            return 200, json.dumps({"checkout_session": {"amount_total": 0, "currency": "brl"}}), {}
        if path.endswith("/backend-api/payments/checkout/snapshot"):
            return 204, "", {}
        if path.endswith("/confirm"):
            return 200, json.dumps({"submission_attempt": {"state": "requires_approval"}}), {}
        if path.endswith("/backend-api/payments/checkout/approve"):
            return 200, json.dumps({"result": "approved"}), {}
        if path == "/v1/payment_pages/cs_qr":
            return 200, json.dumps({
                "submission_attempt": {"state": "requires_action"},
                "next_action": {"pix_display_qr_code": {"hosted_instructions_url": "https://payments.stripe.com/pix/x", "data": qr_paid, "expires_at": 1780000000}},
            }), {}
        raise AssertionError(f"unexpected: {method} {url}")

    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None)
    assert result["ok"] is False
    assert result["reason_code"] == "pix_promo_not_applied"
    # 二维码 tag54 明确编码了 99.90 → 错误信息里应带出该金额
    assert "9990" in str(result.get("error") or "")



# ─────────────────────────────────────────────────────────────────────────────
# OA confirm-token 链路（bax.har 2026-09 对齐）：elements/sessions(cuss) →
# taxes → confirmation_tokens(ctoken) → /payments/checkout/confirm(JSON)。
# ─────────────────────────────────────────────────────────────────────────────
OA_CS_ID = "oaics_live_pix_oa_fixture"
OA_VERIFY_URL = f"https://chatgpt.com/checkout/verify?stripe_session_id={OA_CS_ID}&processor_entity=openai_llc&plan_type=plus"


def _oa_transport(confirm_response, *, checkout_extra=None, ctoken_status=None, ctoken_body=None, legacy_fallback=False, confirm_status=200, confirm_body=None):
    """Transport implementing both OA protocols on the same checkout session.

    token endpoints: elements/sessions(cuss), confirmation_tokens,
    /backend-api/payments/checkout/confirm.
    legacy endpoints (legacy_fallback=True): payment_pages init/confirm,
    approve, payment_pages poll — used to prove the auto fallback.
    """
    calls: list[str] = []
    forms: list[dict] = []
    json_bodies: list[dict] = []
    queries: list[str] = []

    def response(payload, status=200):
        return status, json.dumps(payload), {"content-type": "application/json"}

    qr = {"hosted_instructions_url": "https://payments.stripe.com/pix/oa", "image_url_png": "https://files.stripe.com/pix/oa.png", "data": "000201oa", "expires_at": 1780000000}

    def transport(method, url, headers, json_body, form, timeout, allow_redirects, proxy):
        path = urlparse(url).path
        calls.append(f"{method} {path}")
        forms.append(dict(form or {}))
        if json_body is not None:
            json_bodies.append(dict(json_body))
        queries.append(url)
        if path.endswith("/") and "promo_campaign" in url:
            return 200, "<html></html>", {}
        if path.endswith("/backend-api/payments/checkout") and "/confirm" not in path and "/taxes" not in path and "/approve" not in path:
            payload = {
                "tag": "custom_checkout_session",
                "checkout_session_id": OA_CS_ID,
                "publishable_key": "pk_live_fixture",
                "processor_entity": "openai_llc",
                "checkout_ui_mode": "custom",
                "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
                "plan_name": "chatgptplusplan",
                "requires_manual_approval": False,
                "customer_session_client_secret": "cuss_secret_oa1",
                "payment_method_types": ["card", "link", "pix"],
                "confirm_return_url": OA_VERIFY_URL,
                "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
            }
            if isinstance(checkout_extra, dict):
                payload.update(checkout_extra)
            return response(payload)
        if path == "/v1/elements/sessions":
            return response({"session_id": "elements_session_oa1", "config_id": "71703d86-2ef7-4f74-9192-e90325d5bd3f", "customer": {"customer": "cus_oa1"}, "ordered_payment_method_types_and_wallets": ["card", "pix"]})
        if path.endswith("/backend-api/payments/checkout/taxes"):
            return response({"using_automatic_tax": False, "checkout_session": {"amount_total": 0, "currency": "brl"}, "customer": "cus_oa1"})
        if path == "/v1/confirmation_tokens":
            if ctoken_status is not None:
                body = ctoken_body if isinstance(ctoken_body, dict) else {"error": {"code": "resource_missing", "message": "endpoint removed"}}
                return ctoken_status, json.dumps(body), {"content-type": "application/json"}
            return response({"id": "ctoken_oa1", "object": "confirmation_token", "payment_method_preview": {"type": "pix"}})
        if path.endswith("/backend-api/payments/checkout/confirm"):
            if confirm_status >= 400:
                body = confirm_body if isinstance(confirm_body, dict) else {"detail": "internal server error"}
                return confirm_status, json.dumps(body), {"content-type": "application/json"}
            return response(confirm_response)
        if legacy_fallback:
            if path.endswith("/init"):
                return response({"config_id": "checkout_cfg", "init_checksum": "checksum", "version": "b0f5e7abe5", "amount": 0, "payment_method_types": ["card", "link", "pix"]})
            if path.endswith("/backend-api/payments/checkout/snapshot"):
                return 204, "", {}
            if path.endswith("/v1/payment_pages/" + OA_CS_ID + "/confirm"):
                return response({"submission_attempt": {"state": "requires_approval"}})
            if path.endswith("/backend-api/payments/checkout/approve"):
                return response({"result": "approved"})
            if path == f"/v1/payment_pages/{OA_CS_ID}":
                # POST = tax-region update (legacy), GET = payment poll
                if method == "POST":
                    return response({"config_id": "checkout_cfg"})
                return response({"submission_attempt": {"state": "requires_action"}, "next_action": {"pix_display_qr_code": qr}})
        raise AssertionError(f"unexpected request: {method} {url}")

    transport.calls = calls
    transport.forms = forms
    transport.json_bodies = json_bodies
    transport.queries = queries
    return transport


def test_oa_confirm_token_success_flow():
    transport = _oa_transport({"status": "succeeded"})
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "name": "Joao Gomes", "line1": "198 Mill Road", "city": "Salvador", "state": "BA", "postal_code": "40020-160", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None, oa_confirm_protocol="auto")
    assert result["ok"] is True
    assert result["link"] == OA_VERIFY_URL
    assert result["confirm_protocol"] == "token"
    assert result["paid"] is True
    assert result["zero_verified"] is True
    expected = [
        "POST /backend-api/payments/checkout",
        "GET /v1/elements/sessions",
        "POST /backend-api/payments/checkout/taxes",
        "POST /v1/confirmation_tokens",
        "POST /backend-api/payments/checkout/confirm",
    ]
    assert transport.calls == expected, transport.calls
    # elements/sessions carries the customer session secret (bax.har 对齐)
    assert any("customer_session_client_secret=cuss_secret_oa1" in q for q in transport.queries)
    assert any("deferred_intent%5Bamount%5D=0" in q or "deferred_intent[amount]=0" in q for q in transport.queries)
    ctoken_forms = [f for f in transport.forms if f.get("payment_method_data[type]") == "pix"]
    assert ctoken_forms
    form = ctoken_forms[0]
    assert form["payment_method_data[billing_details][tax_id]"] == TAX_ID
    assert form["payment_method_data[billing_details][phone]"] == ""
    assert form["setup_future_usage"] == "off_session"
    assert form["client_context[customer]"] == "cus_oa1"
    assert form["key"] == "pk_live_fixture"
    assert form["_stripe_version"] == "2025-03-31.basil"
    assert form["client_attribution_metadata[client_session_id]"]
    assert form["payment_method_data[client_attribution_metadata][elements_session_id]"] == "elements_session_oa1"
    confirms = [b for b in transport.json_bodies if b.get("selected_payment_method_type") == "pix"]
    assert confirms and confirms[0]["confirm_token"] == "ctoken_oa1"
    assert confirms[0]["checkout_session_id"] == OA_CS_ID
    # confirm-token 链路绝不调用旧 payment_pages / approve
    assert not any("/init" in c or "/approve" in c or "/payment_pages/" in c for c in transport.calls)


def test_oa_confirm_token_qr_next_action():
    qr = {"next_action": {"pix_display_qr_code": {"hosted_instructions_url": "https://payments.stripe.com/pix/oa-qr", "image_url_png": "https://files.stripe.com/pix/oa-qr.png", "data": "000201oa-qr", "expires_at": 1780000000}}}
    transport = _oa_transport({"status": "requires_action", "submission_attempt": {"state": "requires_action"}, **qr})
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None, oa_confirm_protocol="token")
    assert result["ok"] is True
    assert result["link"] == "https://payments.stripe.com/pix/oa-qr"
    assert result["qr_data"] == "000201oa-qr"
    assert result["confirm_protocol"] == "token"
    assert result["zero_verified"] is True


def test_oa_confirm_blocked_manual_approval_is_terminal():
    transport = _oa_transport({"status": "blocked"}, checkout_extra={"requires_manual_approval": True})
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None, oa_confirm_protocol="auto")
    assert result["ok"] is False
    assert result["reason_code"] == "pix_checkout_manual_approval"
    assert result["retryable"] is False
    assert result["link"] == ""
    # 面向用户文案如实呈现上游返回 blocked，不做“人工审核拦截”推断
    assert "blocked" in result["error"]
    assert "人工审核" not in result["error"]
    assert "{\"status\":\"blocked\"}" in result["error"]
    # blocked 是业务终态，不允许协议回退重跑
    assert not any("/init" in c for c in transport.calls)


def test_oa_confirm_contract_error_falls_back_to_legacy():
    # confirmation_tokens 端点返回 404 → token 链路契约错误 → auto 回退 legacy
    # payment_pages 链路（同一次 checkout session）并成功出码。
    transport = _oa_transport({"status": "succeeded"}, ctoken_status=404, legacy_fallback=True)
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None, oa_confirm_protocol="auto")
    assert result["ok"] is True
    assert result["link"] == "https://payments.stripe.com/pix/oa"
    assert result["confirm_protocol"] == "legacy"
    assert any("/v1/confirmation_tokens" in c for c in transport.calls)
    assert any(c.endswith("/init") for c in transport.calls)
    assert any(c.endswith("/v1/payment_pages/" + OA_CS_ID + "/confirm") for c in transport.calls)


def test_oa_confirm_internal_only_endpoint_falls_back_to_legacy():
    # 2026-09 实测：OpenAI 将 /backend-api/payments/checkout/confirm 限制为仅
    # 内部 checkout 会话（400 "This endpoint is only for internal checkout
    # sessions"）。auto 模式必须把它当契约错误回退 legacy，而不是直接失败。
    transport = _oa_transport({"status": "succeeded"}, confirm_status=400,
                              confirm_body={"detail": "This endpoint is only for internal checkout sessions"},
                              legacy_fallback=True)
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None, oa_confirm_protocol="auto")
    assert result["ok"] is True
    assert result["link"] == "https://payments.stripe.com/pix/oa"
    assert result["confirm_protocol"] == "legacy"
    assert any(c.endswith("/backend-api/payments/checkout/confirm") for c in transport.calls)
    assert any(c.endswith("/v1/payment_pages/" + OA_CS_ID + "/confirm") for c in transport.calls)


def test_oa_confirm_internal_only_endpoint_is_terminal_without_fallback():
    # 显式 token 协议且无 legacy 回退时，该 400 仍以原错误返回（不回退、不掩盖）。
    transport = _oa_transport({"status": "succeeded"}, confirm_status=400,
                              confirm_body={"detail": "This endpoint is only for internal checkout sessions"})
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None, oa_confirm_protocol="token")
    assert result["ok"] is False
    assert "internal checkout sessions" in str(result.get("error") or "")
    assert not any(c.endswith("/init") for c in transport.calls)


def test_oa_confirm_explicit_legacy_skips_ctoken():
    transport = _oa_transport({"status": "succeeded"}, legacy_fallback=True)
    result = extract_pix_link(TOKEN, billing={"email": "buyer@example.com", "tax_id": TAX_ID}, transport=transport, max_poll_attempts=1, sleep_fn=lambda _: None, oa_confirm_protocol="legacy")
    assert result["ok"] is True
    assert result["confirm_protocol"] == "legacy"
    assert not any("/v1/confirmation_tokens" in c for c in transport.calls)


def test_oa_blocked_classification():
    assert classify_error("PIX OA confirm blocked by upstream: status=blocked; account requires manual approval") == ("pix_checkout_manual_approval", False)
    assert classify_error("PIX OA confirm blocked by upstream: status=blocked") == ("pix_checkout_blocked", False)
    message = localize_pix_error("PIX OA confirm blocked by upstream: status=blocked")
    assert "blocked" in message
    assert "人工审核" not in message and "拦截" not in message
    message2 = localize_pix_error("PIX OA confirm blocked by upstream: status=blocked; account requires manual approval; upstream={\"status\":\"blocked\"}")
    # 面向用户文案只如实呈现上游返回 blocked，不做“人工审核拦截”推断
    assert "blocked" in message2
    assert "人工审核" not in message2 and "OpenAI 人工" not in message2
    assert "上游返回" in message2
    assert "{\"status\":\"blocked\"}" in message2



def test_normalize_billing_prefers_user_profile_pool(tmpdir=None):
    # 素材池文件优先于内置随机 CNPJ 池；显式 billing 仍最高优先
    import tempfile as _tf
    pool_path = os.path.join(_tf.gettempdir(), "pix_billing_profiles_test.json")
    with open(pool_path, "w", encoding="utf-8") as handle:
        json.dump([{
            "name": "Comercio Local LTDA", "tax_id": "11222333000181",
            "line1": "Rua Teste 10", "city": "Curitiba", "state": "PR", "postal_code": "80020-000",
        }], handle)
    previous = os.environ.get("PIX_BILLING_PROFILES")
    try:
        os.environ["PIX_BILLING_PROFILES"] = pool_path
        profiles = _load_billing_profiles()
        assert len(profiles) == 1 and profiles[0]["tax_id"] == "11222333000181"
        billing = _normalize_billing({"email": "buyer@example.com"}, "buyer@example.com")
        assert billing["name"] == "Comercio Local LTDA"
        assert billing["line1"] == "Rua Teste 10"
        assert billing["city"] == "Curitiba"
        # 显式 billing 覆盖素材池
        explicit = _normalize_billing({"name": "Ana Souza", "line1": "Rua Chile 15", "city": "Salvador",
                                       "state": "BA", "postal_code": "40020-000", "email": "ana@example.com",
                                       "tax_id": TAX_ID}, "ana@example.com")
        assert explicit["name"] == "Ana Souza"
        assert explicit["tax_id"] == TAX_ID
        # 不完整素材条目被过滤
        with open(pool_path, "w", encoding="utf-8") as handle:
            json.dump([{"name": "Incomplete", "tax_id": "11222333000181"}], handle)
        assert _load_billing_profiles() == []
    finally:
        try:
            os.unlink(pool_path)
        except OSError:
            pass
        if previous is None:
            os.environ.pop("PIX_BILLING_PROFILES", None)
        else:
            os.environ["PIX_BILLING_PROFILES"] = previous

if __name__ == "__main__":
    test_qr_parser()
    test_confirm_keeps_elements_context()
    test_stripe_init_omits_empty_elements_session_id()
    test_fingerprint_aliases_and_stripe_headers()
    test_init_parameter_negotiation_is_narrow()
    test_return_url_uses_secret_fragment()
    test_return_url_prefers_init_hosted_url_fragment()
    test_success_flow()
    test_decline_is_terminal_and_structured()
    test_promo_not_applied_user_message_is_account_eligibility()
    test_generate_valid_cpf_and_cnpj()
    test_random_brazil_billing_cnpj_uses_real_pool()
    test_normalize_billing_auto_generates_real_cnpj()
    test_normalize_billing_keeps_explicit_values()
    test_missing_tax_id_generates_real_cnpj_before_network()
    test_has_active_plus()
    test_wait_paid_flags_result_without_watch_when_disabled()
    test_strict_zero_rejects_nonzero_payment_intent()
    test_strict_zero_accepts_zero_payment_intent()
    test_strict_zero_rejects_qr_with_amount()
    test_oa_confirm_token_success_flow()
    test_oa_confirm_token_qr_next_action()
    test_oa_confirm_blocked_manual_approval_is_terminal()
    test_oa_confirm_contract_error_falls_back_to_legacy()
    test_oa_confirm_internal_only_endpoint_falls_back_to_legacy()
    test_oa_confirm_internal_only_endpoint_is_terminal_without_fallback()
    test_oa_confirm_explicit_legacy_skips_ctoken()
    test_oa_blocked_classification()
    test_normalize_billing_prefers_user_profile_pool()
    print("pix_service tests: ok")
