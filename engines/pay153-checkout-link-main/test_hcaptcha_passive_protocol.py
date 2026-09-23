from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "freepp-backend" / "core"))

import hcaptcha_passive_protocol as protocol
import upi_link_service as upi


def _jwt(payload: dict) -> str:
    def enc(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{enc({'alg': 'HS256', 'typ': 'JWT'})}.{enc(payload)}.sig"


class _Response:
    def __init__(self, status: int, *, body: dict | None = None, content: bytes = b"", content_type: str = ""):
        self.status_code = status
        self._body = body
        self.content = content
        self.text = json.dumps(body or {})
        self.headers = {"content-type": content_type}

    def json(self):
        return self._body


class _Session:
    def __init__(self, req: str):
        self.req = req
        self.calls: list[tuple[str, str]] = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        if "checksiteconfig" in url:
            return _Response(200, body={"c": {"type": "hsw", "req": self.req}})
        return _Response(200, content=b"encrypted-response", content_type="application/octet-stream")

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return _Response(200, content=b"function hsw() {}", content_type="text/javascript")


class HCaptchaPassiveProtocolTests(unittest.TestCase):
    def test_hsw_url_uses_current_jwt_asset_location(self):
        req = _jwt({"l": "/c/e426e928545a4a4671037b9367d8ae351ab7a6c0732137f9a97b00bce18205ce"})
        url, version = protocol._hsw_url(req)
        self.assertEqual(
            url,
            "https://newassets.hcaptcha.com/c/e426e928545a4a4671037b9367d8ae351ab7a6c0732137f9a97b00bce18205ce/hsw.js",
        )
        self.assertEqual(version, "e426e928545a4a4671037b9367d8ae351ab7a6c0732137f9a97b00bce18205ce")

    def test_protocol_chain_posts_ext18_payload_and_decrypts_token(self):
        req = _jwt({"l": "/c/e426e928545a4a4671037b9367d8ae351ab7a6c0732137f9a97b00bce18205ce"})
        session = _Session(req)
        invocations: list[str] = []
        stages: list[tuple[str, str]] = []

        def node_runner(helper, input_data, helper_dir, timeout):
            invocations.append(Path(helper).name)
            if Path(helper).name == "_jsdom_mint_unified.js":
                return {
                    "ok": True,
                    "n": "N" * 128,
                    "packed_b64": base64.b64encode(b"msgpack-ext18-fixture").decode(),
                    "ms": 17,
                }
            return {"ok": True, "token": "P1_" + "x" * 40}

        result = protocol.mint_hcaptcha_passive_protocol(
            site_key=protocol.SITEKEY,
            host="js.stripe.com",
            session=session,
            node_runner=node_runner,
            on_stage=lambda name, status, detail: stages.append((name, status)),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["token"].startswith("P1_"))
        self.assertEqual(result["n_len"], 128)
        self.assertEqual(result["packed_len"], len(b"msgpack-ext18-fixture"))
        self.assertEqual(invocations, ["_jsdom_mint_unified.js", "hsw_decrypt_resp_node.js"])
        self.assertEqual(
            stages,
            [("checksiteconfig", "ok"), ("hsw", "ok"), ("pow", "ok"), ("getcaptcha", "ok"), ("decrypt", "ok")],
        )
        self.assertTrue(any("checksiteconfig" in url for _, url in session.calls))
        self.assertTrue(any("getcaptcha/" in url for _, url in session.calls))

    def test_invalid_site_key_is_rejected_before_network(self):
        result = protocol.mint_hcaptcha_passive_protocol(site_key="not-a-site-key")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_site_key")

    def test_upi_confirm_prefers_protocol_token(self):
        fake = {"ok": True, "token": "P1_" + "x" * 40, "n_len": 128, "elapsed_ms": 12}
        with patch.object(upi, "_load_upi_hcaptcha_protocol_module") as loader, patch.dict(
            "os.environ",
            {
                "UPI_HCAPTCHA_PROTOCOL": "1",
                "UPI_HCAPTCHA_CHROME_FALLBACK": "0",
                "UPI_HCAPTCHA_HOST": "js.stripe.com",
            },
            clear=True,
        ):
            loader.return_value.mint_hcaptcha_passive_protocol.return_value = fake
            fields = upi._upi_passive_captcha_fields("http://proxy.invalid:8080", protocol.SITEKEY)
        self.assertEqual(fields["passive_captcha_token"], fake["token"])
        self.assertNotIn("passive_captcha_ekey", fields)
        loader.return_value.mint_hcaptcha_passive_protocol.assert_called_once()
        self.assertEqual(loader.return_value.mint_hcaptcha_passive_protocol.call_args.kwargs["host"], "js.stripe.com")


if __name__ == "__main__":
    unittest.main()
