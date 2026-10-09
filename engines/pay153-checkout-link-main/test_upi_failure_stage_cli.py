from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import upi_cli
import upi_link_service as upi


class UpiFailureStageCliTests(unittest.TestCase):
    def run_cli(self, steps, *, error=None, result=None):
        def extract(*args, **kwargs):
            observed = []
            for item in steps:
                observed.append(item)
                kwargs["on_step"](observed)
            if error is not None:
                raise RuntimeError(error)
            return result

        with patch.object(sys, "argv", ["upi_cli.py", "--at", "fixture-token"]), patch.object(
            upi_cli, "extract_upi_link", side_effect=extract
        ), patch("builtins.print") as output:
            code = upi_cli.main()
        lines = [call.args[0] for call in output.call_args_list]
        return code, lines[:-1], json.loads(lines[-1])

    def test_setup_decline_after_approval_closes_poll_and_preserves_approval(self):
        steps = [
            {"name": "ChatGPT订单风控审批", "status": "ok", "detail": "订单审批通过"},
            {"name": "查询UPI支付设置状态", "status": "run", "detail": "查询 Stripe 支付设置结果"},
        ]
        code, logs, result = self.run_cli(
            steps,
            error=("UPI Custom Checkout submission failed: setup_attempt_failed; "
                   "generic_decline; checkout_approval_payment_failure_with_payment_error"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(logs[0], "[approve] [ok] 订单审批通过")
        self.assertTrue(logs[1].startswith("[poll] [run] "))
        self.assertTrue(logs[-1].startswith("[poll] [fail] UPI Stripe 支付设置失败"))
        self.assertEqual(result["reason_code"], "upi_payment_setup_declined")
        self.assertIs(result["retryable"], False)
        self.assertIn("setup_attempt_failed", result["error_raw"])
        self.assertEqual(steps[0]["status"], "ok")
        self.assertEqual(steps[1]["status"], "run")

    def test_direct_confirm_decline_keeps_confirm_stage(self):
        _, logs, result = self.run_cli(
            [{"name": "确认UPI支付意图", "status": "run", "detail": "提交支付意图确认"}],
            error="UPI Stripe intent failed: status=requires_payment_method; code=generic_decline",
        )
        self.assertTrue(logs[-1].startswith("[confirm] [fail] "))
        self.assertEqual(result["reason_code"], "upi_payment_setup_declined")
        self.assertIs(result["retryable"], False)

    def test_actual_approval_error_remains_approval_failure(self):
        _, logs, result = self.run_cli(
            [{"name": "ChatGPT订单风控审批", "status": "run", "detail": "提交审批"}],
            error="UPI checkout approve failed",
        )
        self.assertTrue(logs[-1].startswith("[approve] [fail] "))
        self.assertEqual(result["reason_code"], "upi_approve_rejected")
        self.assertIs(result["retryable"], True)

    def test_existing_failure_stage_is_not_duplicated(self):
        _, logs, result = self.run_cli(
            [{"name": "查询UPI支付设置状态", "status": "fail", "detail": "上游已拒绝"}],
            error="UPI Custom Checkout submission failed: setup_attempt_failed",
        )
        self.assertEqual(logs, ["[poll] [fail] 上游已拒绝"])
        self.assertEqual(result["reason_code"], "upi_payment_setup_declined")

    def test_structured_failure_also_closes_current_stage(self):
        _, logs, result = self.run_cli(
            [{"name": "查询UPI支付意图状态", "status": "run", "detail": "查询支付意图"}],
            result={"ok": False, "error": "支付设置失败", "reason_code": "upi_payment_setup_declined", "retryable": False},
        )
        self.assertEqual(logs[-1], "[poll] [fail] 支付设置失败")
        self.assertEqual(result["reason_code"], "upi_payment_setup_declined")
        self.assertIs(result["retryable"], False)

    def test_success_does_not_emit_failure(self):
        code, logs, result = self.run_cli(
            [{"name": "生成支付链接和二维码", "status": "ok", "detail": "支付链接已生成"}],
            result={"ok": True, "link": "https://payments.stripe.com/upi/instructions/fixture"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(logs, ["[gateway] [ok] 支付链接已生成"])
        self.assertTrue(result["ok"])

    def test_failure_without_step_does_not_invent_stage(self):
        _, logs, result = self.run_cli([], error="network request failed")
        self.assertEqual(logs, [])
        self.assertEqual(result["reason_code"], "upi_proxy_network_error")

    def test_failure_log_stays_on_one_line(self):
        _, logs, _ = self.run_cli(
            [{"name": "查询UPI支付设置状态", "status": "run", "detail": "查询结果"}],
            result={"ok": False, "error": "查询失败\n第二行诊断", "reason_code": "fixture"},
        )
        self.assertEqual(logs[-1], "[poll] [fail] 查询失败 第二行诊断")

    def run_custom_checkout_cli(self, *, confirmation, payment):
        """Keep the checkout/approval/poll orchestration real; stub I/O only."""
        client = SimpleNamespace(
            client_observation="",
            device_id="fixture-device",
            request=Mock(return_value=(200, '{"result":"approved"}', {})),
        )

        def extract(*args, **kwargs):
            steps = []

            def step(name, status, detail):
                steps.append({"name": name, "status": status, "detail": detail})
                kwargs["on_step"](steps)

            return upi._run_upi_custom_checkout(
                client,
                token="fixture-token",
                step=step,
                steps=steps,
                cs_id="cs_fixture",
                processor_entity="openai_llc",
                stripe_pk="pk_fixture",
                checkout_client_secret="cs_fixture_secret",
                stripe_js_id="fixture-stripe-js",
                elements={},
                init_data={},
                checkout_config_id="fixture-config",
                amount=0,
                billing={},
                proxy="http://proxy.invalid:8080",
                referer="https://chatgpt.com/checkout/openai_llc/cs_fixture",
            )

        with patch.object(sys, "argv", ["upi_cli.py", "--at", "fixture-token"]), patch.object(
            upi_cli, "extract_upi_link", side_effect=extract
        ), patch.object(upi, "_upi_refresh_browser_risk"), patch.object(
            upi, "_locked_stripe_request_ids", return_value={}
        ), patch.object(upi, "_upi_sentinel_headers", return_value={}), patch.object(
            upi, "_chatgpt_headers", return_value={}
        ), patch.object(upi, "_stripe_custom_checkout_confirm", return_value=confirmation) as confirm, patch.object(
            upi, "_stripe_custom_checkout_fetch", return_value=payment
        ) as fetch, patch.object(upi.time, "sleep"), patch("builtins.print") as output:
            code = upi_cli.main()
        lines = [call.args[0] for call in output.call_args_list]
        return code, lines[:-1], json.loads(lines[-1]), client.request, confirm, fetch

    def test_real_custom_checkout_approval_then_poll_decline(self):
        code, logs, result, request, confirm, fetch = self.run_custom_checkout_cli(
            confirmation={"submission_attempt": {"state": "requires_approval"}},
            payment={
                "submission_attempt": {
                    "state": "failed",
                    "error": {"code": "checkout_approval_payment_failure_with_payment_error"},
                },
                "setup_intent": {
                    "status": "requires_payment_method",
                    "last_setup_error": {"code": "setup_attempt_failed", "decline_code": "generic_decline"},
                },
            },
        )
        self.assertEqual(code, 1)
        confirm.assert_called_once()
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], "https://chatgpt.com/backend-api/payments/checkout/approve")
        fetch.assert_called_once()
        approval = logs.index("[approve] [ok] 订单审批通过")
        self.assertTrue(logs[approval + 1].startswith("[poll] [run] "))
        self.assertTrue(logs[-1].startswith("[poll] [fail] UPI Stripe 支付设置失败"))
        self.assertFalse(any(line.startswith("[approve] [fail]") for line in logs))
        self.assertEqual(result["reason_code"], "upi_payment_setup_declined")
        self.assertIs(result["retryable"], False)
        for reason in ("setup_attempt_failed", "generic_decline", "checkout_approval_payment_failure_with_payment_error"):
            self.assertIn(reason, result["error"])

    def test_real_custom_checkout_confirm_decline_does_not_approve(self):
        code, logs, result, request, confirm, fetch = self.run_custom_checkout_cli(
            confirmation={"submission_attempt": {"state": "failed", "error": {"code": "generic_decline"}}},
            payment={},
        )
        self.assertEqual(code, 1)
        confirm.assert_called_once()
        request.assert_not_called()
        fetch.assert_not_called()
        self.assertTrue(logs[-1].startswith("[confirm] [fail] UPI Stripe 支付设置失败"))
        self.assertFalse(any(line.startswith("[approve]") or line.startswith("[poll]") for line in logs))
        self.assertEqual(result["reason_code"], "upi_payment_setup_declined")
        self.assertIs(result["retryable"], False)


if __name__ == "__main__":
    unittest.main()
