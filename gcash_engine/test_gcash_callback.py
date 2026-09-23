import base64
import json
import sys
import threading
import types
import unittest
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

# 本组测试只覆盖回调/订阅逻辑，不加载传输层的第三方 HTTP 依赖。
paypal_proxy = types.ModuleType("sms_tool.paypal_proxy")
paypal_proxy.normalize_proxy_url = lambda value: str(value or "")
sys.modules["sms_tool.paypal_proxy"] = paypal_proxy

import gcash_custom_flow as flow


def _b64url(value):
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response(200, {"status": "processing"})

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return _Response(200, {
            "subscriptions": [{
                "plan_type": "plus",
                "seats_entitled": 1,
                "is_delinquent": False,
            }]
        })


class GCashCallbackTests(unittest.TestCase):
    def test_unusual_activity_detection_is_specific_and_multilingual(self):
        self.assertTrue(flow._is_unusual_activity("Your network or account triggered unusual activity"))
        self.assertTrue(flow._is_unusual_activity("当前账号触发了异常活动"))
        self.assertFalse(flow._is_unusual_activity("upstream connection aborted"))

    def test_chrome_fallback_keeps_identity_but_aligns_tls_and_headers(self):
        fallback = flow._chrome_fallback_fingerprint({
            "device_id": "stable-device",
            "oai_session_id": "stable-session",
            "impersonate": "firefox136",
            "ua": "Firefox UA",
        })
        self.assertEqual(fallback["device_id"], "stable-device")
        self.assertEqual(fallback["oai_session_id"], "stable-session")
        self.assertEqual(fallback["impersonate"], "chrome136")
        self.assertIn("Chrome/136", fallback["ua"])
        self.assertIn("Google Chrome", fallback["sec_ch_ua"])

    def test_checkout_headers_keep_device_cookie_account_and_client_identity_aligned(self):
        account_id = "acct_header_test"
        token = ".".join([
            _b64url({"alg": "none"}),
            _b64url({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}),
            "signature",
        ])
        headers = flow._chatgpt_headers(
            token,
            "/backend-api/payments/checkout",
            device_id="device-123",
            session_id="session-123",
            fingerprint={"ua": "UA", "oai_language": "en-PH"},
        )
        self.assertEqual(headers["oai-device-id"], "device-123")
        self.assertEqual(headers["Cookie"], "oai-did=device-123")
        self.assertEqual(headers["oai-session-id"], "session-123")
        self.assertEqual(headers["Chatgpt-Account-Id"], account_id)
        self.assertTrue(headers["oai-client-version"].startswith("prod-"))
        self.assertTrue(headers["oai-client-build-number"])

    def test_checkout_matches_har_shape_and_marks_risk_block_retryable(self):
        class RiskSession:
            def __init__(self):
                self.request = None
                self.gets = []

            def get(self, url, **kwargs):
                self.gets.append((url, kwargs))
                return _Response(200, {"country_code": "PH"})

            def post(self, url, **kwargs):
                self.request = (url, kwargs)
                return _Response(403, {"detail": "network or account triggered unusual activity"})

        original = flow._new_session
        session = RiskSession()
        flow._new_session = lambda proxy, fingerprint=None: session
        try:
            result = flow.extract_gcash_custom_link(
                "header.payload.signature",
                "http://proxy.example",
                country="GB",
                currency="GBP",
            )
        finally:
            flow._new_session = original

        self.assertEqual(session.gets[0][0], flow._PRICING_CONFIG_URL + "/PH")
        self.assertEqual(session.request[1]["json"], {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "PH", "currency": "PHP"},
            "checkout_ui_mode": "custom",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
        })
        self.assertEqual(result["reason_code"], "checkout_risk_blocked")
        self.assertTrue(result["extra"]["retryable"])
        self.assertEqual(result["extra"]["upstream_status"], 403)
        self.assertIn("unusual activity", result["extra"]["upstream_detail"])
        self.assertIn("GCash", result["error"])

    def test_checkout_http_400_is_retryable_and_preserves_upstream_detail(self):
        class BadRequestSession:
            def get(self, url, **kwargs):
                return _Response(200, {"country_code": "PH"})

            def post(self, url, **kwargs):
                return _Response(400, {"detail": "invalid checkout request"})

        original = flow._new_session
        flow._new_session = lambda proxy, fingerprint=None: BadRequestSession()
        try:
            result = flow.extract_gcash_custom_link("header.payload.signature", "http://proxy.example")
        finally:
            flow._new_session = original

        self.assertEqual(result["reason_code"], "checkout_request_rejected")
        self.assertTrue(result["extra"]["retryable"])
        self.assertEqual(result["extra"]["upstream_status"], 400)
        self.assertIn("invalid checkout request", result["extra"]["upstream_detail"])

    def test_selects_only_the_observed_gcash_custom_method(self):
        self.assertEqual(
            flow._gcash_custom_payment_method_id([
                {"id": "cpmt_other"},
                {"id": flow._GCASH_CPMT_ID, "options": {"type": "static"}},
            ]),
            flow._GCASH_CPMT_ID,
        )
        self.assertEqual(flow._gcash_custom_payment_method_id([{"id": "cpmt_other"}]), "")

    def test_adyen_location_preserves_redirect_result_before_page_redirects(self):
        location = (
            "https://chatgpt.com/checkout/verify?stripe_session_id=checkout_123"
            "&processor_entity=openai_llc&redirectResult=opaque%2Bresult%3D"
        )
        captured = flow._checkout_verify_return_url(
            "https://checkoutshopper-live.adyen.com/checkoutshopper/checkoutPaymentReturn",
            location,
        )
        self.assertEqual(captured, location)
        self.assertEqual(flow._action_result_from_verify_url(captured), "opaque+result=")
        self.assertEqual(flow._checkout_verify_return_url("", "https://chatgpt.com/"), "")

    def test_extracts_chatgpt_account_id_from_access_token(self):
        account_id = "acct_test_123"
        token = ".".join([
            _b64url({"alg": "none"}),
            _b64url({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}),
            "signature",
        ])
        self.assertEqual(flow._jwt_account_id(token), account_id)

    def test_active_plus_requires_entitlement_and_not_delinquent(self):
        self.assertTrue(flow._has_active_plus({
            "data": [{"plan_type": "plus", "seats_entitled": 1, "is_delinquent": False}]
        }))
        self.assertFalse(flow._has_active_plus({
            "data": [{"plan_type": "plus", "seats_entitled": 0, "is_delinquent": False}]
        }))
        self.assertFalse(flow._has_active_plus({
            "data": [{"plan_type": "plus", "seats_entitled": 1, "is_delinquent": True}]
        }))

    def test_continue_uses_action_result_then_verifies_subscription(self):
        account_id = "acct_test_456"
        token = ".".join([
            _b64url({"alg": "none"}),
            _b64url({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}),
            "signature",
        ])
        session = _Session()
        messages = []

        ok = flow._server_finish_and_verify(
            session,
            token,
            "checkout_123",
            "opaque_redirect_result",
            "device_123",
            "session_123",
            {},
            10,
            lambda stage, status, message: messages.append((stage, status, message)),
        )

        self.assertTrue(ok)
        self.assertEqual(session.posts[0][0], flow._CONTINUE_URL)
        self.assertEqual(session.posts[0][1]["json"], {
            "checkout_session_id": "checkout_123",
            "action_result": {"redirectResult": "opaque_redirect_result"},
        })
        self.assertEqual(session.gets[0][1]["params"], {"account_id": account_id})
        self.assertTrue(any(stage == "subscriptions" and status == "ok" for stage, status, _ in messages))

    def test_long_lived_monitor_returns_image_from_the_same_browser_worker(self):
        original = flow._capture_qr_with_browser
        calls = []
        paid = []
        paid_event = threading.Event()

        def fake_capture(*args, **kwargs):
            calls.append(kwargs)
            kwargs["on_image"]("data:image/png;base64,same-browser-image")
            kwargs["on_paid"]("plus_active")
            paid_event.set()
            return ""

        flow._capture_qr_with_browser = fake_capture
        try:
            image = flow._start_qr_refresh_monitor(
                "https://m.gcash.com/example",
                object(),
                "http://proxy.example",
                10,
                on_image=lambda value: None,
                on_paid=paid.append,
                wait_for_first_image=True,
            )
            self.assertTrue(paid_event.wait(1))
        finally:
            flow._capture_qr_with_browser = original

        self.assertEqual(image, "data:image/png;base64,same-browser-image")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["keep_alive"])
        self.assertEqual(paid, ["plus_active"])

    def test_first_qr_event_finishes_before_monitor_returns_result(self):
        original = flow._capture_qr_with_browser
        callback_entered = threading.Event()
        callback_release = threading.Event()
        returned = threading.Event()
        result = {}

        def fake_capture(*args, **kwargs):
            kwargs["on_image"]("data:image/png;base64,serialized-first-image")
            kwargs["on_paid"]("plus_active")
            return ""

        def on_image(_value):
            callback_entered.set()
            callback_release.wait(1)

        def run_monitor():
            result["image"] = flow._start_qr_refresh_monitor(
                "https://m.gcash.com/example",
                object(),
                "http://proxy.example",
                10,
                on_image=on_image,
                wait_for_first_image=True,
            )
            returned.set()

        flow._capture_qr_with_browser = fake_capture
        try:
            monitor = threading.Thread(target=run_monitor)
            monitor.start()
            self.assertTrue(callback_entered.wait(1))
            self.assertFalse(returned.is_set())
            callback_release.set()
            monitor.join(1)
        finally:
            callback_release.set()
            flow._capture_qr_with_browser = original

        self.assertTrue(returned.is_set())
        self.assertEqual(result["image"], "data:image/png;base64,serialized-first-image")
        self.assertEqual(flow._QR_AUTO_REFRESH_SECONDS, 300.0)

    def test_monitor_stops_after_repeated_browser_exit(self):
        original_capture = flow._capture_qr_with_browser
        original_delay = flow._QR_BROWSER_RESTART_DELAY_SECONDS
        calls = []

        def fake_capture(*args, **kwargs):
            calls.append(kwargs)
            return ""

        flow._capture_qr_with_browser = fake_capture
        flow._QR_BROWSER_RESTART_DELAY_SECONDS = 0.01
        try:
            image = flow._start_qr_refresh_monitor(
                "https://m.gcash.com/example",
                object(),
                "http://proxy.example",
                1,
                on_image=lambda value: None,
                wait_for_first_image=True,
            )
        finally:
            flow._capture_qr_with_browser = original_capture
            flow._QR_BROWSER_RESTART_DELAY_SECONDS = original_delay

        self.assertEqual(image, "")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call.get("session_deadline") for call in calls))


if __name__ == "__main__":
    unittest.main()
