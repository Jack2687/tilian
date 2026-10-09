from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

import upi_cli
import upi_link_service as upi


HOSTED_URL = "https://payments.stripe.com/upi/instructions/fixture"


class UpiAmountContractTests(unittest.TestCase):
    def test_fractional_minor_units_never_become_zero(self):
        for amount in (0.5, -0.5, "0.5", "-0.5"):
            with self.subTest(amount=amount):
                self.assertIsNone(upi._amount_from_payload({"amount_due": amount}))

    def test_invalid_minor_units_do_not_crash_or_become_zero(self):
        for amount in (False, True, None, float("inf"), float("-inf"), float("nan")):
            with self.subTest(amount=amount):
                self.assertIsNone(upi._amount_from_payload({"amount_due": amount}))

    def test_verified_integer_zero_and_nonzero_amounts_are_preserved(self):
        for raw, expected in ((0, 0), (0.0, 0), ("0", 0), (100, 100), (100.0, 100), ("100", 100)):
            with self.subTest(raw=raw):
                self.assertEqual(upi._amount_from_payload({"amount_due": raw}), expected)

    def test_invalid_explicit_total_cannot_fall_back_to_a_zero_line_item(self):
        payload = {"amount_due": 0.5, "line_items": [{"amount": 0}]}
        self.assertIsNone(upi._amount_from_payload(payload))

    def test_invalid_nested_total_cannot_fall_back_to_discount_amount(self):
        for amount in (None, 0.5):
            with self.subTest(amount=amount):
                payload = {
                    "checkout_state": {
                        "total": {
                            "total": {"minorUnitsAmount": amount},
                            "discount": {"amount": 0},
                        },
                    },
                    "line_items": [{"amount": 0}],
                }
                self.assertIsNone(upi._amount_from_payload(payload))

    def test_valid_nested_totals_and_legacy_amount_shapes_are_preserved(self):
        payloads = (
            ({"checkout_state": {"total": {"total": {"minorUnitsAmount": 0}, "subtotal": {"amount": 100}}}}, 0),
            ({"checkout_state": {"total": {"minorUnitsAmount": 100}}}, 100),
            ({"total_summary": {"due": 0}, "line_items": [{"amount": 100}]}, 0),
            ({"invoice": {"amount_due": 100}}, 100),
            ({"checkout_session": {"amount_total": 0}}, 0),
            ({"payment_intent": {"amount": 100}}, 100),
            ({"line_items": [{"amount": 100}]}, 100),
        )
        for payload, expected in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(upi._amount_from_payload(payload), expected)

    def test_final_zero_gate_rejects_invalid_or_fractional_amount(self):
        for amount in (0.5, -0.5, False, True, None, float("inf"), float("nan")):
            with self.subTest(amount=amount):
                with self.assertRaisesRegex(RuntimeError, "upi promo (?:amount unavailable|not applied)"):
                    upi._require_zero_due(amount, "fixture")

    def test_force_mode_keeps_its_explicit_warning_behavior(self):
        for amount in (None, 100, 0.5):
            with self.subTest(amount=amount):
                warnings = []
                result = upi._require_zero_due(
                    amount, "fixture", force=True,
                    on_warn=lambda source, observed: warnings.append((source, observed)),
                )
                self.assertEqual(result, 0)
                self.assertEqual(warnings, [("fixture", amount)])


class UpiCliResultContractTests(unittest.TestCase):
    def run_cli(self, result):
        argv = ["upi_cli.py", "--at", "fixture-token", "--proxy", "http://proxy.invalid:8080"]
        with patch.object(sys, "argv", argv), patch.object(
            upi_cli, "extract_upi_link", return_value=result
        ), patch("builtins.print") as output:
            code = upi_cli.main()
        return code, json.loads(output.call_args_list[-1].args[0])

    def test_hosted_instructions_only_result_is_successful(self):
        for field in ("hosted_instructions_url", "hostedInstructionsUrl", "link"):
            with self.subTest(field=field):
                code, output = self.run_cli({"ok": True, field: HOSTED_URL})
                self.assertEqual(code, 0)
                self.assertTrue(output["ok"])
                self.assertEqual(output["link"], HOSTED_URL)
                self.assertEqual(output["reason_code"], "")

    def test_failure_preserves_engine_reason_and_hides_copyable_link(self):
        code, output = self.run_cli({
            "ok": False,
            "provider_redirect_url": HOSTED_URL,
            "error": "Checkout failed",
            "reason_code": "upi_promo_not_applied",
            "retryable": False,
        })
        self.assertEqual(code, 1)
        self.assertFalse(output["ok"])
        self.assertEqual(output["link"], "")
        self.assertEqual(output["reason_code"], "upi_promo_not_applied")
        self.assertFalse(output["retryable"])

    def test_failure_without_link_preserves_engine_reason(self):
        code, output = self.run_cli({
            "ok": False,
            "reason_code": "upi_checkout_expired",
            "retryable": False,
        })
        self.assertEqual(code, 1)
        self.assertEqual(output["link"], "")
        self.assertEqual(output["reason_code"], "upi_checkout_expired")

    def test_success_without_any_link_is_reported_as_missing(self):
        code, output = self.run_cli({"ok": True})
        self.assertEqual(code, 1)
        self.assertFalse(output["ok"])
        self.assertEqual(output["link"], "")
        self.assertEqual(output["reason_code"], "upi_link_missing")

    def test_truthy_string_is_not_a_success_flag(self):
        code, output = self.run_cli({"ok": "false", "provider_redirect_url": HOSTED_URL})
        self.assertEqual(code, 1)
        self.assertFalse(output["ok"])
        self.assertEqual(output["link"], "")
        self.assertEqual(output["reason_code"], "upi_extract_failed")

    def test_provider_decline_is_terminal_and_localization_is_stable(self):
        raw = (
            "UPI Stripe intent failed: status=requires_payment_method; "
            "code=payment_method_provider_decline; decline_code=processing_error"
        )
        argv = ["upi_cli.py", "--at", "fixture-token", "--proxy", "http://proxy.invalid:8080"]
        with patch.object(sys, "argv", argv), patch.object(
            upi_cli, "extract_upi_link", side_effect=RuntimeError(raw)
        ), patch("builtins.print") as output:
            code = upi_cli.main()
        payload = json.loads(output.call_args_list[-1].args[0])
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason_code"], "upi_payment_setup_declined")
        self.assertFalse(payload["retryable"])
        self.assertNotIn("blocked", payload["error"].lower())
        localized = upi.localize_upi_error(raw)
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in localized))
        self.assertNotIn("blocked", localized.lower())
        self.assertEqual(upi.localize_upi_error(localized), localized)


if __name__ == "__main__":
    unittest.main()
