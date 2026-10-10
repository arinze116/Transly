"""Regression tests for URL validation before remote media downloads."""

import asyncio
import socket
import unittest
from unittest.mock import patch

from security import UnsafeURL, validate_resolved_host, validate_url


def dns_result(address):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


class URLValidationTests(unittest.TestCase):
    def test_only_http_and_https_are_allowed(self):
        for value in ("file:///etc/passwd", "ftp://example.com/a.mp4", "javascript:alert(1)"):
            with self.subTest(value=value), self.assertRaises(UnsafeURL):
                validate_url(value)

    def test_rejects_local_and_non_global_ip_literals(self):
        for value in (
            "http://localhost/file.mp4",
            "http://127.0.0.1/file.mp4",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/file.mp4",
        ):
            with self.subTest(value=value), self.assertRaises(UnsafeURL):
                validate_url(value)

    def test_accepts_public_https_hostname_syntax(self):
        self.assertEqual(
            validate_url("https://media.example.com/video.mp4"),
            "https://media.example.com/video.mp4",
        )

    def test_rejects_hostname_resolving_to_private_ip(self):
        with patch("security.socket.getaddrinfo", return_value=dns_result("10.0.0.8")):
            with self.assertRaisesRegex(UnsafeURL, "resolves to a private address"):
                asyncio.run(validate_resolved_host("https://media.example.com/video.mp4"))

    def test_accepts_hostname_resolving_only_to_global_ip(self):
        with patch("security.socket.getaddrinfo", return_value=dns_result("93.184.216.34")):
            self.assertEqual(
                asyncio.run(validate_resolved_host("https://media.example.com/video.mp4")),
                "https://media.example.com/video.mp4",
            )

    def test_rejects_unresolvable_hostname(self):
        with patch("security.socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
            with self.assertRaisesRegex(UnsafeURL, "Host could not be resolved"):
                asyncio.run(validate_resolved_host("https://media.example.invalid/video.mp4"))


if __name__ == "__main__":
    unittest.main()
