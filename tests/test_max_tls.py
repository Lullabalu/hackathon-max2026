import hashlib
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from ip_bot.max_api import APIError, DEFAULT_CA_CERT_FILE, MaxAPI, max_ssl_context


class MaxTLSTests(unittest.TestCase):
    def test_bundled_ca_preserves_default_roots_and_tls_verification(self):
        with patch.dict("os.environ", {}, clear=True):
            original = ssl.create_default_context()
            context = max_ssl_context()
        original_roots = set(original.get_ca_certs(binary_form=True))
        actual_roots = set(context.get_ca_certs(binary_form=True))
        self.assertTrue(original_roots)
        self.assertTrue(original_roots <= actual_roots)
        bundled_der = ssl.PEM_cert_to_DER_cert(DEFAULT_CA_CERT_FILE.read_text())
        self.assertIn(bundled_der, actual_roots)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        # Создание MAX-контекста не меняет доверие в других HTTP-клиентах.
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(original_roots, set(ssl.create_default_context().get_ca_certs(binary_form=True)))

    def test_bundled_ca_matches_verified_official_download(self):
        digest = hashlib.sha256(DEFAULT_CA_CERT_FILE.read_bytes()).hexdigest()
        self.assertEqual(digest, "936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504")

    def test_explicit_ca_override_works_without_bundled_path(self):
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "custom-root.crt"
            ca_file.write_bytes(DEFAULT_CA_CERT_FILE.read_bytes())
            with patch.dict("os.environ", {"MAX_CA_CERT_FILE": str(ca_file)}), patch("ip_bot.max_api.DEFAULT_CA_CERT_FILE", Path(directory) / "absent.crt"):
                context = max_ssl_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_invalid_configured_ca_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "invalid.crt"
            ca_file.write_text("not a certificate")
            for path in (ca_file, Path(directory) / "absent.crt"):
                with self.subTest(path=path.name), patch.dict("os.environ", {"MAX_CA_CERT_FILE": str(path)}):
                    with self.assertRaisesRegex(APIError, "MAX_CA_CERT_FILE"):
                        MaxAPI("example-not-a-real-token")

    def test_tls_verification_failure_has_actionable_error_without_token(self):
        api = MaxAPI("example-not-a-real-token")
        failure = ssl.SSLCertVerificationError("certificate verify failed")
        for error in (failure, URLError(failure)):
            with self.subTest(error=type(error).__name__), patch.object(api.opener, "open", side_effect=error):
                with self.assertRaisesRegex(APIError, "MAX_CA_CERT_FILE") as caught:
                    api.request("GET", "/me")
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn(api.token, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
