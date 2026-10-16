from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import parse_qsl, urlparse

import upi_link_service as upi
import upi_sentinel
import upi_cli


def fixture_attestation(deploy_id: str) -> str:
    """构造与 HAR 同形的 oai-web-deployment-attestation（JWT 结构）。"""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "ES256", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"version": 1, "track": "stable", "deployId": deploy_id, "issuedAt": 1, "expiresAt": 2}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.signature"


class FakeSentinelModule:
    """替身哨兵模块：记录 mint 入参，模拟 UPI 私有桥 / 共享桥两种签名。"""

    def __init__(self, so=None, legacy: bool = False) -> None:
        self.calls: list[dict] = []
        self.so = so
        self.legacy = legacy

    def mint_sentinel_sync(self, **kwargs):
        if self.legacy:
            allowed = {
                "flow", "device_id", "user_agent", "proxy", "cores", "page_url", "language",
                "timezone", "platform", "platform_label", "screen_w", "screen_h",
                "max_touch_points", "cookie_header", "timeout_s",
            }
            kwargs = {key: value for key, value in kwargs.items() if key in allowed}
        self.calls.append(kwargs)
        token = {
            "p": "proof",
            "t": "turnstile",
            "c": "challenge",
            "id": str(kwargs.get("device_id") or ""),
            "flow": str(kwargs.get("flow") or ""),
        }
        if self.so:
            token["_so"] = {
                "so": self.so,
                "c": "challenge",
                "id": str(kwargs.get("device_id") or ""),
                "flow": "chatgpt_checkout",
            }
        return token

    @staticmethod
    def sentinel_header_value(token):
        if not isinstance(token, dict) or not token.get("p") or not token.get("c"):
            return ""
        return json.dumps({k: v for k, v in token.items() if not str(k).startswith("_")})

    @staticmethod
    def so_header_value(so_token):
        if not isinstance(so_token, dict) or not so_token.get("so"):
            return ""
        return json.dumps(so_token)


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.base_proxy = "http://proxy.invalid:8080"
        self.attestation = "fixture.attestation"
        self.attestation_source = "browser"
        self.client_observation = "v1.r.p.fixture"
        self.client_version = "prod-fixture"
        self.client_build = "10000000"
        self.chrome_full = "136.0.0.0"
        self.kernel = "chrome"
        self.device_id = "fixture-device-id"
        self.session_id = "fixture-session-id"
        self.ua = "Fixture Chrome/136"
        self.accept_language = "hi-IN,hi;q=0.9"
        self.locale = "hi-IN"
        self.sec_ch_ua = '"Chromium";v="136"'
        self.sec_ch_ua_platform = '"Windows"'
        self.cookie_header = "oai-did=fixture-device-id"
        # 哨兵/风控上下文（对齐 UPI.har 的新字段，缺省值为空以便断言"初值"）
        self.region = "IN"
        self.timezone = "Asia/Kolkata"
        self.hardware_concurrency = 12
        self.platform = "Win32"
        self.platform_label = "Windows"
        self.max_touch_points = 0
        self.browser_sentinel_tokens: dict[str, str] = {}
        self.upi_sentinel_so: dict[str, str] = {}
        self.attestation_deploy_id = ""
        self.client_observation_at = 0.0
        self.stripe_request_ids = fixture_stripe_request_ids()
        self.browser_risk_cookie_count = 2

    def refresh_cookie_header(self) -> str:
        return self.cookie_header

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return 200, "{}", {"content-type": "application/json"}


def fixture_billing() -> dict[str, str]:
    return {
        "name": "Test Buyer",
        "email": "buyer@example.com",
        "line1": "40, Forjett Street",
        "line2": "Grant Road West",
        "city": "Mumbai",
        "state": "Maharashtra",
        "postal_code": "400026",
        "country": "IN",
    }


def fixture_stripe_request_ids() -> dict[str, str]:
    return {
        "guid": "guid_fixture",
        "muid": "muid_fixture",
        "sid": "sid_fixture",
        "stripe_js_id": "stripe-js-fixture",
    }


def install_live_browser_risk(client: RecordingClient, **_kwargs) -> dict[str, object]:
    client.attestation = "fixture.attestation"
    client.attestation_source = "browser"
    client.client_observation = "v1.r.p.fresh"
    client.client_observation_at = time.monotonic()
    client.stripe_request_ids = fixture_stripe_request_ids()
    client.browser_risk_cookie_count = 2
    return {
        "attestation": True,
        "attestation_source": "browser",
        "attestation_length": 100,
        "sentinel": True,
        "sentinel_length": 100,
        "observation": True,
        "browser_cookies": True,
        "browser_cookie_count": 2,
        "stripe_ids": True,
    }


