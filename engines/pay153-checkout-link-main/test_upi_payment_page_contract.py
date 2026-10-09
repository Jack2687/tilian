from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import upi_link_service as upi
from test_upi_link_service import RecordingClient


HOSTED_URL = "https://payments.stripe.com/upi/instructions/payment-page-fixture"
QR_URL = "https://qr.stripe.com/payment-page-fixture.svg"
DEEP_LINK = "upi://pay?pa=fixture%40example&pn=Fixture&am=0&cu=INR"


def payment_page(*, hosted: bool = True, deep_link: bool = False) -> dict:
    action = {
        "qr_code": {
            "image_url_svg": QR_URL,
            "expires_at": 2000000000,
        },
    }
    if hosted:
        action["hosted_instructions_url"] = HOSTED_URL
    if deep_link:
        action["upi_deep_link"] = DEEP_LINK
    return {
        "status": "open",
        "payment_status": "unpaid",
        "submission_attempt": {"state": "processing"},
        "setup_intent": {
            "id": "seti_payment_page_fixture",
            "status": "requires_action",
        },
        "next_action": {
            "type": "upi_handle_redirect_or_display_qr_code",
            "upi_handle_redirect_or_display_qr_code": action,
        },
    }


class PaymentPagePollingContractTests(unittest.TestCase):
    def poll(self, responses: list[dict]) -> dict:
        self.events = []
        self.stages = []
        self.timeline = []
        client = SimpleNamespace()
        pending = iter(responses)

        def fetch_response(*args, **kwargs):
            self.timeline.append(("fetch",))
            return next(pending)

        def record_stage(*args):
            self.stages.append(args)
            self.timeline.append(("stage", *args))

        with patch.object(
            upi, "_stripe_custom_checkout_fetch", side_effect=fetch_response
        ) as fetch, patch.object(upi.time, "sleep") as sleep, patch.object(
            upi.UpiHttpClient,
            "request",
            side_effect=AssertionError("Live HTTP is disabled in this test"),
        ), patch("socket.create_connection", side_effect=AssertionError("Network disabled")):
            self.fetch = fetch
            self.sleep = sleep
            return upi._stripe_custom_checkout_poll(
                client,
                cs_id="cs_test_payment_page_fixture",
                stripe_pk="pk_test_fixture",
                stripe_js_id="js_fixture",
                elements={},
                proxy="http://proxy.invalid:8080",
                max_attempts=len(responses),
                on_qr_update=self.events.append,
                step=record_stage,
            )

    def assert_no_payment_material_emitted(self) -> None:
        self.assertFalse(any(event.get("event") == "qr_refresh" for event in self.events))
        self.assertFalse(any(event.get("paid") is True for event in self.events))

    def test_failed_submission_rejects_residual_hosted_link_and_qr(self):
        payload = payment_page()
        payload["submission_attempt"] = {
            "state": "failed",
            "error": {
                "code": "checkout_approval_payment_failure_with_payment_error",
                "payment_error": {
                    "code": "setup_attempt_failed",
                    "decline_code": "generic_decline",
                },
            },
        }
        with self.assertRaisesRegex(RuntimeError, "submission failed") as raised:
            self.poll([payload])
        for code in (
            "checkout_approval_payment_failure_with_payment_error",
            "setup_attempt_failed",
            "generic_decline",
        ):
            self.assertIn(code, str(raised.exception))
        self.assertEqual(self.fetch.call_count, 1)
        self.assert_no_payment_material_emitted()

    def test_failed_submission_rejects_residual_deep_link_and_qr(self):
        payload = payment_page(hosted=False, deep_link=True)
        payload["submission_attempt"] = {"state": "failed"}
        with self.assertRaisesRegex(RuntimeError, "submission failed"):
            self.poll([payload])
        self.assert_no_payment_material_emitted()

    def test_poll_starts_its_stage_before_fetching_a_failed_submission(self):
        payload = payment_page()
        payload["submission_attempt"] = {"state": "failed"}
        with self.assertRaisesRegex(RuntimeError, "submission failed"):
            self.poll([payload])
        self.assertEqual(self.timeline[0][:3], ("stage", "查询UPI支付设置状态", "run"))
        self.assertEqual(self.timeline[1], ("fetch",))

    def test_nested_canceled_intent_rejects_residual_payment_material(self):
        for holder in ("setup_intent", "payment_intent"):
            with self.subTest(holder=holder):
                payload = payment_page()
                payload.pop("setup_intent")
                payload[holder] = {"status": "canceled"}
                with self.assertRaisesRegex(RuntimeError, "status=canceled"):
                    self.poll([payload])
                self.assertEqual(self.fetch.call_count, 1)
                self.assert_no_payment_material_emitted()

    def test_nested_requires_payment_method_with_error_rejects_residual_material(self):
        for holder, error_field in (
            ("setup_intent", "last_setup_error"),
            ("payment_intent", "last_payment_error"),
        ):
            with self.subTest(holder=holder):
                payload = payment_page()
                payload.pop("setup_intent")
                payload[holder] = {
                    "status": "requires_payment_method",
                    error_field: {
                        "code": "payment_method_provider_decline",
                        "decline_code": "processing_error",
                    },
                }
                with self.assertRaisesRegex(RuntimeError, "requires_payment_method") as raised:
                    self.poll([payload])
                self.assertIn("payment_method_provider_decline", str(raised.exception))
                self.assertIn("processing_error", str(raised.exception))
                self.assert_no_payment_material_emitted()

    def test_qr_image_only_continues_until_hosted_instructions_are_ready(self):
        first, second = payment_page(hosted=False), payment_page()
        result = self.poll([first, second])
        self.assertEqual(result, second)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(upi._upi_copyable_link(result), HOSTED_URL)
        self.assertGreaterEqual(self.sleep.call_count, 1)
        self.assertFalse(any(event.get("paid") is True for event in self.events))

    def test_initial_requires_payment_method_without_error_keeps_polling(self):
        for holder in ("setup_intent", "payment_intent"):
            with self.subTest(holder=holder):
                first = {
                    "status": "open",
                    "submission_attempt": {"state": "processing"},
                    holder: {"status": "requires_payment_method"},
                }
                second = payment_page()
                self.assertEqual(self.poll([first, second]), second)
                self.assertEqual(self.fetch.call_count, 2)

    def test_qr_image_only_continues_until_upi_deep_link_is_ready(self):
        first = payment_page(hosted=False)
        second = payment_page(hosted=False, deep_link=True)
        result = self.poll([first, second])
        self.assertEqual(result, second)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(upi._upi_copyable_link(result), DEEP_LINK)
        self.assertFalse(any(event.get("paid") is True for event in self.events))

    def test_requires_action_with_hosted_link_returns_and_emits_qr(self):
        payload = payment_page()
        self.assertEqual(self.poll([payload]), payload)
        self.assertEqual(self.fetch.call_count, 1)
        qr_events = [event for event in self.events if event.get("event") == "qr_refresh"]
        self.assertTrue(qr_events)
        self.assertEqual(qr_events[-1]["hosted_instructions_url"], HOSTED_URL)
        self.assertEqual(qr_events[-1]["qr_url"], QR_URL)
        self.assertFalse(any(event.get("paid") is True for event in self.events))

    def test_unrelated_nested_url_does_not_end_payment_page_poll(self):
        first = {
            "status": "open",
            "submission_attempt": {"state": "processing"},
            "customer_details": {"url": "https://example.invalid/fixture"},
        }
        second = payment_page()
        self.assertEqual(self.poll([first, second]), second)
        self.assertEqual(self.fetch.call_count, 2)


