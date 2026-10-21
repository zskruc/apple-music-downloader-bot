from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("sabr_installer", Path(__file__).with_name("install_sabr_tool.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class FixtureResponse(io.BytesIO):
    def geturl(self):
        return "https://release-fixture.invalid/yt-dlp"


class SabrInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.destination = self.directory / "vendor/yt-dlp"
        self.payload = b"synthetic installer fixture; never executed"

    def fixture(self, payload=None):
        return mock.Mock(return_value=FixtureResponse(self.payload if payload is None else payload))

    def pins(self):
        return mock.patch.multiple(installer, EXPECTED_BYTES=len(self.payload),
                                   EXPECTED_SHA256=hashlib.sha256(self.payload).hexdigest())

    def test_matching_fixture_is_atomically_installed_with_executable_mode(self):
        opener = self.fixture()
        with self.pins():
            installer.install_tool(self.destination, opener=opener)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])
        self.assertEqual(opener.call_args.kwargs, {"timeout": 60})
        self.assertEqual(opener.call_args.args[0].full_url, installer.ASSET_URL)

    def test_bad_hash_does_not_replace_existing_tool(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"previous reviewed fixture")
        bad_payload = b"x" * len(self.payload)
        with self.pins(), self.assertRaises(installer.InstallError):
            installer.install_tool(self.destination, opener=self.fixture(bad_payload))
        self.assertEqual(self.destination.read_bytes(), b"previous reviewed fixture")
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_bad_hash_leaves_no_candidate_or_tool(self):
        with mock.patch.object(installer, "EXPECTED_BYTES", len(self.payload)), \
                self.assertRaises(installer.InstallError):
            installer.install_tool(self.destination, opener=self.fixture())
        self.assertEqual(list(self.destination.parent.iterdir()), [])

    def test_truncated_fixture_is_rejected_and_cleaned(self):
        with self.pins(), self.assertRaises(installer.InstallError):
            installer.install_tool(self.destination, opener=self.fixture(self.payload[:-1]))
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [])

    def test_oversized_fixture_is_rejected_and_cleaned(self):
        with self.pins(), self.assertRaises(installer.InstallError):
            installer.install_tool(self.destination, opener=self.fixture(self.payload + b"extra"))
        self.assertEqual(list(self.destination.parent.iterdir()), [])

    def test_network_failure_preserves_existing_tool(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"previous reviewed fixture")
        opener = mock.Mock(side_effect=OSError("synthetic network failure"))
        with self.assertRaises(OSError):
            installer.install_tool(self.destination, opener=opener)
        self.assertEqual(self.destination.read_bytes(), b"previous reviewed fixture")
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_candidate_is_not_executable_during_transfer(self):
        observed = []
        def opener(request, *, timeout):
            candidates = list(self.destination.parent.glob(".yt-dlp-download-*"))
            observed.extend(path.stat().st_mode & 0o777 for path in candidates)
            return FixtureResponse(self.payload)
        with self.pins():
            installer.install_tool(self.destination, opener=opener)
        self.assertEqual(observed, [0o600])

    def test_non_https_redirect_is_rejected(self):
        response = FixtureResponse(self.payload)
        response.geturl = lambda: "http://release-fixture.invalid/yt-dlp"
        with self.pins(), self.assertRaises(installer.InstallError):
            installer.install_tool(self.destination, opener=mock.Mock(return_value=response))
        self.assertEqual(list(self.destination.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
