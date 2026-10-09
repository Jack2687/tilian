from __future__ import annotations

import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import urlparse

import upi_link_service as upi
from test_upi_link_service import (
    RecordingClient,
    fixture_billing,
    fixture_stripe_request_ids,
    install_live_browser_risk,
)


SETUP_ID = "seti_fixture"
SETUP_SECRET = "seti_fixture_secret_fixture"
PAYMENT_ID = "pi_fixture"
PAYMENT_SECRET = "pi_fixture_secret_fixture"
RETURN_URL = "https://chatgpt.com/checkout/verify?checkout_session_id=oaics_fixture&source=fixture"
HOSTED_URL = "https://payments.stripe.com/upi/instructions/fixture"
QR_URL = "https://qr.stripe.com/fixture.svg"


def fixture_requires_action(intent_id: str) -> dict:
    return {
        "id": intent_id,
        "status": "requires_action",
        "next_action": {
            "type": "upi_handle_redirect_or_display_qr_code",
            "upi_handle_redirect_or_display_qr_code": {
                "hosted_instructions_url": HOSTED_URL,
                "qr_code": {
                    "image_url_svg": QR_URL,
                    "image_url_png": "https://qr.stripe.com/fixture.png",
                    "expires_at": 2000000000,
                },
            },
        },
    }


class CheckoutIntentContractTests(unittest.TestCase):
    def test_top_level_secret_identifies_each_supported_intent(self):
        for kind, intent_id, secret in (
            ("setup_intent", SETUP_ID, SETUP_SECRET),
            ("payment_intent", PAYMENT_ID, PAYMENT_SECRET),
        ):
            for explicit_type in (False, True):
                with self.subTest(kind=kind, explicit_type=explicit_type):
                    payload = {"status": "success", "client_secret": secret}
                    if explicit_type:
                        payload["type"] = kind
                    self.assertEqual(upi._checkout_intent(payload), (kind, intent_id, secret))

    def test_nested_intent_and_id_shapes_preserve_identity(self):
        for kind, intent_id, secret in (
            ("setup_intent", SETUP_ID, SETUP_SECRET),
            ("payment_intent", PAYMENT_ID, PAYMENT_SECRET),
        ):
            payloads = (
                {kind: {"id": intent_id, "client_secret": secret}},
                {kind: {"id": intent_id}, "client_secret": secret},
                {kind: intent_id, "client_secret": secret},
                {"id": intent_id, "client_secret": secret},
            )
            for payload in payloads:
                with self.subTest(kind=kind, keys=list(payload)):
                    self.assertEqual(upi._checkout_intent(payload), (kind, intent_id, secret))

    def test_conflicting_type_or_id_is_rejected_without_exposing_secret(self):
        payloads = (
            {"type": "payment_intent", "client_secret": SETUP_SECRET},
            {"type": "setup_intent", "client_secret": PAYMENT_SECRET},
            {"setup_intent": "seti_other", "client_secret": SETUP_SECRET},
            {"payment_intent": {"id": "pi_other", "client_secret": PAYMENT_SECRET}},
            {"id": "seti_other", "client_secret": SETUP_SECRET},
            {"setup_intent": {"id": PAYMENT_ID, "client_secret": PAYMENT_SECRET}},
        )
        for payload in payloads:
            with self.subTest(keys=list(payload)):
                with self.assertRaises(RuntimeError) as raised:
                    upi._checkout_intent(payload)
                self.assertNotIn(SETUP_SECRET, str(raised.exception))
                self.assertNotIn(PAYMENT_SECRET, str(raised.exception))

    def test_missing_secret_or_unsupported_secret_fails_closed(self):
        for payload in ({}, {"type": "setup_intent", "id": SETUP_ID}, {"client_secret": "cuss_fixture_secret_fixture"}):
            with self.subTest(keys=list(payload)):
                with self.assertRaises(RuntimeError) as raised:
                    upi._checkout_intent(payload)
                self.assertNotIn("cuss_fixture_secret_fixture", str(raised.exception))


