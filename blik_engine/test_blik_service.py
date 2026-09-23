import json
import unittest

import blik_service
from blik_service import (
    BlikEligibilityError,
    _CHATGPT_CLIENT_BUILD,
    _CHATGPT_CLIENT_VERSION,
    _approve_checkout,
    _blik_response_state,
    _checkout_diagnostic,
    _checkout_is_paid,
    _checkout_verify_url,
    _client_identity,
    _blik_state_detail,
    _is_editable_blik_input,
    _normalize_sentinel_headers,
    _stripe_session_id,
    _valid_client_observation,
    _validate_checkout_eligibility,
)


class _FakeLocator:
    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata

    def evaluate(self, _script: str) -> dict:
        return self.metadata


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    """Records the single approve POST so the HAR header contract can be asserted."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        return _FakeResponse({"result": "approved"})


def _lowered(headers: dict) -> dict:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _payload() -> dict:
    return {
        "currency": "pln",
        "payment_method_types": ["card", "link", "blik"],
        "total_summary": {
            "due": 0,
            "subtotal": 8049,
            "total": 0,
            "total_discount_amount_aggregate": 8049,
            "total_discount_amounts": [{
                "amount": 8049,
                "coupon": {
                    "duration": "repeating",
                    "duration_in_months": 1,
                    "name": "ChatGPT Plus - 1 Month Free Trial",
                    "percent_off": 100,
                },
                "currency": "pln",
                "intervals": 1,
            }],
        },
    }


def _reason(payload: dict) -> str:
    try:
        _validate_checkout_eligibility(payload)
    except BlikEligibilityError as exc:
        assert exc.retryable is False
        return exc.reason_code
    raise AssertionError("expected BlikEligibilityError")


class BlikEligibilityTests(unittest.TestCase):
    def test_accepts_har_zero_pln_blik_free_trial(self) -> None:
        result = _validate_checkout_eligibility(_payload())
        self.assertEqual(result["amount"], 0)
        self.assertEqual(result["currency"], "PLN")
        self.assertIs(result["promo_applied"], True)
        self.assertIs(result["promo_eligible"], True)
        self.assertEqual(result["promo_duration_months"], 1)

    def test_rejects_missing_blik_before_waiting_for_code(self) -> None:
        payload = _payload()
        payload["payment_method_types"] = ["card", "link"]
        payload["ordered_payment_method_types"] = ["blik", "card", "link"]
        self.assertEqual(_reason(payload), "blik_method_unavailable")

    def test_rejects_unknown_or_nonzero_due(self) -> None:
        payload = _payload()
        del payload["total_summary"]["due"]
        self.assertEqual(_reason(payload), "blik_amount_unknown")

        payload = _payload()
        payload["total_summary"]["due"] = 8049
        self.assertEqual(_reason(payload), "blik_not_zero_due")

    def test_rejects_non_pln_checkout(self) -> None:
        payload = _payload()
        payload["currency"] = "eur"
        self.assertEqual(_reason(payload), "blik_currency_mismatch")

    def test_rejects_ineligible_or_unapplied_trial(self) -> None:
        payload = _payload()
        payload["total_summary"]["total_discount_amounts"] = []
        self.assertEqual(_reason(payload), "blik_promo_ineligible")

        payload = _payload()
        payload["total_summary"]["total_discount_amount_aggregate"] = 1
        self.assertEqual(_reason(payload), "blik_promo_not_applied")

        payload = _payload()
        payload["total_summary"]["total_discount_amounts"][0]["amount"] = 1
        self.assertEqual(_reason(payload), "blik_promo_not_applied")

    def test_blik_code_field_rejects_radio_and_accepts_text_input(self) -> None:
        self.assertFalse(_is_editable_blik_input(_FakeLocator({
            "tag": "input", "type": "radio", "readOnly": False, "disabled": False,
        })))
        self.assertTrue(_is_editable_blik_input(_FakeLocator({
            "tag": "input", "type": "text", "readOnly": False, "disabled": False,
        })))
        self.assertFalse(_is_editable_blik_input(_FakeLocator({
            "tag": "input", "type": "text", "readOnly": True, "disabled": False,
        })))

    def test_parses_har_confirmation_states(self) -> None:
        self.assertEqual(_blik_response_state({
            "status": "open",
            "submission_attempt": {"state": "requires_approval", "error": None},
        })["submission"], "requires_approval")
        self.assertEqual(_blik_response_state({
            "setup_intent": {"status": "requires_action", "last_setup_error": None},
        })["status"], "requires_action")
        declined = _blik_response_state({
            "payment_object_status": "requires_payment_method",
            "last_setup_error": {
                "code": "payment_method_not_available",
                "decline_code": "generic_decline",
                "message": "The payment has failed for an unknown reason.",
            },
        })
        self.assertEqual(declined["status"], "requires_payment_method")
        self.assertEqual(declined["error"], "payment_method_not_available")
        self.assertEqual(declined["decline_code"], "generic_decline")
        self.assertIn("unknown reason", declined["error_message"])
        self.assertEqual(
            _blik_state_detail(declined),
            "payment_method_not_available: generic_decline: The payment has failed for an unknown reason.",
        )

    def test_requires_authoritative_paid_checkout_state(self) -> None:
        self.assertTrue(_checkout_is_paid({"status": "complete", "payment_status": "paid"}))
        self.assertFalse(_checkout_is_paid({"status": "open", "payment_status": "paid"}))
        self.assertFalse(_checkout_is_paid({"status": "complete", "payment_status": "unpaid"}))

    def test_accepts_only_signed_observation_shape(self) -> None:
        self.assertTrue(_valid_client_observation("v1.r.p.NyKJx52V61RzNji8"))
        self.assertFalse(_valid_client_observation("true"))
        self.assertFalse(_valid_client_observation(""))

    def test_splits_session_observer_from_main_sentinel_header(self) -> None:
        headers = _normalize_sentinel_headers({
            "openai-sentinel-token": json.dumps({
                "p": "proof", "c": "challenge", "id": "device", "flow": "checkout_session_approval",
                "_so": {"so": "observer", "c": "challenge"},
            }),
        })
        self.assertNotIn("_so", json.loads(headers["openai-sentinel-token"]))
        self.assertEqual(json.loads(headers["OpenAI-Sentinel-SO-Token"])["so"], "observer")


class BlikHarContractTests(unittest.TestCase):
    """Locks the wire contract observed in the 2026-09-09 BLIK success HAR."""

    def test_client_identity_is_a_deployment_identity(self) -> None:
        identity = _client_identity()
        self.assertEqual(identity["oai-client-version"], _CHATGPT_CLIENT_VERSION)
        self.assertEqual(identity["oai-client-build-number"], _CHATGPT_CLIENT_BUILD)
        # The HAR shows oai-client-version == "prod-" + attestation.deployId.
        self.assertRegex(_CHATGPT_CLIENT_VERSION, r"^prod-[0-9a-f]{40}$")
        self.assertRegex(_CHATGPT_CLIENT_BUILD, r"^\d+$")
        # A fresh dict each call, so callers cannot mutate the shared default.
        identity["oai-client-version"] = "mutated"
        self.assertEqual(_client_identity()["oai-client-version"], _CHATGPT_CLIENT_VERSION)

    def test_checkout_verify_url_matches_the_har_return_url(self) -> None:
        self.assertEqual(
            _checkout_verify_url({"id": "cs_live_abc", "entity": "openai_ie"}),
            "https://chatgpt.com/checkout/verify?stripe_session_id=cs_live_abc"
            "&processor_entity=openai_ie&plan_type=plus",
        )

    def test_approve_request_matches_the_har_header_contract(self) -> None:
        session = _FakeSession()
        original = blik_service._approval_risk_headers
        blik_service._approval_risk_headers = lambda *args, **kwargs: {
            "openai-sentinel-token": json.dumps({
                "p": "proof", "c": "challenge", "id": "device-1",
                "flow": "checkout_session_approval",
            }),
            "OpenAI-Sentinel-SO-Token": json.dumps({"so": "observer"}),
        }
        try:
            approval, error = _approve_checkout(
                session,
                {
                    "id": "cs_live_abc",
                    "entity": "openai_ie",
                    "url": "https://chatgpt.com/checkout/openai_ie/cs_live_abc",
                    "_checkout_risk": {"oai-web-deployment-attestation": "attestation-token"},
                },
                "a.b.c",
                "http://proxy.invalid:8080",
                {"device_id": "device-1", "session_seed": "seed-1"},
                "oai-did=device-1",
                30.0,
                browser_page=None,
                browser_risk={},
            )
        finally:
            blik_service._approval_risk_headers = original

        self.assertEqual(error, "")
        self.assertEqual(approval, "approved")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(
            call["json"],
            {"checkout_session_id": "cs_live_abc", "processor_entity": "openai_ie"},
        )
        headers = _lowered(call["headers"])
        # Sentinel, session observer and the deployment attestation all travel.
        self.assertTrue(headers["openai-sentinel-token"])
        self.assertTrue(headers["openai-sentinel-so-token"])
        self.assertEqual(headers["oai-web-deployment-attestation"], "attestation-token")
        # The deployment identity must match the attestation's deploy id.
        self.assertEqual(headers["oai-client-version"], _CHATGPT_CLIENT_VERSION)
        self.assertEqual(headers["oai-client-build-number"], _CHATGPT_CLIENT_BUILD)
        # Nothing was captured for this run, so the signed observation proof
        # must stay absent rather than being fabricated with a placeholder.
        self.assertNotIn("x-oai-is-client-observation", headers)

    def test_approve_forwards_only_a_captured_observation_proof(self) -> None:
        def captured(value: str) -> dict:
            session = _FakeSession()
            original = blik_service._approval_risk_headers
            blik_service._approval_risk_headers = lambda *args, **kwargs: {
                "openai-sentinel-token": json.dumps({
                    "p": "proof", "c": "challenge", "id": "device-1",
                    "flow": "checkout_session_approval",
                }),
            }
            try:
                _approve_checkout(
                    session,
                    {
                        "id": "cs_live_abc",
                        "entity": "openai_ie",
                        "url": "https://chatgpt.com/checkout/openai_ie/cs_live_abc",
                    },
                    "a.b.c",
                    "http://proxy.invalid:8080",
                    {"device_id": "device-1", "session_seed": "seed-1"},
                    "oai-did=device-1",
                    30.0,
                    browser_page=None,
                    browser_risk={"observation": value},
                )
            finally:
                blik_service._approval_risk_headers = original
            return _lowered(session.calls[0]["headers"])

        # HAR entry 64 (approve) sends the observation proof minted by the
        # session observer, so a genuinely captured value must travel.
        self.assertEqual(
            captured("v1.r.p.NyKJx52V61RzNji8").get("x-oai-is-client-observation"),
            "v1.r.p.NyKJx52V61RzNji8",
        )
        # Placeholders such as "true" are not a signed proof and must be dropped.
        self.assertNotIn("x-oai-is-client-observation", captured("true"))

    def test_approve_blocked_is_terminal_and_never_auto_retried(self) -> None:
        # The 2026-09-09 HAR analysis and the BLIK contract document both state
        # that approve.result=blocked is a business/risk terminal state. It must
        # be surfaced immediately instead of being replayed with a fresh token.
        session = _FakeSession()
        session.post = lambda url, json=None, headers=None, timeout=None: (
            session.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
            or _FakeResponse({"result": "blocked"})
        )
        original = blik_service._approval_risk_headers
        blik_service._approval_risk_headers = lambda *args, **kwargs: {
            "openai-sentinel-token": json.dumps({
                "p": "proof", "c": "challenge", "id": "device-1",
                "flow": "checkout_session_approval",
            }),
            "OpenAI-Sentinel-SO-Token": json.dumps({"so": "observer"}),
        }
        try:
            approval, error = _approve_checkout(
                session,
                {
                    "id": "cs_live_abc",
                    "entity": "openai_ie",
                    "url": "https://chatgpt.com/checkout/openai_ie/cs_live_abc",
                    "_checkout_risk": {"oai-web-deployment-attestation": "attestation-token"},
                },
                "a.b.c",
                "http://proxy.invalid:8080",
                {"device_id": "device-1", "session_seed": "seed-1"},
                "oai-did=device-1",
                30.0,
                browser_page=None,
                browser_risk={},
            )
        finally:
            blik_service._approval_risk_headers = original

        self.assertEqual(approval, "blocked")
        self.assertEqual(error, "")
        self.assertEqual(len(session.calls), 1)

    def test_session_refresh_paths_match_the_har(self) -> None:
        calls: list[dict] = []

        class _Http:
            def get(self, url, headers=None, timeout=None):
                calls.append({"url": url, "headers": _lowered(headers or {})})
                return _FakeResponse({"accessToken": "a.b.c"})

        fp = {"device_id": "device-1", "session_seed": "seed-1"}
        self.assertEqual(
            blik_service._refresh_session_token(_Http(), fp, "oai-did=device-1", 30.0),
            "a.b.c",
        )
        self.assertTrue(
            calls[0]["url"].endswith("/api/auth/session?refresh=true&reason=token_expired&method=GET")
        )
        # The endpoint authenticates with cookies only - the HAR sends no bearer.
        self.assertNotIn("authorization", calls[0]["headers"])
        # The same-origin session refresh still presents the deployment identity
        # captured in the success HAR.
        self.assertEqual(calls[0]["headers"].get("oai-client-version"), _CHATGPT_CLIENT_VERSION)
        self.assertEqual(calls[0]["headers"].get("oai-client-build-number"), _CHATGPT_CLIENT_BUILD)

        self.assertEqual(
            blik_service._refresh_workspace_session(_Http(), fp, "oai-did=device-1", 30.0),
            "a.b.c",
        )
        self.assertTrue(calls[1]["url"].endswith(
            "/api/auth/session?workspace_update=true&reason=checkout_success"
            "&path=%2Fpayments%2Fsuccess"
        ))

    def test_stripe_session_id_resolution_matches_the_har(self) -> None:
        # HAR creation/read response: tag=custom_checkout_session with the
        # Stripe id in checkout_session_id.
        self.assertEqual(
            _stripe_session_id({"checkout_session_id": "cs_live_abc"}),
            "cs_live_abc",
        )
        # A ChatGPT-internal session can still expose the Stripe id through the
        # Checkout client secret, so the run is recoverable instead of fatal.
        self.assertEqual(
            _stripe_session_id({
                "checkout_session_id": "oaics_deadbeef",
                "client_secret": "cs_live_xyz_secret_123",
            }),
            "cs_live_xyz",
        )
        # With no Stripe evidence the original id is reported back unchanged.
        self.assertEqual(
            _stripe_session_id({"checkout_session_id": "oaics_deadbeef"}),
            "oaics_deadbeef",
        )

    def test_checkout_diagnostic_reports_promo_without_leaking_secrets(self) -> None:
        text = _checkout_diagnostic({
            "tag": "custom_checkout_session",
            "checkout_session_id": "oaics_deadbeef",
            "checkout_provider": "openai",
            "promo_campaign": {"promo_campaign_id": "plus-1-month-free"},
            "client_secret": "cs_live_secret_must_not_leak",
            "publishable_key": "pk_live_must_not_leak",
        })
        self.assertIn("oaics_deadbeef", text)
        self.assertIn("plus-1-month-free", text)
        self.assertNotIn("must_not_leak", text)

    def test_non_zero_due_reports_the_promo_echo(self) -> None:
        payload = _payload()
        payload["total_summary"]["due"] = 8049
        with self.assertRaises(BlikEligibilityError) as caught:
            _validate_checkout_eligibility(
                payload,
                promo_echo={"promo_campaign_id": "plus-1-month-free"},
            )
        self.assertEqual(caught.exception.reason_code, "blik_not_zero_due")
        self.assertIn("promo_campaign", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