class PaymentPageDiagnosticContractTests(unittest.TestCase):
    def test_page_status_does_not_hide_individual_intent_statuses(self):
        summary = upi._payment_page_poll_summary({
            "status": "open",
            "payment_status": "unpaid",
            "setup_intent": {"status": "requires_payment_method"},
            "payment_intent": {"status": "processing"},
        })
        self.assertIn("setup_status=requires_payment_method", summary)
        self.assertIn("payment_status=processing", summary)

    def test_payment_method_type_uses_only_expanded_upstream_objects(self):
        payloads = (
            {"setup_intent": {"payment_method": {"type": "upi"}}},
            {"payment_intent": {"payment_method": {"type": "card"}}},
            {"setup_intent": {"last_setup_error": {"payment_method": {"type": "upi"}}}},
            {"payment_intent": {"last_payment_error": {"payment_method": {"type": "card"}}}},
            {"submission_attempt": {"error": {"payment_error": {"payment_method": {"type": "upi"}}}}},
        )
        for payload, expected in zip(payloads, ("upi", "card", "upi", "card", "upi")):
            with self.subTest(payload=payload):
                self.assertIn("payment_method_type=" + expected, upi._payment_page_poll_summary(payload))

    def test_unexpanded_or_missing_payment_method_does_not_guess_upi(self):
        for payload in ({}, {"setup_intent": {"payment_method": "pm_fixture"}}):
            with self.subTest(payload=payload):
                summary = upi._payment_page_poll_summary(payload)
                self.assertIn("payment_method_type=-", summary)
                self.assertNotIn("pm_fixture", summary)

    def test_diagnostic_fields_reject_unsafe_or_oversized_values(self):
        for invalid in ("private value=fixture", "x" * 101, {"secret": "hidden-fixture"}, ["secret"]):
            with self.subTest(invalid=invalid):
                summary = upi._payment_page_poll_summary({
                    "setup_intent": {
                        "status": invalid,
                        "client_secret": "seti_fixture_secret_NEVER_LOG",
                        "payment_method": {"type": invalid, "billing_details": {"email": "private@example.invalid"}},
                    },
                    "payment_intent": {"status": invalid},
                })
                self.assertIn("setup_status=-", summary)
                self.assertIn("payment_status=-", summary)
                self.assertIn("payment_method_type=-", summary)
                for secret in ("private value", "x" * 101, "hidden-fixture", "NEVER_LOG", "private@example.invalid"):
                    self.assertNotIn(secret, summary)


