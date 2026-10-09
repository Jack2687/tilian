from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from curl_cffi import CurlOpt
from curl_cffi.requests.exceptions import RequestException

import upi_link_service as upi


def make_ca(name: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


class LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"local TLS verified"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(os.name == "nt", "Windows libcurl CA path compatibility")
class UpiTlsTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="upi-tls-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.source_directory = Path(cls.temporary.name) / "中文证书目录"
        cls.source_directory.mkdir()
        ca_key, ca = make_ca("UPI loopback test CA")
        _, unrelated_ca = make_ca("Unrelated loopback CA")
        cls.ca_path = cls.source_directory / "ca.pem"
        cls.ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cls.unrelated_ca_path = cls.source_directory / "unrelated-ca.pem"
        cls.unrelated_ca_path.write_bytes(unrelated_ca.public_bytes(serialization.Encoding.PEM))

        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        server_certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca.subject)
            .public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        cert_path = cls.source_directory / "server.pem"
        key_path = cls.source_directory / "server.key"
        cert_path.write_bytes(server_certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackHandler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.stop_server)
        cls.port = cls.server.server_port

    @classmethod
    def stop_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # Never use a user's proxy or trust configuration in these local tests.
        environment = {
            "REQUESTS_CA_BUNDLE": "", "CURL_CA_BUNDLE": "",
            "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "*",
            "http_proxy": "", "https_proxy": "", "all_proxy": "", "no_proxy": "*",
        }
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session_for_ca(self, source):
        with patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": str(source)}):
            session = upi._new_upi_http_session("chrome131")
        self.addCleanup(session.close)
        # An explicit empty libcurl proxy prevents platform/env proxy discovery.
        session.curl_options[CurlOpt.PROXY] = ""
        self.assertIsNot(session.verify, False)
        return session

    def test_unicode_bundle_copy_preserves_bytes_and_is_reused(self):
        first = upi._upi_ascii_ca_path(str(self.ca_path))
        second = upi._upi_ascii_ca_path(str(self.ca_path))
        self.assertTrue(first.isascii())
        self.assertEqual(first, second)
        self.assertEqual(Path(first).read_bytes(), self.ca_path.read_bytes())

    def test_missing_and_empty_ca_bundle_report_local_configuration_error(self):
        empty = self.source_directory / "empty.pem"
        empty.write_bytes(b"")
        for source in (self.source_directory / "missing.pem", empty):
            with self.subTest(source=source), self.assertRaisesRegex(
                RuntimeError, "UPI 本地证书配置错误",
            ):
                upi._upi_ascii_ca_path(str(source))

    def test_requests_ca_bundle_keeps_priority_over_curl_ca_bundle(self):
        with patch.dict(os.environ, {
            "REQUESTS_CA_BUNDLE": str(self.ca_path),
            "CURL_CA_BUNDLE": str(self.unrelated_ca_path),
        }):
            session = upi._new_upi_http_session("chrome131")
        self.addCleanup(session.close)
        self.assertEqual(Path(session.verify).read_bytes(), self.ca_path.read_bytes())
        if os.name == "nt":
            self.assertTrue(session.verify.isascii())

    def test_curl_ca_bundle_is_used_when_requests_bundle_is_unset(self):
        with patch.dict(os.environ, {"CURL_CA_BUNDLE": str(self.ca_path)}):
            session = upi._new_upi_http_session("chrome131")
        self.addCleanup(session.close)
        self.assertEqual(Path(session.verify).read_bytes(), self.ca_path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows CA filename compatibility")
    def test_non_ascii_default_ca_bundle_is_used_without_disabling_verification(self):
        with patch("curl_cffi.curl.DEFAULT_CACERT", str(self.ca_path)):
            session = upi._new_upi_http_session("chrome131")
        self.addCleanup(session.close)
        self.assertTrue(session.verify.isascii())
        self.assertEqual(Path(session.verify).read_bytes(), self.ca_path.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows CA filename compatibility")
    def test_unreadable_environment_bundle_fails_during_session_setup(self):
        with patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": str(self.source_directory / "missing.pem")}):
            with self.assertRaisesRegex(RuntimeError, "UPI 本地证书配置错误"):
                upi._new_upi_http_session("chrome131")

    def test_real_tls_accepts_server_signed_by_unicode_path_ca(self):
        session = self.session_for_ca(self.ca_path)
        response = session.get(f"https://localhost:{self.port}/", timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "local TLS verified")

    def test_utf8_mode_subprocess_verifies_server_with_unicode_ca_path(self):
        # The locale encoding is fixed at interpreter startup. A child process
        # covers PYTHONUTF8=1 even when the test runner uses Windows ANSI mode.
        code = "\n".join((
            "import json, sys",
            "from curl_cffi import CurlOpt",
            "from upi_link_service import _new_upi_http_session",
            "with _new_upi_http_session('chrome131') as session:",
            "    session.curl_options[CurlOpt.PROXY] = ''",
            "    response = session.get(sys.argv[1], timeout=5)",
            "    print(json.dumps({'utf8_mode': sys.flags.utf8_mode, 'status': response.status_code, 'body': response.text}))",
        ))
        environment = dict(os.environ)
        environment.update({"PYTHONUTF8": "1", "REQUESTS_CA_BUNDLE": str(self.ca_path)})
        result = subprocess.run(
            [sys.executable, "-c", code, f"https://localhost:{self.port}/"],
            cwd=Path(__file__).resolve().parent,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["utf8_mode"], 1)
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["body"], "local TLS verified")

    def test_real_tls_rejects_untrusted_server_certificate(self):
        session = self.session_for_ca(self.unrelated_ca_path)
        with self.assertRaises(RequestException) as caught:
            session.get(f"https://localhost:{self.port}/", timeout=5)
        self.assertEqual(caught.exception.code, 60)

    def test_real_tls_rejects_hostname_mismatch(self):
        session = self.session_for_ca(self.ca_path)
        with self.assertRaises(RequestException) as caught:
            session.get(f"https://127.0.0.1:{self.port}/", timeout=5)
        self.assertEqual(caught.exception.code, 60)

    def test_http_client_uses_shared_session_factory(self):
        session = MagicMock()
        with patch.object(upi, "_new_upi_http_session", return_value=session) as factory:
            client = upi.UpiHttpClient("", fingerprint={"impersonate": "chrome131"})
        self.addCleanup(client.close)
        factory.assert_called_once_with("chrome131")
        self.assertIs(client.session, session)

    def test_session_token_refresh_uses_shared_factory(self):
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.text = '{"accessToken":"fixture-access-token"}'
        session.get.return_value.json.return_value = {"accessToken": "fixture-access-token"}
        with patch.object(upi, "_new_upi_http_session", return_value=session) as factory:
            token = upi._refresh_access_token_from_session(
                "fixture-session-token", "", "fixture-device", {"impersonate": "chrome131"},
            )
        self.assertEqual(token, "fixture-access-token")
        factory.assert_called_once_with("chrome131")
        session.close.assert_called_once()

    def test_session_token_refresh_preserves_local_ca_loading_failure(self):
        session = MagicMock()
        session.get.side_effect = RuntimeError("curl: (77) error adding trust anchors")
        with patch.object(upi, "_new_upi_http_session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "UPI 本地证书配置错误"):
                upi._refresh_access_token_from_session(
                    "fixture-session-token", "", "fixture-device", {"impersonate": "chrome131"},
                )
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