class StripeSetupConfirmContractTests(unittest.TestCase):
    def test_setup_confirm_sends_token_and_server_return_url_to_setup_endpoint(self):
        client = RecordingClient()
        response = fixture_requires_action(SETUP_ID)

        def request(method, url, **kwargs):
            client.calls.append({"method": method, "url": url, **kwargs})
            return 200, json.dumps(response), {}

        client.request = request
        result = upi._stripe_setup_intent_confirm(
            client,
            setup_intent_id=SETUP_ID,
            client_secret=SETUP_SECRET,
            confirmation_token="ctoken_fixture",
            stripe_pk="pk_test_fixture",
            stripe_js_id=fixture_stripe_request_ids()["stripe_js_id"],
            return_url=RETURN_URL,
            proxy=client.base_proxy,
        )

        self.assertEqual(result, response)
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], f"https://api.stripe.com/v1/setup_intents/{SETUP_ID}/confirm")
        self.assertEqual(call["proxy"], client.base_proxy)
        self.assertEqual(call["headers"]["content-type"], "application/x-www-form-urlencoded")
        for field, expected in {
            "client_secret": SETUP_SECRET,
            "confirmation_token": "ctoken_fixture",
            "key": "pk_test_fixture",
            "return_url": RETURN_URL,
            "client_attribution_metadata[client_session_id]": "stripe-js-fixture",
        }.items():
            self.assertEqual(call["form"][field], expected)