class PaymentPageHttpDiagnosticTests(unittest.TestCase):
    def fetch(self, status: int, payload: dict, response_headers: dict) -> dict:
        client = RecordingClient()
        self.stages = []
        with patch.object(
            client, "request", return_value=(status, json.dumps(payload), response_headers)
        ), patch("socket.create_connection", side_effect=AssertionError("Network disabled")):
            return upi._stripe_custom_checkout_fetch(
                client,
                cs_id="cs_test_payment_page_fixture",
                stripe_pk="pk_test_fixture",
                stripe_js_id="js_fixture",
                elements={},
                proxy=client.base_proxy,
                step=lambda *args: self.stages.append(args),
            )

    def test_fetch_logs_request_id_and_status_without_private_response_fields(self):
        payload = payment_page()
        payload["setup_intent"].update({
            "client_secret": "seti_fixture_secret_PRIVATE_FIXTURE",
            "payment_method": {"type": "upi", "billing_details": {"email": "private@example.invalid"}},
        })
        payload["customer_details"] = {"address": "PRIVATE_ADDRESS"}
        headers = {
            "Request-Id": "req_fixture",
            "Set-Cookie": "PRIVATE_COOKIE",
            "Authorization": "Bearer PRIVATE_AUTHORIZATION",
        }
        self.assertEqual(self.fetch(200, payload, headers), payload)
        diagnostic = "\n".join(stage[2] for stage in self.stages)
        for expected in ("HTTP=200", "request_id=req_fixture", "setup_status=requires_action", "payment_method_type=upi"):
            self.assertIn(expected, diagnostic)
        for private in (
            "PRIVATE_FIXTURE", "private@example.invalid", "PRIVATE_ADDRESS",
            "PRIVATE_COOKIE", "PRIVATE_AUTHORIZATION", HOSTED_URL, QR_URL,
        ):
            self.assertNotIn(private, diagnostic)

    def test_invalid_request_id_is_not_copied_to_diagnostics(self):
        for request_id in ("req_fixture\nCookie=PRIVATE_COOKIE", "r" * 101):
            with self.subTest(request_id=request_id):
                self.fetch(200, {}, {"Request-Id": request_id})
                diagnostic = "\n".join(stage[2] for stage in self.stages)
                self.assertIn("request_id=-", diagnostic)
                self.assertNotIn(request_id, diagnostic)

    def test_failed_http_logs_status_and_request_id_without_response_message(self):
        payload = {"error": {"code": "api_error", "message": "PRIVATE_RESPONSE_MESSAGE"}}
        with self.assertRaises(RuntimeError):
            self.fetch(502, payload, {"X-Request-Id": "req_failure", "Set-Cookie": "PRIVATE_COOKIE"})
        self.assertEqual(self.stages[-1][2], "HTTP=502; request_id=req_failure")


if __name__ == "__main__":
    unittest.main()
