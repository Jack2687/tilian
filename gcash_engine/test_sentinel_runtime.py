import sys
import types
import unittest
from unittest import mock

from gcash_engine import sentinel_runtime


class _Request:
    def all_headers(self):
        return {
            "oai-web-deployment-attestation": "browser-attestation",
            "x-oai-is-client-observation": "v1.r.p.observation",
        }


class _Page:
    def __init__(self, context):
        self.context = context
        self.goto_timeout = None
        self.evaluations = []
        self.added_scripts = []
        self.raise_on_goto = False

    def route(self, _pattern, _handler):
        return None

    def goto(self, _url, *, wait_until, timeout):
        self.goto_timeout = timeout
        if self.raise_on_goto:
            raise RuntimeError("navigation failed")
        return None

    def wait_for_timeout(self, _timeout):
        return None

    def evaluate(self, script, *_args):
        self.evaluations.append(script)
        if "/backend-api/models" in script:
            self.context.request_handler(_Request())
        return ""

    def add_script_tag(self, *, url):
        self.added_scripts.append(url)


class _Context:
    def __init__(self):
        self.request_handler = None
        self.page = _Page(self)

    def add_init_script(self, _script):
        return None

    def add_cookies(self, _cookies):
        return None

    def new_page(self):
        return self.page

    def on(self, event, handler):
        if event == "request":
            self.request_handler = handler

    def cookies(self, _url):
        return []


class _Browser:
    def __init__(self):
        self.context = _Context()
        self.close_calls = 0

    def new_context(self, **_kwargs):
        return self.context

    def close(self):
        self.close_calls += 1
        return None


class _Chromium:
    def __init__(self):
        self.browser = _Browser()
        self.launch_options = None

    def launch(self, **kwargs):
        self.launch_options = kwargs
        return self.browser


class _PlaywrightManager:
    def __init__(self):
        self.chromium = _Chromium()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False


class SentinelRuntimeTests(unittest.TestCase):
    def test_empty_flow_uses_bounded_capture_without_loading_sdk(self):
        manager = _PlaywrightManager()
        playwright_package = types.ModuleType("playwright")
        playwright_api = types.ModuleType("playwright.sync_api")
        playwright_api.sync_playwright = lambda: manager
        playwright_package.sync_api = playwright_api

        with mock.patch.dict(
            sys.modules,
            {
                "playwright": playwright_package,
                "playwright.sync_api": playwright_api,
            },
        ):
            result = sentinel_runtime.capture_browser_bootstrap(
                access_token="access-token",
                session_token="session-token",
                proxy="",
                device_id="device-1",
                session_id="session-1",
                fingerprint={"ua": "Test UA"},
                executable_path="chromium",
                sentinel_flow="",
                timeout_s=25.0,
            )

        page = manager.chromium.browser.context.page
        self.assertGreater(manager.chromium.launch_options["timeout"], 0)
        self.assertLessEqual(manager.chromium.launch_options["timeout"], 10_000)
        self.assertGreater(page.goto_timeout, 0)
        self.assertLessEqual(page.goto_timeout, 10_000)
        models_script = next(script for script in page.evaluations if "/backend-api/models" in script)
        self.assertIn("AbortController", models_script)
        self.assertIn("controller.abort()", models_script)
        self.assertFalse(any("/backend-api/sentinel/sdk.js" in script for script in page.evaluations))
        self.assertEqual(page.added_scripts, [])
        self.assertEqual(result.observation, "v1.r.p.observation")
        self.assertEqual(result.sentinel_token, "")

    def test_navigation_error_still_closes_browser(self):
        manager = _PlaywrightManager()
        manager.chromium.browser.context.page.raise_on_goto = True
        playwright_package = types.ModuleType("playwright")
        playwright_api = types.ModuleType("playwright.sync_api")
        playwright_api.sync_playwright = lambda: manager
        playwright_package.sync_api = playwright_api

        with mock.patch.dict(
            sys.modules,
            {
                "playwright": playwright_package,
                "playwright.sync_api": playwright_api,
            },
        ):
            result = sentinel_runtime.capture_browser_bootstrap(
                access_token="access-token",
                session_token="session-token",
                proxy="",
                device_id="device-1",
                session_id="session-1",
                fingerprint={"ua": "Test UA"},
                executable_path="chromium",
                sentinel_flow="",
                timeout_s=25.0,
            )

        self.assertIn("navigation failed", result.error)
        self.assertEqual(manager.chromium.browser.close_calls, 1)

    def test_mint_sentinel_rejects_non_object_bridge_json(self):
        process = types.SimpleNamespace(stdout=b"[]", stderr=b"", returncode=0)
        with mock.patch.object(sentinel_runtime.subprocess, "run", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "JSON 对象"):
                sentinel_runtime.mint_sentinel(
                    flow="checkout_session_approval",
                    device_id="device-1",
                    user_agent="Test UA",
                    proxy="",
                    page_url="https://chatgpt.com/checkout",
                    language="en-US",
                    timezone="UTC",
                    cookies={},
                )


if __name__ == "__main__":
    unittest.main()
