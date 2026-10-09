from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

import upi_cli


class UpiBillingCliTests(unittest.TestCase):
    def run_cli(self, *, billing_json=None, error=None):
        argv = ["upi_cli.py", "--at", "fixture-token", "--email", "buyer@example.com"]
        if billing_json is not None:
            argv += ["--billing-json", billing_json]
        with patch.object(sys, "argv", argv), patch.object(
            upi_cli, "extract_upi_link", side_effect=error,
            return_value={"ok": True, "link": "https://payments.stripe.com/upi/instructions/fixture"},
        ) as extract, patch("builtins.print") as output:
            code = upi_cli.main()
        return code, json.loads(output.call_args_list[-1].args[0]), extract

    def test_invalid_json_or_non_object_is_terminal_before_extraction(self):
        for value in ('{"address":', '[]', '[{"country":"IN"}]', 'null', 'false', '42', '"IN"'):
            with self.subTest(value=value):
                code, output, extract = self.run_cli(billing_json=value)
                self.assertEqual(code, 1)
                self.assertFalse(output["ok"])
                self.assertEqual(output["link"], "")
                self.assertEqual(output["reason_code"], "upi_billing_invalid")
                self.assertIs(output["retryable"], False)
                extract.assert_not_called()

    def test_missing_or_identity_only_billing_preserves_default_contract(self):
        for value, expected in ((None, {}), ('{}', {}), ('{"name":"Buyer"}', {"name": "Buyer"})):
            with self.subTest(value=value):
                code, output, extract = self.run_cli(billing_json=value)
                self.assertEqual(code, 0)
                self.assertTrue(output["ok"])
                self.assertEqual(extract.call_args.kwargs["billing"], {"email": "buyer@example.com", **expected})

    def test_explicit_billing_validation_error_is_terminal(self):
        message = "UPI 账单地址无效：Mumbai 的邦和邮编不匹配"
        code, output, extract = self.run_cli(error=ValueError(message))
        self.assertEqual(code, 1)
        self.assertEqual(output["reason_code"], "upi_billing_invalid")
        self.assertIs(output["retryable"], False)
        self.assertEqual(output["error"], message)
        extract.assert_called_once()

    def test_english_billing_validation_error_is_terminal(self):
        code, output, _ = self.run_cli(error=ValueError("upi billing invalid: country must be IN"))
        self.assertEqual(code, 1)
        self.assertEqual(output["reason_code"], "upi_billing_invalid")
        self.assertIs(output["retryable"], False)
        self.assertIn("账单地址", output["error"])

    def test_unrelated_failure_keeps_existing_retry_contract(self):
        code, output, _ = self.run_cli(error=RuntimeError("network request failed"))
        self.assertEqual(code, 1)
        self.assertEqual(output["reason_code"], "upi_proxy_network_error")
        self.assertIs(output["retryable"], True)


if __name__ == "__main__":
    unittest.main()
