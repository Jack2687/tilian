from __future__ import annotations

import json
import sys
import unittest
from unittest.mock import patch

import upi_cli


class UpiTlsErrorCliTests(unittest.TestCase):
    def run_failure(self, message, localized=None):
        argv = ["upi_cli.py", "--at", "fixture-token"]
        with patch.object(sys, "argv", argv), patch.object(
            upi_cli, "extract_upi_link", side_effect=RuntimeError(message),
        ), patch.object(
            upi_cli, "localize_upi_error", return_value=localized or message,
        ), patch("builtins.print") as output:
            code = upi_cli.main()
        return code, json.loads(output.call_args_list[-1].args[0])

    def test_raw_ca_loading_errors_are_terminal_before_network_classification(self):
        messages = (
            "Failed to perform, curl: (77). request through proxy failed",
            "curl (77): local certificate loading failed",
            "error adding trust anchors from file: fixture/cacert.pem; proxy configured",
            "error setting certificate verify locations: CAfile: fixture/cacert.pem",
        )
        for message in messages:
            with self.subTest(message=message):
                code, output = self.run_failure(message, localized="UPI 网络请求失败")
                self.assertEqual(code, 1)
                self.assertIs(output["ok"], False)
                self.assertEqual(output["link"], "")
                self.assertEqual(output["reason_code"], "upi_tls_configuration_error")
                self.assertIs(output["retryable"], False)
                self.assertIn("本地证书配置错误", output["error"])
                self.assertNotIn("代理", output["error"])
                self.assertIn(message, output["error_raw"])

    def test_localized_configuration_error_is_terminal(self):
        message = "UPI 本地证书配置错误：请求库无法加载 CA 证书，请检查运行环境（curl 77）"
        code, output = self.run_failure(message)
        self.assertEqual(code, 1)
        self.assertEqual(output["reason_code"], "upi_tls_configuration_error")
        self.assertIs(output["retryable"], False)
        self.assertEqual(output["error"], message)

    def test_unrelated_network_errors_keep_existing_classification(self):
        for message in (
            "curl: (28) Operation timed out",
            "curl: (60) SSL certificate problem: certificate has expired",
            "proxy connection failed",
        ):
            with self.subTest(message=message):
                code, output = self.run_failure(message)
                self.assertEqual(code, 1)
                self.assertEqual(output["reason_code"], "upi_proxy_network_error")
                self.assertIs(output["retryable"], True)


if __name__ == "__main__":
    unittest.main()