class StripeIntentPollingContractTests(unittest.TestCase):
    def poll(self, kind: str, responses: list[dict]):
        client = RecordingClient()
        intent_id, secret = (
            (SETUP_ID, SETUP_SECRET) if kind == "setup" else (PAYMENT_ID, PAYMENT_SECRET)
        )
        pending = iter(responses)

        def request(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(urlparse(url).path, f"/v1/{kind}_intents/{intent_id}")
            client.calls.append({"method": method, "url": url, **kwargs})
            return 200, json.dumps(next(pending)), {}

        client.request = request
        self.poll_client = client
        kwargs = {
            f"{kind}_intent_id": intent_id,
            "client_secret": secret,
            "stripe_pk": "pk_test_fixture",
            "proxy": client.base_proxy,
            "max_attempts": len(responses),
        }
        if kind == "payment":
            kwargs["steps"] = []
        with patch.object(upi, "_emit_upi_qr") as emit_qr, patch.object(
            upi, "_emit_upi_paid"
        ) as emit_paid, patch.object(upi.time, "sleep") as sleep, patch(
            "socket.create_connection", side_effect=AssertionError("Network disabled")
        ):
            self.emit_qr = emit_qr
            self.emit_paid = emit_paid
            self.poll_sleep = sleep
            helper = getattr(upi, f"_poll_stripe_{kind}_intent")
            return helper(client, **kwargs)

    def test_qr_image_alone_does_not_end_polling_before_a_copyable_link(self):
        for kind, intent_id in (("setup", SETUP_ID), ("payment", PAYMENT_ID)):
            with self.subTest(kind=kind):
                first = fixture_requires_action(intent_id)
                del first["next_action"]["upi_handle_redirect_or_display_qr_code"]["hosted_instructions_url"]
                second = fixture_requires_action(intent_id)
                result = self.poll(kind, [first, second])
                self.assertEqual(result, second)
                self.assertEqual(len(self.poll_client.calls), 2)
                self.assertEqual(self.emit_qr.call_count, 2)
                self.emit_paid.assert_not_called()
                self.poll_sleep.assert_called_once()

    def test_terminal_failure_does_not_emit_a_stale_qr(self):
        for kind, intent_id in (("setup", SETUP_ID), ("payment", PAYMENT_ID)):
            with self.subTest(kind=kind):
                rejected = fixture_requires_action(intent_id)
                rejected.update({
                    "status": "requires_payment_method",
                    "last_setup_error": {"code": "setup_attempt_failed", "decline_code": "generic_decline"},
                })
                with self.assertRaises(RuntimeError):
                    self.poll(kind, [rejected])
                self.assertEqual(len(self.poll_client.calls), 1)
                self.emit_qr.assert_not_called()
                self.emit_paid.assert_not_called()
                self.poll_sleep.assert_not_called()

    def test_succeeded_intent_emits_paid_event(self):
        for kind, intent_id in (("setup", SETUP_ID), ("payment", PAYMENT_ID)):
            with self.subTest(kind=kind):
                succeeded = {"id": intent_id, "status": "succeeded"}
                self.assertEqual(self.poll(kind, [succeeded]), succeeded)
                self.assertEqual(len(self.poll_client.calls), 1)
                self.emit_paid.assert_called_once_with(None, "succeeded")
                self.poll_sleep.assert_not_called()


class OaicsIntentRoutingTests(unittest.TestCase):
    """All HTTP responses are local fixtures, including the real confirm helpers."""

    def run_phase(
        self, checkout_response, stripe_response, *, poll_response=None,
        tax_response=None, state_methods=None, elements_methods=None,
    ):
        self.client = RecordingClient()
        self.stripe_requests = []
        self.steps = []
        state = {
            "payment_method_types": ["card", "upi"] if state_methods is None else state_methods,
            "amount": 0,
        }
        elements = {"session_id": "elements_fixture", **state}
        if elements_methods is not None:
            elements["payment_method_types"] = elements_methods

        def request(method, url, **kwargs):
            call = {"method": method, "url": url, **kwargs}
            self.client.calls.append(call)
            path = urlparse(url).path
            if path == "/backend-api/sentinel/ping":
                return 200, "{}", {}
            if url.startswith("https://api.stripe.com/v1/") and path.endswith("/confirm"):
                self.stripe_requests.append(call)
                return 200, json.dumps(stripe_response), {}
            raise AssertionError(f"Unexpected HTTP call in offline test: {method} {url}")

        self.client.request = request
        replacements = {
            "_capture_upi_browser_risk": {"side_effect": install_live_browser_risk},
            "_upi_refresh_browser_risk": {"return_value": {}},
            "_warmup_india_client": {"return_value": None},
            "_human_pause": {"return_value": None},
            "_sentinel_ping": {"return_value": None},
            "_fetch_upi_oaics_state": {"return_value": state},
            "_elements_session": {"return_value": elements},
            "_submit_upi_taxes": {"return_value": {"amount": 0} if tax_response is None else tax_response},
            "_submit_upi_billing_snapshot": {"return_value": (204, "")},
            "_confirmation_token": {"return_value": {"id": "ctoken_fixture"}},
            "_checkout_confirm_with_token": {"return_value": checkout_response},
            "_emit_upi_qr": {"return_value": None},
            "_poll_stripe_setup_intent": (
                {"return_value": poll_response}
                if poll_response is not None
                else {"side_effect": AssertionError("SetupIntent must not be polled")}
            ),
            "_poll_stripe_payment_intent": {"side_effect": AssertionError("PaymentIntent must not be polled")},
        }
        with ExitStack() as stack:
            self.mocks = {
                name: stack.enter_context(patch.object(upi, name, **options))
                for name, options in replacements.items()
            }
            stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network disabled")))
            return upi._run_upi_india_phase(
                self.client,
                token="fixture-access-token",
                step=lambda name, status, detail: self.steps.append((name, status, detail)),
                steps=[],
                cs_id="oaics_fixture",
                processor_entity="openai_llc",
                stripe_pk="pk_test_fixture",
                customer_session_secret="cuss_fixture",
                checkout_ui_mode="deferred",
                referer="https://chatgpt.com/checkout/openai_llc/oaics_fixture",
                india_base=self.client.base_proxy,
                billing=fixture_billing(),
                promo_payload=state,
                use_promo=True,
            )

    def test_invalid_final_tax_amount_cannot_reuse_the_initial_zero_amount(self):
        with self.assertRaisesRegex(RuntimeError, "upi promo amount unavailable"):
            self.run_phase(
                {"status": "success", "type": "setup_intent", "client_secret": SETUP_SECRET},
                fixture_requires_action(SETUP_ID),
                tax_response={"checkout_session": {"amount_due": 0.5}},
            )
        self.mocks["_confirmation_token"].assert_not_called()
        self.mocks["_checkout_confirm_with_token"].assert_not_called()
        self.assertEqual(self.stripe_requests, [])

    def test_confirmation_token_preserves_the_complete_checkout_method_list(self):
        methods = ["link", "card", "upi"]
        result = self.run_phase(
            {"status": "success", "type": "setup_intent", "client_secret": SETUP_SECRET},
            fixture_requires_action(SETUP_ID),
            state_methods=methods,
            elements_methods=["upi", "card"],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.mocks["_elements_session"].call_args.kwargs["payment_method_types"], methods,
        )
        self.assertEqual(
            self.mocks["_confirmation_token"].call_args.kwargs["payment_method_types"], methods,
        )

    def test_har_setup_intent_requires_action_returns_link_without_claiming_paid(self):
        result = self.run_phase(
            {"status": "success", "type": "setup_intent", "client_secret": SETUP_SECRET, "confirm_return_url": RETURN_URL},
            fixture_requires_action(SETUP_ID),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["setup_intent_id"], SETUP_ID)
        self.assertEqual(result["setup_intent_status"], "requires_action")
        self.assertFalse(result.get("payment_intent_id"))
        self.assertFalse(result.get("payment_intent_status"))
        self.assertEqual(result["long_url"], HOSTED_URL)
        self.assertEqual(result["hosted_instructions_url"], HOSTED_URL)
        self.assertEqual(result["qr_url"], QR_URL)
        self.assertIs(result["paid"], False)
        self.assertEqual(len(self.stripe_requests), 1)
        self.assertEqual(urlparse(self.stripe_requests[0]["url"]).path, f"/v1/setup_intents/{SETUP_ID}/confirm")
        self.assertEqual(self.stripe_requests[0]["form"]["return_url"], RETURN_URL)
        self.mocks["_poll_stripe_payment_intent"].assert_not_called()
        self.mocks["_poll_stripe_setup_intent"].assert_not_called()

    def test_setup_intent_without_link_polls_the_setup_endpoint(self):
        result = self.run_phase(
            {"type": "setup_intent", "client_secret": SETUP_SECRET},
            {"id": SETUP_ID, "status": "requires_action"},
            poll_response=fixture_requires_action(SETUP_ID),
        )
        self.assertEqual(result["long_url"], HOSTED_URL)
        self.assertIs(result["paid"], False)
        self.mocks["_poll_stripe_setup_intent"].assert_called_once()
        kwargs = self.mocks["_poll_stripe_setup_intent"].call_args.kwargs
        self.assertEqual(kwargs["setup_intent_id"], SETUP_ID)
        self.assertEqual(kwargs["client_secret"], SETUP_SECRET)
        self.mocks["_poll_stripe_payment_intent"].assert_not_called()

    def test_setup_failure_stops_after_the_first_confirm_without_polling(self):
        with self.assertRaises(RuntimeError):
            self.run_phase(
                {"status": "success", "type": "setup_intent", "client_secret": SETUP_SECRET},
                {
                    "id": SETUP_ID,
                    "status": "requires_payment_method",
                    "last_setup_error": {"code": "setup_attempt_failed", "decline_code": "generic_decline"},
                },
            )
        self.assertEqual(len(self.stripe_requests), 1)
        self.mocks["_poll_stripe_setup_intent"].assert_not_called()
        self.mocks["_poll_stripe_payment_intent"].assert_not_called()

    def test_payment_intent_route_and_result_remain_supported(self):
        result = self.run_phase(
            {"status": "success", "payment_intent": {"id": PAYMENT_ID, "client_secret": PAYMENT_SECRET}, "confirm_return_url": RETURN_URL},
            fixture_requires_action(PAYMENT_ID),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["payment_intent_id"], PAYMENT_ID)
        self.assertEqual(result["payment_intent_status"], "requires_action")
        self.assertFalse(result.get("setup_intent_id"))
        self.assertFalse(result.get("setup_intent_status"))
        self.assertEqual(result["long_url"], HOSTED_URL)
        self.assertIs(result["paid"], False)
        self.assertEqual(len(self.stripe_requests), 1)
        self.assertEqual(urlparse(self.stripe_requests[0]["url"]).path, f"/v1/payment_intents/{PAYMENT_ID}/confirm")
        self.assertEqual(self.stripe_requests[0]["form"]["return_url"], RETURN_URL)
        self.mocks["_poll_stripe_setup_intent"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