class UpiHarContractTests(unittest.TestCase):
    def test_required_firefox_impersonation_never_falls_back_to_chrome(self):
        with patch.object(upi, "_curl_impersonate_supported", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "required impersonate profile: firefox133"):
                upi._pick_india_profile("firefox", "firefox133")

    def test_setup_intent_failure_is_terminal_for_the_current_checkout(self):
        self.assertEqual(upi.MAX_SETUP_INTENT_RETRIES, 0)
        message = upi.localize_upi_error("setup_attempt_failed: generic_decline")
        self.assertIn("当前 Checkout 已终止", message)

    def test_cli_marks_explicit_risk_block_as_terminal(self):
        argv = ["upi_cli.py", "--at", "header.payload.signature", "--proxy", "http://proxy.invalid:8080"]
        with patch.object(sys, "argv", argv), patch.object(
            upi_cli,
            "extract_upi_link",
            side_effect=RuntimeError("UPI checkout confirm blocked; result=blocked"),
        ), patch("builtins.print") as mocked_print:
            code = upi_cli.main()
        payload = json.loads(mocked_print.call_args_list[-1].args[0])
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason_code"], "upi_risk_blocked")
        self.assertFalse(payload["retryable"])

    def test_ineligible_promo_is_only_diagnostic(self):
        """优惠资格预检不得终止任务。

        源站从不调用 check_coupon，且明确把入口标记注释为"不作为活动资格判定"
        （app.py:1332-1333）；真正的判定是建单之后的实际应付金额。

        这里曾把 not_eligible / already_redeemed 抛成终态。实测同一账号同一条
        印度代理：源站能建单并走到「优惠未归零：amount=189736」，移植版却停在更早
        的预检上，报出源站不会产生的「账号无优惠资格」。
        """
        for state in ("not_eligible", "already_redeemed", "unknown", "eligible"):
            recorded = []
            with patch.object(upi, "_probe_upi_exit_city", return_value=""), patch.object(
                upi, "resolve_upi_access_token", return_value="header.payload.signature"
            ), patch.object(upi, "_jwt_email", return_value="buyer@example.com"), patch.object(
                upi, "_fetch_promo_coupon_state", return_value=state
            ), patch.object(
                upi,
                "_run_upi_attempt",
                lambda *args, **kwargs: recorded.append(kwargs) or {"ok": True, "link": "https://example.test/pay"},
            ):
                result = upi.extract_upi_link(
                    "header.payload.signature",
                    "http://proxy.invalid:8080",
                    use_promo=True,
                )
            self.assertTrue(recorded, f"state={state} 应当在预检之后继续建单")
            self.assertTrue(result.get("ok"), state)

    def test_promo_ineligible_is_retryable_across_proxies(self):
        """资格失败随代理出口变化，必须可换代理重试，而不是账号级终态。"""
        argv = ["upi_cli.py", "--at", "header.payload.signature", "--proxy", "http://proxy.invalid:8080"]
        with patch.object(sys, "argv", argv), patch.object(
            upi_cli,
            "extract_upi_link",
            side_effect=RuntimeError("upi promo not eligible: coupon eligibility precheck"),
        ), patch("builtins.print") as mocked_print:
            code = upi_cli.main()
        payload = json.loads(mocked_print.call_args_list[-1].args[0])
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason_code"], "upi_promo_ineligible")
        self.assertTrue(payload["retryable"])

    def test_auth_and_promo_preflight_can_reuse_the_checkout_client(self):
        client = RecordingClient()

        def request(method, url, **kwargs):
            client.calls.append({"method": method, "url": url, **kwargs})
            if url.endswith("/backend-api/me"):
                return 200, "{}", {}
            if "/promo_campaign/check_coupon" in url:
                return 200, json.dumps({"state": "eligible"}), {}
            raise AssertionError(f"unexpected request: {method} {url}")

        client.request = request
        token = upi.resolve_upi_access_token(
            "header.payload.signature",
            client.base_proxy,
            client=client,
        )
        state = upi._fetch_promo_coupon_state(
            token,
            client.base_proxy,
            client.base_proxy,
            client=client,
        )
        self.assertEqual(token, "header.payload.signature")
        self.assertEqual(state, "eligible")
        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertEqual(call["proxy"], client.base_proxy)
            self.assertEqual(call["headers"]["oai-device-id"], client.device_id)
            self.assertEqual(call["headers"]["oai-session-id"], client.session_id)

    def test_authenticated_socks_proxy_uses_local_browser_bridge(self):
        try:
            browser_proxy, bridge = upi._upi_browser_proxy(
                "socks5h://fixture-user:fixture-pass@127.0.0.1:65534"
            )
        except RuntimeError as exc:
            # 该桥复用 gcash_engine 的 Playwright 代理实现；本机若因该模块初始化
            # 失败（例如 BranchConfig 缺省参数）拿不到桥，就跳过——UPI 协议本身
            # 不依赖它（UPI_BROWSER_RISK 默认关闭）。
            self.skipTest(f"gcash 浏览器代理桥不可用：{exc}")
        try:
            self.assertTrue(browser_proxy.startswith("http://127.0.0.1:"))
            self.assertIsNotNone(bridge)
        finally:
            if bridge is not None:
                bridge.close()

    def test_cli_logs_use_momo_stage_names(self):
        with patch("builtins.print") as mocked_print:
            upi_cli.emit_step([{"name": "创建支付订单", "status": "ok", "detail": "印度 UPI 订单已创建"}])
        line = mocked_print.call_args.args[0]
        self.assertEqual(line, "[checkout] [ok] 印度 UPI 订单已创建")

    def test_billing_expands_state_code_and_keeps_line2(self):
        billing = upi.build_upi_billing(
            "buyer@example.com",
            {
                "name": "Test Buyer",
                "address": {
                    "line1": "40, Forjett Street",
                    "line2": "Grant Road West",
                    "city": "Mumbai",
                    "state": "MH",
                    "postal_code": "400026",
                },
            },
        )
        self.assertEqual(billing["state"], "Maharashtra")
        self.assertEqual(billing["line2"], "Grant Road West")

    def test_custom_elements_request_matches_har_contract(self):
        client = RecordingClient()

        def request(method, url, **kwargs):
            client.calls.append({"method": method, "url": url, **kwargs})
            return 200, json.dumps({"session_id": "elements_fixture"}), {}

        client.request = request
        result = upi._elements_session(
            client,
            stripe_pk="pk_test_fixture",
            amount=0,
            stripe_js_id="stripe-js-fixture",
            proxy=client.base_proxy,
            checkout_session_id="cs_live_fixture",
            checkout_ui_mode="custom",
            use_promo=True,
        )
        self.assertEqual(result["session_id"], "elements_fixture")
        query = dict(parse_qsl(urlparse(client.calls[0]["url"]).query, keep_blank_values=True))
        self.assertEqual(query["deferred_intent[amount]"], "0")
        self.assertEqual(query["deferred_intent[payment_method_types][2]"], "upi")
        self.assertEqual(query["checkout_session_id"], "cs_live_fixture")
        self.assertEqual(query["_stripe_version"], upi.STRIPE_CUSTOM_VERSION)

    def test_oaics_elements_request_uses_base_har_contract(self):
        client = RecordingClient()

        def request(method, url, **kwargs):
            client.calls.append({"method": method, "url": url, **kwargs})
            return 200, json.dumps({"session_id": "oaics_elements_fixture"}), {}

        client.request = request
        client.region = "IN"
        client.timezone = "Asia/Kolkata"
        result = upi._elements_session(
            client,
            customer_session_secret="cuss_fixture",
            stripe_pk="pk_live_fixture",
            amount=0,
            stripe_js_id="stripe-js-fixture",
            proxy=client.base_proxy,
            checkout_session_id="oaics_fixture",
            checkout_ui_mode="deferred",
            use_promo=True,
            payment_method_types=["card", "upi"],
        )
        self.assertEqual(result["session_id"], "oaics_elements_fixture")
        query = dict(parse_qsl(urlparse(client.calls[0]["url"]).query, keep_blank_values=True))
        self.assertEqual(query["customer_session_client_secret"], "cuss_fixture")
        self.assertEqual(query["_stripe_version"], upi.STRIPE_VERSION)
        self.assertNotIn("client_betas[0]", query)
        self.assertNotIn("client_betas[1]", query)
        self.assertEqual(query["browser_timezone"], "Asia/Kolkata")
        self.assertEqual(query["locale"], "hi-IN")
        self.assertEqual(query["deferred_intent[payment_method_types][0]"], "card")
        self.assertEqual(query["deferred_intent[payment_method_types][1]"], "upi")

    def test_oaics_confirmation_token_preserves_checkout_context(self):
        client = RecordingClient()

        def request(method, url, **kwargs):
            client.calls.append({"method": method, "url": url, **kwargs})
            return 200, json.dumps({"id": "ctoken_fixture"}), {}

        client.request = request
        billing = fixture_billing()
        billing["phone"] = "+919999999999"
        with patch.object(upi, "_stripe_runtime_version", return_value="runtime-fixture"):
            result = upi._confirmation_token(
                client,
                checkout_session_id="oaics_fixture",
                stripe_pk="pk_live_fixture",
                billing=billing,
                stripe_js_id="stripe-js-fixture",
                elements={
                    "session_id": "elements_fixture",
                    "config_id": "elements_config",
                },
                payment_method_types=["card", "upi"],
                proxy=client.base_proxy,
                time_on_page_ms=30000,
            )
        self.assertEqual(result["id"], "ctoken_fixture")
        form = client.calls[-1]["form"]
        self.assertEqual(form["payment_method_data[type]"], "upi")
        self.assertEqual(form["payment_method_data[allow_redisplay]"], "limited")
        self.assertEqual(
            form["payment_method_data[client_attribution_metadata][checkout_session_id]"],
            "oaics_fixture",
        )
        self.assertEqual(form["client_context[payment_method_types][0]"], "card")
        self.assertEqual(form["client_context[payment_method_types][1]"], "upi")
        self.assertNotIn("client_context[payment_method_types][2]", form)
        self.assertEqual(
            form["payment_method_data[billing_details][address][line2]"],
            "Grant Road West",
        )
        self.assertEqual(form["payment_method_data[billing_details][phone]"], "+919999999999")
        self.assertIn("runtime-fixture", form["payment_method_data[payment_user_agent]"])

    def test_oaics_state_refresh_uses_authenticated_checkout_route(self):
        client = RecordingClient()

        def request(method, url, **kwargs):
            client.calls.append({"method": method, "url": url, **kwargs})
            return 200, json.dumps({
                "customer_session_client_secret": "cuss_refreshed",
                "payment_method_types": ["card", "upi"],
            }), {}

        client.request = request
        state = upi._fetch_upi_oaics_state(
            client,
            "access-token",
            cs_id="oaics_fixture",
            processor_entity="openai_llc",
            proxy=client.base_proxy,
        )
        self.assertEqual(state["customer_session_client_secret"], "cuss_refreshed")
        self.assertEqual(client.calls[0]["method"], "GET")
        self.assertIn("/backend-api/payments/checkout/openai_llc/oaics_fixture", client.calls[0]["url"])
        self.assertEqual(client.calls[0]["headers"]["x-openai-target-route"], "/backend-api/payments/checkout/openai_llc/oaics_fixture")

    def test_upi_next_action_uses_hosted_instructions_as_copyable_link(self):
        payload = {
            "next_action": {
                "type": "upi_handle_redirect_or_display_qr_code",
                "upi_handle_redirect_or_display_qr_code": {
                    "hosted_instructions_url": "https://payments.stripe.com/upi/instructions/fixture",
                    "qr_code": {
                        "expires_at": 1788596700,
                        "image_url_png": "https://qr.stripe.com/live_fixture.png",
                        "image_url_svg": "https://qr.stripe.com/live_fixture.svg",
                    },
                },
            }
        }
        qr = upi._extract_upi_qr(payload)
        self.assertEqual(qr["hosted_instructions_url"], "https://payments.stripe.com/upi/instructions/fixture")
        self.assertEqual(qr["qr_url"], "https://qr.stripe.com/live_fixture.svg")
        self.assertEqual(upi._upi_copyable_link(payload, qr), "https://payments.stripe.com/upi/instructions/fixture")
        no_hosted = {"next_action": {"upi_handle_redirect_or_display_qr_code": {
            "qr_code": {"image_url_png": "https://qr.stripe.com/live_fixture.png"}
        }}}
        self.assertEqual(upi._upi_copyable_link(no_hosted), "")
        legacy_deep_link = {"next_action": {"upi_handle_redirect_or_display_qr_code": {
            "upi_uri": "upi://pay?pa=fixture@upi&am=0",
            "qr_code": {"image_url_png": "https://qr.stripe.com/live_fixture.png"},
        }}}
        self.assertEqual(upi._upi_copyable_link(legacy_deep_link), "upi://pay?pa=fixture@upi&am=0")

    def test_full_address_update_and_confirm_use_advanced_config(self):
        client = RecordingClient()

        def request(method, url, **kwargs):
            client.calls.append({"method": method, "url": url, **kwargs})
            return 200, json.dumps({"submission_attempt": {"state": "requires_approval"}}), {}

        client.request = request
        billing = fixture_billing()
        elements = {"session_id": "elements_fixture", "config_id": "elements_config"}
        upi._stripe_custom_checkout_update(
            client,
            cs_id="cs_live_fixture",
            stripe_pk="pk_test_fixture",
            stripe_js_id="stripe-js-fixture",
            elements=elements,
            billing=billing,
            proxy=client.base_proxy,
            address_phase="full",
        )
        update_form = client.calls[-1]["form"]
        self.assertEqual(update_form["tax_region[state]"], "Maharashtra")
        self.assertEqual(update_form["tax_region[line2]"], "Grant Road West")
        self.assertEqual(update_form["_stripe_version"], upi.STRIPE_CUSTOM_VERSION)

        upi._stripe_custom_checkout_confirm(
            client,
            cs_id="cs_live_fixture",
            processor_entity="openai_llc",
            stripe_pk="pk_test_fixture",
            stripe_js_id="stripe-js-fixture",
            elements=elements,
            init_data={
                "config_id": "init_config",
                "init_checksum": "checksum",
                "version": "runtime-fixture",
            },
            billing=billing,
            amount=0,
            checkout_client_secret="cs_live_fixture_secret_value",
            checkout_config_id="updated_config",
            proxy=client.base_proxy,
            request_ids=client.stripe_request_ids,
            use_promo=True,
        )
        confirm_form = client.calls[-1]["form"]
        self.assertEqual(
            confirm_form["payment_method_data[client_attribution_metadata][checkout_config_id]"],
            "init_config",
        )
        self.assertEqual(
            confirm_form["client_attribution_metadata[checkout_config_id]"],
            "updated_config",
        )
        self.assertEqual(confirm_form["version"], "runtime-fixture")
        self.assertEqual(confirm_form["passive_captcha_ekey"], "")

    def test_custom_checkout_sequence_matches_har(self):
        client = RecordingClient()
        client.attestation = ""
        events: list[str] = []

        def update(*args, address_phase, **kwargs):
            events.append("update:" + address_phase)
            return {"config_id": "updated_config", "amount": 0}

        def request(method, url, **kwargs):
            path = urlparse(url).path
            if path.endswith("/sentinel/ping"):
                return 200, json.dumps({"status": "ok"}), {}
            if path.endswith("/checkout/taxes"):
                events.append("taxes")
                return 200, json.dumps({"checkout_session": {"amount_total": 0}}), {}
            if path.endswith("/checkout/snapshot"):
                events.append("snapshot")
                return 204, "", {}
            raise AssertionError(f"unexpected request: {method} {url}")

        client.request = request
        replacements = (
            ("_capture_upi_browser_risk", install_live_browser_risk),
            ("_fetch_web_attestation", lambda *args, **kwargs: ""),
            ("_warmup_india_client", lambda *args, **kwargs: None),
            ("_human_pause", lambda *args, **kwargs: None),
            ("_stripe_custom_checkout_init", lambda *args, **kwargs: events.append("init") or {"config_id": "init_config", "amount": 0}),
            ("_elements_session", lambda *args, **kwargs: events.append("elements") or {"session_id": "elements_fixture", "config_id": "elements_config", "ordered_payment_method_types_and_wallets": ["upi"]}),
            ("_stripe_custom_checkout_update", update),
            ("_stripe_custom_checkout_fetch", lambda *args, **kwargs: events.append("fetch") or {"amount": 0}),
            ("_run_upi_custom_checkout", lambda *args, **kwargs: events.append("run") or {"ok": True, "amount": kwargs["amount"]}),
        )
        with ExitStack() as stack:
            for name, replacement in replacements:
                stack.enter_context(patch.object(upi, name, replacement))
            result = upi._run_upi_india_phase(
                client,
                token="header.payload.signature",
                step=lambda *args: None,
                steps=[],
                cs_id="cs_live_fixture",
                processor_entity="openai_llc",
                stripe_pk="pk_test_fixture",
                checkout_client_secret="cs_live_fixture_secret_value",
                checkout_ui_mode="custom",
                referer="https://chatgpt.com/checkout/openai_llc/cs_live_fixture",
                india_base=client.base_proxy,
                billing=fixture_billing(),
                promo_payload={"checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}}},
                use_promo=True,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(
            events,
            [
                "init",
                "elements",
                "update:country",
                "snapshot",
                "run",
            ],
        )

    def test_browser_bootstrap_runs_when_observation_is_missing(self):
        client = RecordingClient()
        client.client_observation = ""
        captured: list[str] = []

        def capture(active_client, **kwargs):
            captured.append("browser")
            return install_live_browser_risk(active_client)

        def request(method, url, **kwargs):
            if url.endswith("/backend-api/payments/checkout"):
                return 200, json.dumps(
                    {
                        "checkout_session_id": "cs_live_fixture",
                        "processor_entity": "openai_llc",
                        "checkout_ui_mode": "custom",
                        "publishable_key": "pk_test_fixture",
                        "client_secret": "cs_live_fixture_secret_value",
                        "payment_method_types": ["upi"],
                        "promo_campaign": {"promo_campaign_id": "plus-1-month-free"},
                        "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
                    }
                ), {}
            raise AssertionError(f"unexpected request: {method} {url}")

        client.request = request
        replacements = (
            ("_warmup_upi_session", lambda *args, **kwargs: (200, "oai-did=fixture-device-id")),
            ("_fetch_web_attestation", lambda *args, **kwargs: "fixture.attestation"),
            ("_capture_upi_browser_risk", capture),
            ("_upi_sentinel_headers", lambda *args, **kwargs: {"openai-sentinel-token": "fixture"}),
            ("_sentinel_ping", lambda *args, **kwargs: None),
            ("_warmup_india_client", lambda *args, **kwargs: None),
            ("_human_pause", lambda *args, **kwargs: None),
            ("_fetch_checkout_page_data", lambda *args, **kwargs: (200, "")),
            ("_run_upi_india_phase", lambda *args, **kwargs: {"ok": True}),
        )
        with ExitStack() as stack:
            for name, replacement in replacements:
                stack.enter_context(patch.object(upi, name, replacement))
            result = upi._run_upi_attempt(
                client,
                "header.payload.signature",
                lambda *args: None,
                [],
                promotion_proxy=client.base_proxy,
                billing=fixture_billing(),
                use_promo=True,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(captured, ["browser"])
        self.assertEqual(client.client_observation, "v1.r.p.fresh")

    def test_missing_browser_proofs_fail_before_real_checkout(self):
        client = RecordingClient()
        client.attestation = ""
        client.client_observation = ""
        stages: list[tuple[str, str, str]] = []

        def request(method, url, **kwargs):
            if url.endswith("/backend-api/payments/checkout"):
                return 200, json.dumps(
                    {
                        "checkout_session_id": "cs_live_fixture",
                        "processor_entity": "openai_llc",
                        "checkout_ui_mode": "custom",
                        "publishable_key": "pk_test_fixture",
                        "client_secret": "cs_live_fixture_secret_value",
                        "payment_method_types": ["upi"],
                        "promo_campaign": {"promo_campaign_id": "plus-1-month-free"},
                        "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
                    }
                ), {}
            raise AssertionError(f"unexpected request: {method} {url}")

        client.request = request
        replacements = (
            ("_warmup_upi_session", lambda *args, **kwargs: (200, "oai-did=fixture-device-id")),
            ("_fetch_web_attestation", lambda *args, **kwargs: ""),
            ("_capture_upi_browser_risk", lambda *args, **kwargs: {
                "attestation": False,
                "attestation_source": "none",
                "attestation_length": 0,
                "sentinel": True,
                "sentinel_length": 100,
                "observation": False,
                "error": "fixture browser did not expose proof headers",
            }),
            ("_upi_sentinel_headers", lambda *args, **kwargs: {"openai-sentinel-token": "fixture"}),
            ("_sentinel_ping", lambda *args, **kwargs: None),
            ("_warmup_india_client", lambda *args, **kwargs: None),
            ("_human_pause", lambda *args, **kwargs: None),
            ("_fetch_checkout_page_data", lambda *args, **kwargs: (200, "")),
            ("_run_upi_india_phase", lambda *args, **kwargs: {"ok": True}),
        )
        with ExitStack() as stack:
            for name, replacement in replacements:
                stack.enter_context(patch.object(upi, name, replacement))
            with self.assertRaisesRegex(RuntimeError, "风控上下文生成失败"):
                upi._run_upi_attempt(
                    client,
                    "header.payload.signature",
                    lambda *args: stages.append(args),
                    [],
                    promotion_proxy=client.base_proxy,
                    billing=fixture_billing(),
                    use_promo=True,
                )
        self.assertEqual(client.client_observation, "")


    @unittest.skip("SetupIntent 风控现在交给 Node 换代理+指纹完整重建 Checkout")
    def test_setup_intent_retries_only_stage_and_rotates_proxy(self):
        client = RecordingClient()
        confirm_proxies: list[str] = []
        seen_sessions: list[tuple[str, str, str]] = []
        seen_request_ids: list[dict[str, str]] = []
        attempts = iter([
            RuntimeError("setup_attempt_failed: generic_decline"),
            RuntimeError("setup_attempt_failed"),
            {"submission_attempt": {"state": "succeeded"}},
        ])

        def confirm(active_client, **kwargs):
            confirm_proxies.append(kwargs["proxy"])
            seen_sessions.append((kwargs["stripe_js_id"], kwargs["elements"]["session_id"], kwargs["cs_id"]))
            seen_request_ids.append(kwargs["request_ids"])
            outcome = next(attempts)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def poll(active_client, **kwargs):
            return {"submission_attempt": {"state": "succeeded"}, "url": "upi://pay?pa=fixture@upi&am=0"}

        with patch.object(upi, "_stripe_custom_checkout_confirm", confirm), patch.object(upi, "_stripe_custom_checkout_poll", poll):
            result = upi._run_upi_custom_checkout(
                client,
                token="token",
                step=lambda *args: None,
                steps=[],
                cs_id="cs_fixture",
                processor_entity="openai_llc",
                stripe_pk="pk_fixture",
                checkout_client_secret="cs_fixture_secret",
                stripe_js_id="stripe-js-fixed",
                elements={"session_id": "elements-fixed"},
                init_data={"config_id": "config-fixed"},
                checkout_config_id="config-latest",
                amount=0,
                billing=fixture_billing(),
                proxy="http://proxy-a:1000",
                setup_proxies=["http://proxy-a:1000", "http://proxy-b:1000", "http://proxy-c:1000"],
                referer="https://chatgpt.com/checkout/openai_llc/cs_fixture",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(confirm_proxies, ["http://proxy-a:1000", "http://proxy-b:1000", "http://proxy-c:1000"])
        self.assertEqual(len(set(seen_sessions)), 1)
        self.assertEqual(len({tuple(sorted(item.items())) for item in seen_request_ids}), 1)
        self.assertEqual(result["provider_redirect_url"], "upi://pay?pa=fixture@upi&am=0")


    def test_force_zero_skips_local_interrupt(self):
        # 强压 0 元：金额非 0 / 缺失时不再抛错，返回 0 继续 confirm/start。
        warnings: list[tuple[str, object]] = []
        self.assertEqual(upi._require_zero_due(199, "优惠后", force=True, on_warn=lambda s, a: warnings.append((s, a))), 0)
        self.assertEqual(upi._require_zero_due(None, "优惠后", force=True, on_warn=lambda s, a: warnings.append((s, a))), 0)
        self.assertEqual(len(warnings), 2)
        # 未开启强压时仍按原行为抛错。
        with self.assertRaises(RuntimeError):
            upi._require_zero_due(199, "优惠后")

    def test_force_payment_method_skips_upi_offered_interrupt(self):
        # 强制支付方式：payment_method_types 不含 upi 时不再抛错。
        warnings: list[str] = []
        methods = upi._ensure_upi_offered(
            {"payment_method_types": ["card"]}, "IN bootstrap",
            force=True, on_warn=lambda phase, ms: warnings.append(phase),
        )
        self.assertEqual(methods, ["card"])
        self.assertEqual(warnings, ["IN bootstrap"])
        with self.assertRaises(RuntimeError):
            upi._ensure_upi_offered({"payment_method_types": ["card"]}, "IN bootstrap")

    def test_auto_mode_detects_cs_live_vs_oaics(self):
        client = RecordingClient()
        checkout_ui_modes: list[str] = []

        def request(method, url, **kwargs):
            if url.endswith("/backend-api/payments/checkout"):
                body = kwargs.get("json_body") or {}
                # Native auto mode lets the server select the checkout type.
                self.assertNotIn("checkout_ui_mode", body)
                return 200, json.dumps(
                    {
                        "checkout_session_id": session_id,
                        "processor_entity": "openai_llc",
                        "checkout_ui_mode": "custom",
                        "publishable_key": "pk_test_fixture",
                        "client_secret": session_id + "_secret_value" if session_id.startswith('cs_') else None,
                        "customer_session_client_secret": "cuss_fixture" if session_id.startswith('oaics_') else None,
                        "payment_method_types": ["upi"],
                        "promo_campaign": {"promo_campaign_id": "plus-1-month-free"},
                        "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
                    }
                ), {}
            raise AssertionError(f"unexpected request: {method} {url}")

        def india_phase(active_client, **kwargs):
            checkout_ui_modes.append(kwargs["checkout_ui_mode"])
            return {"ok": True}

        client.request = request
        replacements = (
            ("_warmup_upi_session", lambda *args, **kwargs: (200, "oai-did=fixture-device-id")),
            ("_fetch_web_attestation", lambda *args, **kwargs: "fixture.attestation"),
            ("_capture_upi_browser_risk", install_live_browser_risk),
            ("_upi_sentinel_headers", lambda *args, **kwargs: {"openai-sentinel-token": "fixture"}),
            ("_sentinel_ping", lambda *args, **kwargs: None),
            ("_warmup_india_client", lambda *args, **kwargs: None),
            ("_human_pause", lambda *args, **kwargs: None),
            ("_fetch_checkout_page_data", lambda *args, **kwargs: (200, "")),
            ("_run_upi_india_phase", india_phase),
        )
        with ExitStack() as stack:
            for name, replacement in replacements:
                stack.enter_context(patch.object(upi, name, replacement))
            for session_id in ('cs_live_fixture', 'cs_test_fixture', 'oaics_fixture'):
                upi._run_upi_attempt(
                    client,
                    "header.payload.signature",
                    lambda *args: None,
                    [],
                    promotion_proxy=client.base_proxy,
                    billing=fixture_billing(),
                    use_promo=True,
                    checkout_ui_mode="auto",
                )
        self.assertEqual(checkout_ui_modes, ["custom", "custom", "deferred"])

    def test_qr_event_forwards_stripe_urls_without_inline_base64(self):
        # 现行实现（见 _emit_upi_qr docstring）：只透传 Stripe 的二维码 URL，
        # 不再在 Python 侧下载并内联 base64 —— 那会让 qr_refresh 事件膨胀到 MB 级，
        # 超过 Node 子进程行缓冲后被整行丢弃，连二维码 URL 一起丢。
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(121))

        class BinaryQrClient:
            def request_bytes(self, *args, **kwargs):
                return 200, png, {"content-type": "image/png"}

        events = []
        upi._emit_upi_qr(
            {
                "qr_url": "https://qr.stripe.com/test.png",
                "qr_image_url": "https://qr.stripe.com/test.png",
                "image_url_png": "https://qr.stripe.com/test.png",
                "expires_at": 1789594965,
            },
            events.append,
            lambda *args: None,
            BinaryQrClient(),
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "qr_refresh")
        self.assertEqual(events[0]["qr_url"], "https://qr.stripe.com/test.png")
        self.assertNotIn("qr_image", events[0])

    def test_paid_watch_emits_success_from_same_checkout_session(self):
        client = RecordingClient()
        events = []
        payload = {
            "submission_attempt": {"state": "succeeded"},
            "setup_intent": {"status": "succeeded"},
        }
        with patch.object(upi, "_upi_subscriptions_active", return_value=False), patch.object(
            upi, "_stripe_custom_checkout_fetch", return_value=payload
        ) as fetch:
            result = upi._wait_upi_paid(
                client,
                "header.payload.signature",
                cs_id="cs_live_fixture",
                stripe_pk="pk_fixture",
                stripe_js_id="stripe-js-fixture",
                elements={"session_id": "elements_fixture"},
                proxy=client.base_proxy,
                timeout_seconds=30,
                on_qr_update=events.append,
                sleep_fn=lambda _: None,
            )
        self.assertTrue(result["paid"])
        self.assertEqual(result["confirmed_via"], "stripe")
        self.assertEqual(events[-1]["event"], "payment_status")
        self.assertEqual(events[-1]["payment_status"], "succeeded")
        fetch.assert_called_once()

    def test_paid_watch_does_not_redownload_same_qr_png(self):
        client = RecordingClient()
        events = []
        payload = {
            "submission_attempt": {"state": "succeeded"},
            "setup_intent": {
                "status": "succeeded",
                "next_action": {
                    "type": "upi_handle_redirect_or_display_qr_code",
                    "upi_handle_redirect_or_display_qr_code": {
                        "hosted_instructions_url": "https://payments.stripe.com/upi/instructions/fixture",
                        "qr_code": {
                            "image_url_png": "https://qr.stripe.com/fixture.png",
                            "image_url_svg": "https://qr.stripe.com/fixture.svg",
                        },
                    },
                },
            },
        }
        with patch.object(upi, "_upi_subscriptions_active", return_value=False), patch.object(
            upi, "_stripe_custom_checkout_fetch", return_value=payload
        ), patch.object(upi, "_emit_upi_qr", wraps=upi._emit_upi_qr) as emit_qr:
            result = upi._wait_upi_paid(
                client,
                "header.payload.signature",
                cs_id="cs_live_fixture",
                stripe_pk="pk_fixture",
                stripe_js_id="stripe-js-fixture",
                elements={"session_id": "elements_fixture"},
                proxy=client.base_proxy,
                timeout_seconds=30,
                on_qr_update=events.append,
                sleep_fn=lambda _: None,
            )
        self.assertTrue(result["paid"])
        self.assertIsNone(emit_qr.call_args.args[3])
        self.assertTrue(any(event.get("qr_url") for event in events))


class UpiHar20260917AlignmentTests(unittest.TestCase):
    """2026-09-17 印度 UPI.har 对齐回归：deployId / SO 时机 / 观察证明轮换 / 账单身份。"""

    # ── 哨兵指纹数组（p[]）与 HAR 逐位对齐 ──────────────────────────────
    def test_attestation_deploy_id_is_parsed_from_jwt_payload(self):
        token = fixture_attestation("c63495d40cbaaab25a58e53e1207430a35bb2343")
        self.assertEqual(upi._attestation_deploy_id(token), "c63495d40cbaaab25a58e53e1207430a35bb2343")
        self.assertEqual(upi._attestation_deploy_id(""), "")
        self.assertEqual(upi._attestation_deploy_id("not-a-jwt"), "")
        self.assertEqual(upi._attestation_deploy_id("a.@@@.c"), "")

    def test_attestation_deploy_id_accepts_real_two_part_format(self):
        # 线上真实格式是 "<base64url(payload)>.<base64url(signature)>"（无 JWT header）
        payload = base64.urlsafe_b64encode(
            json.dumps({"version": 1, "track": "stable", "deployId": "c63495d40cbaaab25a58e53e1207430a35bb2343"}).encode()
        ).decode().rstrip("=")
        self.assertEqual(
            upi._attestation_deploy_id(payload + ".c2lnbmF0dXJl"),
            "c63495d40cbaaab25a58e53e1207430a35bb2343",
        )
        # 三段 JWT 仍然兼容
        self.assertEqual(
            upi._attestation_deploy_id("h." + payload + ".sig"),
            "c63495d40cbaaab25a58e53e1207430a35bb2343",
        )

    def test_page_param_name_follows_har(self):
        self.assertEqual(upi._page_param_name("https://chatgpt.com/?promo_campaign=plus-1-month-free"), "promo_campaign")
        self.assertEqual(upi._page_param_name("https://chatgpt.com/checkout"), "")
        self.assertEqual(upi._page_param_name(""), "")

    def test_mint_receives_deploy_id_and_page_param(self):
        client = RecordingClient()
        client.attestation = fixture_attestation("deploy-fixture-1")
        client.upi_sentinel_so = {}
        module = FakeSentinelModule()
        with patch.object(upi, "_load_upi_sentinel_module", return_value=module):
            headers = upi._upi_sentinel_headers(
                "http://proxy.invalid:8080",
                client.device_id,
                "chatgpt_checkout",
                client,
                "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            )
        self.assertIn("openai-sentinel-token", headers)
        self.assertEqual(module.calls[0]["deploy_id"], "deploy-fixture-1")
        self.assertEqual(module.calls[0]["page_param"], "promo_campaign")
        self.assertEqual(module.calls[0]["device_id"], client.device_id)

    def test_mint_receives_live_sentinel_version(self):
        client = RecordingClient()
        client.sentinel_version = "20260917123a"
        module = FakeSentinelModule()
        with patch.object(upi, "_load_upi_sentinel_module", return_value=module):
            upi._upi_sentinel_headers(
                "http://proxy.invalid:8080",
                client.device_id,
                "chatgpt_checkout",
                client,
                "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            )
        self.assertEqual(module.calls[0]["sentinel_version"], "20260917123a")

    def test_sentinel_version_env_override(self):
        with patch.dict(os.environ, {"UPI_SENTINEL_VERSION": "20260101000a"}):
            client = upi.UpiHttpClient("http://proxy.invalid:8080", region="IN")
            try:
                self.assertEqual(client.sentinel_version, "20260101000a")
            finally:
                client.close()

    def test_checkout_caches_so_and_approval_reuses_it(self):
        client = RecordingClient()
        client.upi_sentinel_so = {}
        checkout_module = FakeSentinelModule(so="so-from-checkout")
        with patch.object(upi, "_load_upi_sentinel_module", return_value=checkout_module):
            checkout_headers = upi._upi_sentinel_headers(
                "http://proxy.invalid:8080",
                client.device_id,
                "chatgpt_checkout",
                client,
                "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            )
        # HAR #12：建单只带 sentinel 主令牌，不带 SO。
        self.assertIn("openai-sentinel-token", checkout_headers)
        self.assertNotIn("OpenAI-Sentinel-SO-Token", checkout_headers)
        self.assertEqual(client.upi_sentinel_so.get("chatgpt_checkout", ""), '{"so": "so-from-checkout", "c": "challenge", "id": "fixture-device-id", "flow": "chatgpt_checkout"}')
        # HAR #215/#238：approval flow 的响应不下发 so → approve 复用建单缓存（HAR #228）。
        approval_module = FakeSentinelModule(so=None)
        with patch.object(upi, "_load_upi_sentinel_module", return_value=approval_module):
            approval_headers = upi._upi_sentinel_headers(
                "http://proxy.invalid:8080",
                client.device_id,
                "checkout_session_approval",
                client,
                "https://chatgpt.com/checkout/openai_llc/cs_live_fixture",
            )
        self.assertEqual(json.loads(approval_headers["OpenAI-Sentinel-SO-Token"])["so"], "so-from-checkout")

    def test_shared_bridge_signature_still_supported(self):
        client = RecordingClient()
        client.upi_sentinel_so = {}
        legacy = FakeSentinelModule(legacy=True)
        with patch.object(upi, "_load_upi_sentinel_module", return_value=legacy):
            headers = upi._upi_sentinel_headers(
                "http://proxy.invalid:8080",
                client.device_id,
                "chatgpt_checkout",
                client,
                "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            )
        self.assertIn("openai-sentinel-token", headers)
        self.assertNotIn("deploy_id", legacy.calls[0])

    def test_local_sentinel_module_is_loaded_from_upi_engine(self):
        module = upi._load_upi_sentinel_module()
        self.assertTrue(callable(getattr(module, "mint_sentinel_sync", None)))
        self.assertEqual(os.path.basename(str(getattr(module, "__file__", ""))), "upi_sentinel.py")

    def test_sentinel_daemon_client_is_optional_and_fails_soft(self):
        # 常驻桥是 momo/gopay 同款加速：没配 socket 时静默回退一次性进程
        with patch.dict(os.environ, {"UPI_SENTINEL_DAEMON": ""}):
            self.assertTrue(upi_sentinel._daemon_enabled())
        with patch.dict(os.environ, {"UPI_SENTINEL_DAEMON": "0"}):
            self.assertFalse(upi_sentinel._daemon_enabled())
        with patch.dict(os.environ, {"UPI_SENTINEL_DAEMON_SOCKET": "/tmp/definitely-not-here.sock"}):
            self.assertEqual(upi_sentinel._daemon_sockets(), [])
            # 无 AF_UNIX（Windows）或 socket 不存在时返回 None，由调用方回退一次性进程
            self.assertIsNone(upi_sentinel._mint_via_daemon(b"{}", 2.0))

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过哨兵指纹自检")
    def test_fingerprint_array_matches_har_fields(self):
        array = upi_sentinel.selftest_config_array(
            device_id="fixture-device-id",
            deploy_id="c63495d40cbaaab25a58e53e1207430a35bb2343",
            language="hi-IN",
            timezone="Asia/Kolkata",
            page_url="https://chatgpt.com/?promo_campaign=plus-1-month-free",
            cores=12,
            user_agent="Fixture Chrome/153",
            timeout_s=60.0,
        )
        self.assertEqual(len(array), 25)
        self.assertEqual(array[0], 3000)
        self.assertEqual(array[2], 4395630592)
        self.assertEqual(array[5], "https://chatgpt.com/backend-api/sentinel/sdk.js")
        self.assertEqual(array[6], "prod-c63495d40cbaaab25a58e53e1207430a35bb2343")
        self.assertEqual(array[7], "hi-IN")
        self.assertEqual(array[8], "hi-IN")
        self.assertEqual(array[15], "promo_campaign")
        self.assertEqual(array[16], 12)
        self.assertEqual(list(array[18:25]), [0] * 7)
        self.assertIn("GMT+0530", str(array[1]))

    @unittest.skipUnless(shutil.which("node"), "node 不可用，跳过哨兵指纹自检")
    def test_probe_fields_rotate_but_session_id_is_stable(self):
        samples = [
            upi_sentinel.selftest_config_array(
                device_id="fixture-device-id",
                language="hi-IN",
                timezone="Asia/Kolkata",
                timeout_s=60.0,
            )
            for _ in range(5)
        ]
        self.assertEqual(len({row[14] for row in samples}), 1)          # sid 由 device 派生
        self.assertGreater(len({tuple(row[10:13]) for row in samples}), 1)  # 探针每次随机

    # ── 客户端身份（反风控一致性）：oai-client-version / build / 标记头 ──
    def test_client_identity_follows_page_build_then_attestation(self):
        client = RecordingClient()
        page_html = (
            '<html class="x" lang="hi-IN" '
            'data-build="prod-43b1c8c37a40012814d5c4b46c679ed84d9ae33d" '
            'data-seq="10884122" data-theme="light">'
        )
        upi._apply_upi_page_identity(client, page_html)
        self.assertEqual(client.client_version, "prod-43b1c8c37a40012814d5c4b46c679ed84d9ae33d")
        self.assertEqual(client.client_build, "10884122")
        # 部署证明（服务端为本会话签发）优先于页面 data-build
        client.attestation = fixture_attestation("c63495d40cbaaab25a58e53e1207430a35bb2343")
        upi._apply_upi_page_identity(client, page_html)
        self.assertEqual(client.client_version, "prod-c63495d40cbaaab25a58e53e1207430a35bb2343")
        self.assertEqual(client.client_build, "10884122")
        self.assertEqual(upi._upi_client_deploy_id(client), "c63495d40cbaaab25a58e53e1207430a35bb2343")
        # 发头前会再次对齐：deployId 存在时 version 必须是 prod-<deployId>
        headers = {k.lower(): v for k, v in upi._chatgpt_headers(client, "h.p.s").items()}
        self.assertEqual(headers["oai-client-version"], "prod-c63495d40cbaaab25a58e53e1207430a35bb2343")
        self.assertEqual(headers["oai-client-build-number"], "10884122")

    def test_chatgpt_headers_include_real_frontend_markers(self):
        client = RecordingClient()
        client.attestation = ""
        headers = {k.lower(): v for k, v in upi._chatgpt_headers(client, "h.p.s").items()}
        # HAR #12/#228 等请求固定携带的三个前端标记头
        self.assertEqual(headers["x-openai-web-frontend"], "core_web")
        self.assertEqual(headers["x-openai-codex-window-type"], "not_applicable")
        self.assertEqual(headers["x-oai-is-pending-updates"], '{"v":3,"updates":[]}')
        # oai-telemetry 动态生成（HAR #12 形如 [1,801.4,8,64,42,2,0,805]），不再是静态 [1,null]
        self.assertRegex(headers["oai-telemetry"], r"^\[1,\d+(\.\d+)?,\d+,\d+,\d+,2,0,\d+(\.\d+)?\]$")

    def test_telemetry_is_dynamic_and_self_consistent(self):
        client = RecordingClient()
        first = upi._upi_telemetry(client)
        second = upi._upi_telemetry(client)
        self.assertNotEqual(first, second)
        for value in (first, second):
            parts = json.loads(value)
            self.assertEqual(parts[0], 1)
            self.assertEqual(parts[5], 2)
            self.assertEqual(parts[6], 0)
            self.assertGreater(parts[1], 0)
            self.assertGreaterEqual(parts[7], parts[1])   # d ≈ elapsed（HAR #12: 805 ≈ 801.4）
            self.assertLessEqual(parts[2], 60)

    def test_warmup_reads_payment_methods_before_pricing(self):
        client = RecordingClient()
        account_id = "cbbf533c-854f-4bd2-a2fc-871a4bf45a57"
        payload = base64.urlsafe_b64encode(
            json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}).encode()
        ).decode().rstrip("=")
        token = "header." + payload + ".sig"
        upi._warmup_india_client(client, token, "http://proxy.invalid:8080")
        urls = [call["url"] for call in client.calls]
        self.assertTrue(urls, "warmup 至少发起一次请求")
        self.assertEqual(urls[0], "https://chatgpt.com/backend-api/payments/payment_methods?account_id=" + account_id)
        first = client.calls[0]
        # target-path/route 不带 query（与 HAR #1 一致）
        self.assertEqual(first["headers"]["x-openai-target-route"], "/backend-api/payments/payment_methods")
        self.assertTrue(any("checkout_pricing_config" in url for url in urls))

    # ── 自定义 Checkout 的 Stripe 字段与 HAR 取值一致 ────────────────
    def test_stripe_locale_and_timezone_follow_session_fingerprint(self):
        client = RecordingClient()
        self.assertEqual(upi._stripe_browser_locale(client), "hi-IN")
        self.assertEqual(upi._stripe_browser_timezone(client), "Asia/Calcutta")
        client.locale = "vi-VN"
        client.timezone = "Asia/Ho_Chi_Minh"
        self.assertEqual(upi._stripe_browser_locale(client), "vi-VN")
        self.assertEqual(upi._stripe_browser_timezone(client), "Asia/Saigon")

    def test_custom_checkout_confirm_field_values_match_har(self):
        captured: dict = {}

        class ConfirmStub(RecordingClient):
            def request(self, method, url, **kwargs):
                if kwargs.get("form"):
                    captured.update(kwargs["form"])
                return 200, '{"id":"ppage_fixture","status":"open"}', {}

        with patch.object(
            upi,
            "_upi_passive_captcha_fields",
            lambda *args, **kwargs: {"passive_captcha_token": "P1_fixture", "passive_captcha_ekey": ""},
        ):
            upi._stripe_custom_checkout_confirm(
                ConfirmStub(),
                cs_id="cs_live_fixture",
                processor_entity="openai_llc",
                stripe_pk="pk_live_fixture",
                stripe_js_id="stripe-js-fixture",
                elements={
                    "session_id": "elements_fixture",
                    "config_id": "cfg_elements",
                    "passive_captcha": {"site_key": "fixture-key"},
                },
                init_data={"config_id": "cfg_init", "init_checksum": "init_checksum_fixture"},
                billing=fixture_billing(),
                amount=0,
                checkout_client_secret="cs_live_fixture_secret",
                checkout_config_id="cfg_state",
                proxy="http://proxy.invalid:8080",
                request_ids=fixture_stripe_request_ids(),
            )
        # HAR #208 的关键取值
        self.assertEqual(captured["elements_session_client[locale]"], "hi-IN")
        self.assertEqual(captured["expected_amount"], "0")
        self.assertEqual(captured["expected_payment_method_type"], "upi")
        self.assertEqual(captured["init_checksum"], "init_checksum_fixture")
        self.assertEqual(captured["payment_method_data[type]"], "upi")
        self.assertEqual(captured["payment_method_data[client_attribution_metadata][payment_intent_creation_flow]"], "deferred")
        self.assertEqual(captured["client_attribution_metadata[payment_intent_creation_flow]"], "deferred")
        self.assertEqual(captured["client_attribution_metadata[checkout_config_id]"], "cfg_state")
        self.assertEqual(captured["_stripe_version"], upi.STRIPE_CUSTOM_VERSION)
        self.assertEqual(captured["passive_captcha_token"], "P1_fixture")
        # 账单里有 line2 → 带上；HAR 的 confirm 没有"空 line2"这种字段
        self.assertEqual(captured["payment_method_data[billing_details][address][line2]"], "Grant Road West")
        self.assertIn("return_url", captured)

        # 空 line2 不应出现在表单里（与 HAR #208 一致）
        captured.clear()
        billing = fixture_billing()
        billing["line2"] = ""
        with patch.object(
            upi,
            "_upi_passive_captcha_fields",
            lambda *args, **kwargs: {"passive_captcha_token": "P1_fixture", "passive_captcha_ekey": ""},
        ):
            upi._stripe_custom_checkout_confirm(
                ConfirmStub(),
                cs_id="cs_live_fixture",
                processor_entity="openai_llc",
                stripe_pk="pk_live_fixture",
                stripe_js_id="stripe-js-fixture",
                elements={
                    "session_id": "elements_fixture",
                    "config_id": "cfg_elements",
                    "passive_captcha": {"site_key": "fixture-key"},
                },
                init_data={"config_id": "cfg_init", "init_checksum": "init_checksum_fixture"},
                billing=billing,
                amount=0,
                checkout_client_secret="cs_live_fixture_secret",
                checkout_config_id="cfg_state",
                proxy="http://proxy.invalid:8080",
                request_ids=fixture_stripe_request_ids(),
            )
        self.assertNotIn("payment_method_data[billing_details][address][line2]", captured)

    def test_init_and_elements_use_session_locale(self):
        client = RecordingClient()
        captured: dict = {}

        def request(method, url, **kwargs):
            if kwargs.get("form"):
                captured.update(kwargs["form"])
            return 200, '{"id":"ppage_fixture"}', {}

        client.request = request
        upi._stripe_custom_checkout_init(
            client,
            cs_id="cs_live_fixture",
            stripe_pk="pk_live_fixture",
            stripe_js_id="stripe-js-fixture",
            proxy="http://proxy.invalid:8080",
        )
        self.assertEqual(captured["browser_locale"], "hi-IN")
        self.assertEqual(captured["browser_timezone"], "Asia/Calcutta")
        self.assertEqual(captured["elements_session_client[locale]"], "hi-IN")

    # ── 观察证明按阶段轮换 ────────────────────────────────────────────
    def test_observation_staleness_gate(self):
        client = RecordingClient()
        client.client_observation = "v1.r.p.fixture"
        client.client_observation_at = 0.0
        self.assertTrue(upi._upi_observation_stale(client))
        client.client_observation_at = time.monotonic()
        self.assertFalse(upi._upi_observation_stale(client))
        client.client_observation = "true"
        self.assertTrue(upi._upi_observation_stale(client))
        client.client_observation = ""
        self.assertTrue(upi._upi_observation_stale(client))

    def test_env_observation_injection_is_always_ignored(self):
        with patch.dict(os.environ, {"UPI_CLIENT_OBSERVATION": "v1.r.p.AbCdEfGh12345678"}):
            self.assertEqual(upi._upi_env_client_observation(), "")
            client = upi.UpiHttpClient("http://proxy.invalid:8080", region="IN")
            try:
                self.assertEqual(client.client_observation, "")
                self.assertEqual(client.client_observation_at, 0)
            finally:
                client.close()
        for rejected in ("true", "off", "1", "not-a-proof"):
            with patch.dict(os.environ, {"UPI_CLIENT_OBSERVATION": rejected}):
                self.assertEqual(upi._upi_env_client_observation(), "")

    def test_browser_risk_refreshes_by_default_only_when_stale(self):
        client = RecordingClient()
        client.client_observation = ""
        captured: list[str] = []

        def capture(active_client, **kwargs):
            captured.append("capture")
            return install_live_browser_risk(active_client)

        with patch.object(upi, "_capture_upi_browser_risk", capture):
            upi._upi_refresh_browser_risk(client, access_token="t", proxy="p", page_url="u")
            self.assertEqual(len(captured), 1)
            # 刚刷新过：不再重复捕获。
            upi._upi_refresh_browser_risk(client, access_token="t", proxy="p", page_url="u")
            self.assertEqual(len(captured), 1)
            client.client_observation_at = time.monotonic() - upi.UPI_OBSERVATION_MAX_AGE_S - 5
            upi._upi_refresh_browser_risk(client, access_token="t", proxy="p", page_url="u")
            self.assertEqual(len(captured), 2)

    # ── Stripe 运行时版本（confirm 的 version 字段）────────────────────
    def test_runtime_version_prefers_env_then_live_then_constant(self):
        client = RecordingClient()
        with patch.dict(os.environ, {"PAY153_UPI_STRIPE_RUNTIME_VERSION": "deadbeef01"}):
            self.assertEqual(
                upi._stripe_runtime_version(client, "http://proxy.invalid:8080", upi.STRIPE_RUNTIME_VERSION),
                "deadbeef01",
            )
        with patch.dict(os.environ, {"PAY153_UPI_STRIPE_RUNTIME_VERSION": ""}):
            with patch.object(upi, "_fetch_stripe_runtime_version", return_value="e27faf36d5"):
                self.assertEqual(
                    upi._stripe_runtime_version(client, "http://proxy.invalid:8080", upi.STRIPE_RUNTIME_VERSION),
                    "e27faf36d5",
                )
            with patch.object(upi, "_fetch_stripe_runtime_version", return_value=""):
                self.assertEqual(
                    upi._stripe_runtime_version(client, "http://proxy.invalid:8080", "6f8494a281"),
                    "6f8494a281",
                )

    def test_runtime_version_parses_build_salt(self):
        client = RecordingClient()

        def request(method, url, **kwargs):
            return 200, "var r=/*! STRIPE_JS_BUILD_SALT e27faf36d5*/\"e27faf36d5\",o=1;", {}

        client.request = request
        with patch.dict(os.environ, {"PAY153_UPI_STRIPE_RUNTIME_VERSION": ""}):
            upi._STRIPE_RUNTIME_VERSION_CACHE["value"] = ""
            upi._STRIPE_RUNTIME_VERSION_CACHE["at"] = 0.0
            self.assertEqual(upi._fetch_stripe_runtime_version(client, "http://proxy.invalid:8080"), "e27faf36d5")

    # ── 账单身份：全随机印度地址 ──────────────────────────────────────
    def test_billing_address_is_always_random_indian(self):
        rows = [upi._random_upi_billing_address() for _ in range(12)]
        for row in rows:
            address = row["address"]
            self.assertEqual(address["country"], "IN")
            self.assertTrue(address["line1"].strip())
            self.assertTrue(address["city"].strip())
            self.assertRegex(address["state"], r"^[A-Z]{2}$")
            self.assertRegex(address["postal_code"], r"^\d{6}$")
            self.assertIn(row["name"], upi.UPI_BILLING_NAMES)
        # 连续多次抽取必须出现不同地址（全随机，不按账号/设备固定）
        self.assertGreater(len({(r["address"]["line1"], r["address"]["postal_code"]) for r in rows}), 1)

    def test_billing_city_sync_is_opt_in(self):
        # 默认（未设 UPI_BILLING_SYNC_CITY）不限制城市 → 池内五地区随机
        with patch.dict(os.environ, {"UPI_BILLING_SYNC_CITY": ""}):
            self.assertFalse(upi._upi_billing_sync_city())
        # 显式要求同城时才收敛到该城市
        billing = upi._random_upi_billing_address(preferred_city="Kolkata")
        self.assertEqual(billing["address"]["city"], "Kolkata")
        self.assertEqual(billing["address"]["state"], "WB")
        self.assertTrue(billing["address"]["postal_code"].startswith("700"))

    def test_complete_indian_caller_address_is_kept(self):
        caller = fixture_billing()
        billing = upi.build_upi_billing("buyer@example.com", caller)
        self.assertEqual(billing["city"], caller["city"])
        self.assertEqual(billing["postal_code"], caller["postal_code"])
        self.assertEqual(billing["line1"], caller["line1"])
        self.assertEqual(billing["country"], "IN")

    def test_invalid_entered_address_is_rejected_instead_of_replaced(self):
        for caller in (
            {
                "name": "Alex Morgan",
                "address": {
                    "line1": "1 Market Street",
                    "city": "San Francisco",
                    "state": "CA",
                    "postal_code": "94105",
                    "country": "US",
                },
            },
            {"name": "Partial Address", "address": {"city": "Mumbai"}},
            {
                "address": {
                    "line1": "1 Somewhere",
                    "city": "Nowhere",
                    "state": "Maharashtra",
                    "postal_code": "400026",
                },
            },
        ):
            with self.assertRaisesRegex(ValueError, 'UPI 账单地址'):
                upi.build_upi_billing("buyer@example.com", caller)

    def test_every_extract_run_resolves_a_fresh_random_indian_billing(self):
        captured: list[dict] = []

        def run_attempt(client, token, step, steps, **kwargs):
            captured.append(kwargs.get("billing") or {})
            return {"ok": True}

        patches = (
            patch.object(upi, "_probe_upi_exit_city", return_value=""),
            patch.object(upi, "resolve_upi_access_token", return_value="header.payload.signature"),
            patch.object(upi, "_jwt_email", return_value="buyer@example.com"),
            patch.object(upi, "_fetch_promo_coupon_state", return_value="eligible"),
            patch.object(upi, "_human_pause", lambda *args, **kwargs: None),
            patch.object(upi, "_run_upi_attempt", run_attempt),
        )
        for _ in range(3):
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                result = upi.extract_upi_link(
                    "header.payload.signature",
                    "http://proxy.invalid:8080",
                    use_promo=True,
                )
            self.assertTrue(result.get("ok"))
        # 每次提链都会重新解析账单身份（重建 Checkout 会话时同理）
        self.assertEqual(len(captured), 3)
        for billing in captured:
            self.assertEqual(billing.get("country"), "IN")
            self.assertRegex(billing.get("postal_code", ""), r"^\d{6}$")
            self.assertIn(str(billing.get("city", "")).lower(), upi._known_indian_cities())
        # 全随机：多次提链不应始终落在同一套地址
        self.assertGreater(len({(b["line1"], b["postal_code"]) for b in captured}), 1)

    def test_billing_pool_is_limited_to_the_five_configured_states(self):
        rows = upi._upi_billing_address_rows()
        self.assertEqual({row[2] for row in rows}, set(upi.UPI_BILLING_STATES))
        self.assertEqual(len(rows), len(set(rows)))
        # 池外的邦/联邦属地不参与抽样（Kochi/KL 曾在池中）
        self.assertFalse({"kochi", "hyderabad", "ahmedabad", "jaipur"} & upi._known_indian_cities())


class UpiGit5ProtocolRailTests(unittest.TestCase):
    """源站 upi-git5：「纯协议 · 无浏览器内核」。

    这组用例的核心断言是"协议模式绝不启动浏览器"——用 patch 把浏览器运行时
    的入口换成会抛异常的哨兵，任何一次误调用都会立刻暴露，而不是被静默降级。
    """

    MACOS_PROFILE = {
        "kernel": "chrome",
        "impersonate": "chrome146-macos",
        "platform": "MacIntel",
        "platform_os": "macOS",
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="146", "Google Chrome";v="146", "Not.A/Brand";v="99"',
        "sec_ch_ua_platform": '"macOS"',
        "locale": "en-IN",
        "oai_language": "en-IN",
        "timezone": "Asia/Kolkata",
        "device_id": "git5-device",
        "oai_session_id": "git5-session",
    }

    def build_client(self, rail: str = upi.UPI_GIT5_RAIL, *, with_locale: bool = True):
        profile = dict(self.MACOS_PROFILE)
        if not with_locale:
            profile.pop("locale")
            profile.pop("oai_language")
        return upi.UpiHttpClient(
            "http://127.0.0.1:9",
            region="IN",
            fingerprint=profile,
            upi_rail=rail,
        )

    def test_rail_predicate_only_matches_git5(self):
        self.assertTrue(upi._upi_is_protocol_rail("upi-git5"))
        self.assertTrue(upi._upi_is_protocol_rail("UPI-GIT5"))
        for rail in ("upi", "upi-git", "upi-git2", "upi-git3", "upi-git4", "", None):
            self.assertFalse(upi._upi_is_protocol_rail(rail), rail)

    def test_git5_identity_is_macos_and_en_in(self):
        client = self.build_client()
        self.assertEqual(client.platform, "MacIntel")
        self.assertEqual(client.platform_label, "macOS")
        self.assertEqual(client.impersonate, "chrome146")
        self.assertEqual(client.locale, "en-IN")
        self.assertEqual(client.accept_language, upi.UPI_GIT5_ACCEPT_LANGUAGE)

    def test_browser_rail_keeps_its_default_locale(self):
        """git5 的 en-IN 覆盖不能泄漏到浏览器 rail。"""
        client = self.build_client(rail="upi", with_locale=False)
        self.assertEqual(client.locale, upi.IN_BROWSER_LOCALE)
        self.assertFalse(upi._upi_protocol_client(client))

    def test_protocol_mode_never_loads_the_browser_runtime(self):
        """把浏览器入口换成会抛异常的哨兵，协议模式必须完全绕开它。"""
        client = self.build_client()
        with patch.object(
            upi, "_load_upi_sentinel_runtime", side_effect=AssertionError("浏览器运行时被加载")
        ):
            with patch.object(upi, "_fetch_web_attestation", return_value=""):
                risk = upi._capture_upi_browser_risk(
                    client,
                    access_token="token",
                    proxy=client.base_proxy,
                    page_url="https://chatgpt.com/",
                    flow="",
                )
        self.assertTrue(risk["protocol"])
        self.assertTrue(risk["observation"])
        self.assertTrue(risk["stripe_ids"])

    def test_protocol_context_rotates_observation_but_keeps_stripe_ids(self):
        client = self.build_client()
        with patch.object(upi, "_fetch_web_attestation", return_value=""):
            upi._capture_upi_browser_risk(
                client, access_token="t", proxy=client.base_proxy,
                page_url="https://chatgpt.com/", flow="",
            )
            first_observation = client.client_observation
            first_ids = dict(client.stripe_request_ids)
            self.assertTrue(first_observation.startswith("v1.r.p."))

            upi._capture_upi_browser_risk(
                client, access_token="t", proxy=client.base_proxy,
                page_url="https://chatgpt.com/", flow="",
            )
        # HAR 里观察证明是阶段级值，每次捕获都换；Stripe 标识在一次尝试内稳定。
        self.assertNotEqual(client.client_observation, first_observation)
        self.assertEqual(client.stripe_request_ids, first_ids)

    def test_protocol_mode_allows_a_missing_deployment_attestation(self):
        """源站 macOS 上下文本来就没有 attestation 头，请求头组装会过滤空值。"""
        client = self.build_client()
        client.client_observation = "v1.r.p.fixture"
        self.assertTrue(
            upi._upi_risk_context_ok(client, {"attestation_source": "none", "browser_cookies": False})
        )

    def test_browser_rail_still_requires_a_deployment_attestation(self):
        """协议模式的放宽不能泄漏到浏览器 rail——那里必须 fail-closed。"""
        client = self.build_client(rail="upi")
        client.client_observation = "v1.r.p.fixture"
        client.attestation = ""
        self.assertFalse(
            upi._upi_risk_context_ok(client, {"attestation_source": "browser", "browser_cookies": True})
        )

    def test_protocol_mode_re_mints_sentinel_instead_of_using_the_cache(self):
        client = self.build_client()
        client.browser_sentinel_tokens["chatgpt_checkout"] = "cached-token"
        module = FakeSentinelModule()
        with patch.object(upi, "_load_upi_sentinel_module", return_value=module):
            upi._upi_sentinel_headers(
                client.base_proxy, client.device_id, "chatgpt_checkout", client, "https://chatgpt.com/"
            )
        # 协议模式既不消费也不清空浏览器缓存。
        self.assertEqual(client.browser_sentinel_tokens.get("chatgpt_checkout"), "cached-token")
        self.assertEqual(len(module.calls), 1)

    def test_pick_india_profile_accepts_all_four_macos_targets(self):
        for target in upi.UPI_GIT5_MACOS_PROFILES:
            kernel = "firefox" if target.startswith("firefox") else "chrome"
            with patch.object(upi, "_curl_impersonate_supported", return_value=True):
                profile = upi._pick_india_profile(kernel, target)
            self.assertEqual(profile["impersonate"], target[: -len(upi.UPI_GIT5_MACOS_SUFFIX)])

    def test_protocol_mode_skips_the_passive_captcha_solver(self):
        client = self.build_client()
        with patch.object(
            upi, "_load_upi_hcaptcha_module", side_effect=AssertionError("不应启动 Chrome 求解")
        ):
            fields = upi._upi_passive_captcha_fields(
                client.base_proxy, "sitekey-fixture", None, client=client
            )
        self.assertEqual(fields, {})

    def test_protocol_mode_runs_the_whole_attempt_without_a_browser(self):
        """走完整个建单阶段，浏览器入口一旦被调用就会抛异常。"""
        client = self.build_client()
        client.calls = []
        stages: list[tuple[str, str]] = []

        def request(method, url, **kwargs):
            client.calls.append({"method": method, "url": url, **kwargs})
            if url.endswith("/backend-api/payments/checkout"):
                return 200, json.dumps(
                    {
                        "checkout_session_id": "cs_live_fixture",
                        "processor_entity": "openai_llc",
                        "checkout_ui_mode": "custom",
                        "publishable_key": "pk_test_fixture",
                        "client_secret": "cs_live_fixture_secret_value",
                        "payment_method_types": ["upi"],
                        "promo_campaign": {"promo_campaign_id": "plus-1-month-free"},
                        "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
                    }
                ), {}
            return 200, "{}", {}

        client.request = request
        replacements = (
            ("_warmup_upi_session", lambda *args, **kwargs: (200, "oai-did=git5-device")),
            ("_fetch_web_attestation", lambda *args, **kwargs: ""),
            ("_upi_sentinel_headers", lambda *args, **kwargs: {"openai-sentinel-token": "fixture"}),
            ("_sentinel_ping", lambda *args, **kwargs: None),
            ("_warmup_india_client", lambda *args, **kwargs: None),
            ("_human_pause", lambda *args, **kwargs: None),
            ("_fetch_checkout_page_data", lambda *args, **kwargs: (200, "")),
            ("_run_upi_india_phase", lambda *args, **kwargs: {"ok": True}),
        )
        with ExitStack() as stack:
            for name, replacement in replacements:
                stack.enter_context(patch.object(upi, name, replacement))
            stack.enter_context(
                patch.object(upi, "_load_upi_sentinel_runtime", side_effect=AssertionError("浏览器运行时被加载"))
            )
            result = upi._run_upi_attempt(
                client,
                "header.payload.signature",
                lambda name, status, detail: stages.append((name, status)),
                [],
                promotion_proxy=client.base_proxy,
                billing=fixture_billing(),
                use_promo=True,
                upi_rail=upi.UPI_GIT5_RAIL,
            )
        self.assertTrue(result.get("ok"))
        self.assertIn(("风控上下文", "ok"), stages)


class UpiRiskBlockedClassificationTests(unittest.TestCase):
    """风控拦截的判定必须与源站一致（app.py:1213-1217）。

    回归背景：这里曾用一个递归 payload 扫描器，凡是嵌套对象里的
    status/result/state/code/reason 等于 blocked 就算命中。confirm/approve 的
    正常失败响应会嵌套 submission_attempt / payment_intent，于是
    generic_decline（支付方式设置失败）被误报成"上游返回 blocked"的终态风控拦截，
    任务直接终止。源站从不做这种匹配，最坏也只是"支付通道拒绝"。
    """

    def _classify(self, status: int, text: str) -> bool:
        """按真实调用路径判定：走 _raise_if_upi_risk_blocked，而不是直接问谓词。

        直接测谓词会漏掉"谓词没错、但调用点自己又加了一层递归匹配"这类回归，
        而线上真正决定任务生死的正是调用点。
        """
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = None
        try:
            upi._raise_if_upi_risk_blocked(status, text, 'test', payload)
            return False
        except RuntimeError as exc:
            if 'risk_blocked' in str(exc):
                return True
            raise

    def assert_blocked(self, status: int, text: str) -> None:
        self.assertTrue(self._classify(status, text), text[:120])

    def assert_not_blocked(self, status: int, text: str) -> None:
        self.assertFalse(self._classify(status, text), text[:120])

    def test_nested_blocked_marker_does_not_terminate_the_task(self):
        """回归的核心形状。

        旧的递归扫描器把嵌套对象里 status/result/state/code/reason 任意一个等于
        blocked 都算成风控拦截。源站只认正文里的 '"status":"blocked"' 字面量
        （app.py:1217），不递归、也不看 state/code/reason。

        这里的形状特意只包含源站**不会**命中的键：一旦有人再把判定放宽成递归
        扫描，这些用例就会变红。
        """
        for nested in (
            {"result": "exception", "submission_attempt": {"state": "blocked"}},
            {"result": "exception", "error": {"code": "blocked"}},
            {"submission_attempt": {"state": "failed"}, "retry": {"reason": "blocked"}},
        ):
            self.assert_not_blocked(200, json.dumps(nested))

    def test_generic_decline_is_not_a_risk_block(self):
        """实测触发过的形状：approve 返回 result=exception + generic_decline。"""
        payload = {
            "result": "exception",
            "submission_attempt": {
                "state": "failed",
                "failure_code": "generic_decline",
                "failure_reason": "The latest attempt to set up the payment method has failed.",
            },
        }
        self.assert_not_blocked(200, json.dumps(payload))

    def test_requires_payment_method_is_not_a_risk_block(self):
        payload = {
            "submission_attempt": {"state": "requires_payment_method", "failure_code": "generic_decline"},
            "setup_intent": {"status": "requires_payment_method"},
        }
        self.assert_not_blocked(200, json.dumps(payload))

    def test_expired_and_plain_rejections_are_not_risk_blocks(self):
        self.assert_not_blocked(200, json.dumps({"result": "expired"}))
        self.assert_not_blocked(409, json.dumps({"detail": "already exists"}))
        self.assert_not_blocked(500, "internal error")

    def test_reference_signals_still_classify_as_risk_blocks(self):
        self.assert_blocked(403, "{}")
        self.assert_blocked(200, json.dumps({"detail": "unusual_activity detected"}))
        self.assert_blocked(200, '{"detail":"unusual activity"}')
        self.assert_blocked(200, '{"status":"blocked"}')

    def test_generic_decline_survives_json_or_error(self):
        """走完整的 _json_or_error 路径也不能被误判成拦截。"""
        payload = json.dumps({
            "submission_attempt": {"state": "failed", "failure_code": "generic_decline"},
        })
        data = upi._json_or_error(200, payload, 'checkout confirm')
        self.assertEqual(data["submission_attempt"]["failure_code"], "generic_decline")

    def test_approve_generic_decline_is_not_reported_as_blocked(self):
        """approve 阶段的 generic_decline 必须走普通拒绝，而不是风控终态。"""
        client = RecordingClient()
        client.attestation = "fixture.attestation"
        client.attestation_source = "browser"
        client.client_observation = "v1.r.p.fixture"

        def request(method, url, **kwargs):
            return 200, json.dumps({
                "result": "exception",
                "submission_attempt": {"state": "failed", "failure_code": "generic_decline"},
            }), {}

        client.request = request
        with ExitStack() as stack:
            for name, replacement in (
                ("_upi_refresh_browser_risk", lambda *args, **kwargs: {}),
                ("_upi_sentinel_headers", lambda *args, **kwargs: {}),
                ("_chatgpt_headers", lambda *args, **kwargs: {}),
                ("_target_headers", lambda base, path, referer: {}),
            ):
                stack.enter_context(patch.object(upi, name, replacement))
            with self.assertRaises(RuntimeError) as ctx:
                upi._checkout_approve_custom(
                    client,
                    token="header.payload.signature",
                    cs_id="cs_live_fixture",
                    processor_entity="openai_llc",
                    proxy=client.base_proxy,
                    referer="https://chatgpt.com/checkout/openai_llc/cs_live_fixture",
                )
        message = str(ctx.exception)
        self.assertIn("approve rejected", message)
        self.assertNotIn("risk_blocked", message)
        self.assertNotIn("blocked", message)


class UpiGit5CheckoutPayloadTests(unittest.TestCase):
    """Auto checkout uses the native request captured at HAR entry 438."""

    def _create_body(self, rail: str, use_promo: bool = True, ui_mode: str = 'auto') -> dict:
        client = RecordingClient()
        captured: dict = {}

        def request(method, url, **kwargs):
            if url.endswith('/backend-api/payments/checkout'):
                captured.update(kwargs.get('json_body') or {})
                return 200, json.dumps({
                    'checkout_session_id': 'cs_live_fixture',
                    'processor_entity': 'openai_llc',
                    'checkout_ui_mode': 'custom',
                    'publishable_key': 'pk_test_fixture',
                    'client_secret': 'cs_live_fixture_secret',
                    'payment_method_types': ['upi'],
                }), {}
            return 200, '{}', {}

        client.request = request
        replacements = (
            ('_capture_upi_browser_risk', install_live_browser_risk),
            ('_warmup_upi_session', lambda *a, **k: (200, 'oai-did=x')),
            ('_fetch_web_attestation', lambda *a, **k: ''),
            ('_upi_sentinel_headers', lambda *a, **k: {'openai-sentinel-token': 't'}),
            ('_sentinel_ping', lambda *a, **k: None),
            ('_warmup_india_client', lambda *a, **k: None),
            ('_human_pause', lambda *a, **k: None),
            ('_fetch_checkout_page_data', lambda *a, **k: (200, '')),
            ('_run_upi_india_phase', lambda *a, **k: {'ok': True}),
            ('resolve_upi_access_token', lambda *a, **k: 'header.payload.signature'),
            ('_jwt_email', lambda *a, **k: 'buyer@example.com'),
            ('_fetch_promo_coupon_state', lambda *a, **k: 'unknown'),
            ('_probe_upi_exit_city', lambda *a, **k: ''),
        )
        with ExitStack() as stack:
            for name, replacement in replacements:
                stack.enter_context(patch.object(upi, name, replacement))
            upi._run_upi_attempt(
                client, 'header.payload.signature',
                lambda *a: None, [],
                promotion_proxy=client.base_proxy,
                billing=fixture_billing(),
                use_promo=use_promo,
                upi_rail=rail,
                checkout_ui_mode=ui_mode,
            )
        return captured

    def test_git5_create_omits_cancel_url_and_check_card_proxy(self):
        body = self._create_body(upi.UPI_GIT5_RAIL)
        self.assertNotIn('cancel_url', body)
        self.assertNotIn('check_card_proxy', body)
        self.assertNotIn('checkout_ui_mode', body)

    def test_browser_auto_create_matches_native_har_fields(self):
        body = self._create_body('upi')
        self.assertNotIn('cancel_url', body)
        self.assertNotIn('check_card_proxy', body)
        self.assertNotIn('checkout_ui_mode', body)

    def test_explicit_checkout_mode_is_preserved(self):
        for requested, sent in (('custom', 'custom'), ('hosted', 'custom'), ('deferred', 'deferred')):
            with self.subTest(requested=requested):
                self.assertEqual(self._create_body('upi', ui_mode=requested)['checkout_ui_mode'], sent)

    def test_create_still_carries_the_promo_campaign(self):
        body = self._create_body(upi.UPI_GIT5_RAIL)
        self.assertEqual(body.get('promo_campaign'), {
            'promo_campaign_id': upi.UPI_PROMO_COUPON,
            'is_coupon_from_query_param': False,
        })


if __name__ == "__main__":
    unittest.main()
