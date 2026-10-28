#!/usr/bin/env python3
"""Regression tests for fail-closed file hashing and the single authoritative Kalshi pin.

No network, no broker, no Kalshi server invocation. All fixtures are temporary files.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import secure_hash  # noqa: E402
import kalshi_source_pin as pin  # noqa: E402
from public_snapshot import requires_files  # noqa: E402

MAX = 1_000_000


class TestSecureHash(unittest.TestCase):
    def test_regular_file_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f.txt"
            path.write_bytes(b"hello world\n")
            self.assertEqual(secure_hash.sha256_file_strict(path, MAX),
                             hashlib.sha256(b"hello world\n").hexdigest())

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real.txt"
            real.write_bytes(b"payload")
            link = Path(td) / "link.txt"
            link.symlink_to(real)
            self.assertIsNone(secure_hash.sha256_file_strict(link, MAX))
            # the real path still hashes, proving only the symlink was refused
            self.assertIsNotNone(secure_hash.sha256_file_strict(real, MAX))

    def test_replaced_file_changes_the_digest(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f.txt"
            path.write_bytes(b"before")
            first = secure_hash.sha256_file_strict(path, MAX)
            path.write_bytes(b"after!")
            self.assertNotEqual(first, secure_hash.sha256_file_strict(path, MAX))

    def test_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(secure_hash.sha256_file_strict(Path(td), MAX))

    def test_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            fifo = Path(td) / "pipe"
            try:
                os.mkfifo(fifo)
            except (AttributeError, OSError):
                self.skipTest("mkfifo unavailable")
            self.assertIsNone(secure_hash.sha256_file_strict(fifo, MAX))

    def test_oversized_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "big.bin"
            path.write_bytes(b"x" * 2048)
            self.assertIsNone(secure_hash.sha256_file_strict(path, 1024))
            self.assertIsNotNone(secure_hash.sha256_file_strict(path, 4096))

    def test_empty_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "empty"
            path.write_bytes(b"")
            self.assertIsNone(secure_hash.sha256_file_strict(path, MAX))

    def test_missing_file_is_rejected(self):
        self.assertIsNone(secure_hash.sha256_file_strict(Path("/nonexistent/xyz"), MAX))

    def test_unreadable_file_is_rejected(self):
        if os.geteuid() == 0:
            self.skipTest("root bypasses permission bits")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "locked"
            path.write_bytes(b"secret")
            os.chmod(path, 0o000)
            try:
                self.assertIsNone(secure_hash.sha256_file_strict(path, MAX))
            finally:
                os.chmod(path, 0o600)

    def test_invalid_max_bytes_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f"
            path.write_bytes(b"data")
            for bad in (0, -1, None, "100", True):
                self.assertIsNone(secure_hash.sha256_file_strict(path, bad), repr(bad))

    def test_platform_fallback_path_still_rejects_symlinks(self):
        """Simulate a platform without O_NOFOLLOW."""
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real"
            real.write_bytes(b"payload")
            link = Path(td) / "link"
            link.symlink_to(real)
            with mock.patch.object(secure_hash, "HAS_NOFOLLOW", False):
                self.assertIsNone(secure_hash.sha256_file_strict(link, MAX))
                self.assertIsNotNone(secure_hash.sha256_file_strict(real, MAX))

    def test_no_exception_escapes(self):
        for value in (None, 12, object(), b"bytes"):
            try:
                self.assertIsNone(secure_hash.sha256_file_strict(value, MAX))
            except Exception as exc:  # noqa: BLE001
                self.fail(f"raised for {value!r}: {exc}")


class TestKalshiSourcePin(unittest.TestCase):
    @requires_files("scripts/kalshi/config/connector_policy.json")
    def test_authoritative_digest_comes_from_the_policy_json(self):
        expected = pin.expected_sha256()
        self.assertIsNotNone(expected)
        document = json.loads(pin.POLICY_PATH.read_text())
        self.assertEqual(expected, document["source_integrity"]["sha256"])

    @requires_files("scripts/kalshi/config/connector_policy.json",
                    "scripts/validate_broker_permissions.py")
    def test_all_consumers_agree_with_the_authority(self):
        """A stale duplicate literal must not be able to disagree silently."""
        import broker_runtime_audit as bra
        self.assertEqual(bra.KALSHI_EXPECTED_SOURCE_SHA256, pin.expected_sha256())
        self.assertEqual(Path(bra.KALSHI_SERVER_SOURCE_PATH), pin.source_path())

    def test_broker_runtime_audit_holds_no_independent_sha_literal(self):
        source = (ROOT / "scripts" / "broker_runtime_audit.py").read_text()
        self.assertNotIn("a03b249fb7ff5d1935d64c35e5ed651328933ac3e59b4980bd07fe03b1fa3a71",
                         source, "duplicate SHA literal reintroduced")

    def test_missing_policy_fails_closed(self):
        with mock.patch.object(pin, "POLICY_PATH", Path("/nonexistent/policy.json")):
            self.assertIsNone(pin.expected_sha256())
            self.assertFalse(pin.source_current())

    def test_malformed_sha_fails_closed(self):
        for bad in ("nothex", "A03B249F" * 8, "abc", 123, None, "a" * 63):
            document = {"source_integrity": {"enforce_sha256": True, "sha256": bad}}
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "p.json"
                path.write_text(json.dumps(document))
                with mock.patch.object(pin, "POLICY_PATH", path):
                    self.assertIsNone(pin.expected_sha256(), repr(bad))
                    self.assertFalse(pin.source_current())

    def test_enforcement_disabled_is_treated_as_no_pin(self):
        document = {"source_integrity": {"enforce_sha256": False, "sha256": "a" * 64}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.json"
            path.write_text(json.dumps(document))
            with mock.patch.object(pin, "POLICY_PATH", path):
                self.assertIsNone(pin.expected_sha256())
                self.assertFalse(pin.source_current())

    def test_wrong_source_path_fails_closed(self):
        with mock.patch.object(pin, "SOURCE_PATH", Path("/nonexistent/server.py")):
            self.assertIsNone(pin.current_sha256())
            self.assertFalse(pin.source_current())

    def test_source_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            impostor = Path(td) / "server.py"
            impostor.write_bytes(b"print('not the pinned server')\n")
            with mock.patch.object(pin, "SOURCE_PATH", impostor):
                self.assertIsNotNone(pin.current_sha256())
                self.assertFalse(pin.source_current())

    def test_relative_source_path_fails_closed(self):
        with mock.patch.object(pin, "SOURCE_PATH", Path("relative/server.py")):
            self.assertIsNone(pin.source_path())
            self.assertFalse(pin.source_current())


if __name__ == "__main__":
    unittest.main()
