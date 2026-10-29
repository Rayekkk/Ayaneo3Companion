# SPDX-License-Identifier: BSD-3-Clause
"""This plugin's side of its two downloads: the pinned URL, hosts, redirect hosts, SHA-256, size limit, deadline
and cancel reach dk_net, its errors are mapped, and only a verified RyzenAdj binary is installed.

The transport guarantees (overall deadline, size limit with or without Content-Length, redirects, TLS) are
dk_net's own, tested in test_dk_net.py.
"""
from contextlib import ExitStack
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import support  # noqa: F401  (the Decky harness before any backend import)
import audio_backend
import companion_common
import dk_net
import tdp_backend

HELPER = b"\x7fELF fake ryzenadj"


def archive(payload=HELPER):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as bundle:
        info = tarfile.TarInfo("ryzenadj-linux/ryzenadj")
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    return data.getvalue()


class RyzenAdjDownloadTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.bin_dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "helper"
        self.stack.enter_context(patch.object(tdp_backend, "BIN_DIR", str(self.bin_dir)))
        self.stack.enter_context(patch.object(tdp_backend, "BIN_PATH", str(self.bin_dir / "ryzenadj")))
        self.stack.enter_context(patch.object(tdp_backend, "_download_archive", support.REAL_DOWNLOAD_ARCHIVE))
        self.calls, self.bodies = [], []

    def spool(self, body=b"", error=None):
        def spool(*args, **kwargs):
            self.calls.append((args, kwargs))
            if error is not None:
                raise error
            self.bodies.append(io.BytesIO(body))
            return self.bodies[-1]
        self.stack.enter_context(patch.object(dk_net, "spool", side_effect=spool))

    def test_the_pinned_archive_is_requested_with_its_bounds_and_copied_whole(self):
        self.spool(b"firstsecond")
        cancel = lambda: False  # noqa: E731
        out = io.BytesIO()
        tdp_backend._download_archive(out, cancel)
        self.assertEqual(out.getvalue(), b"firstsecond")
        self.assertTrue(self.bodies[0].closed, "the spooled body is closed after the copy")
        (args, kwargs), = self.calls
        self.assertEqual(args, (tdp_backend.RYZENADJ_URL, tdp_backend.BIN_DIR, tdp_backend.MAX_DOWNLOAD_BYTES,
                                tdp_backend.RYZENADJ_SHA256))
        self.assertEqual(kwargs, {"hosts": companion_common.GITHUB_RELEASE_HOSTS,
                                  "redirects": companion_common.GITHUB_ASSET_HOSTS,
                                  "deadline": companion_common.HARDWARE_DOWNLOAD_SECONDS, "cancel": cancel})
        self.assertIn(urlsplit(tdp_backend.RYZENADJ_URL).hostname, companion_common.GITHUB_RELEASE_HOSTS)
        self.assertNotIn("github.com", companion_common.GITHUB_ASSET_HOSTS, "a redirect only leads to asset storage")
        self.assertRegex(tdp_backend.RYZENADJ_SHA256, "^[0-9a-f]{64}$")
        self.assertLessEqual(tdp_backend.MAX_DOWNLOAD_BYTES, dk_net.MAX_BODY)
        self.assertLessEqual(companion_common.HARDWARE_DOWNLOAD_SECONDS, dk_net.MAX_DEADLINE)

    def test_a_failed_transfer_is_a_runtime_error_and_a_cancelled_one_is_interrupted(self):
        for error in (dk_net.NetError("timed out"), dk_net.TooLarge("too large"), dk_net.Mismatch("other file")):
            for cancel in (None, lambda: False):
                with self.subTest(error=error, cancel=cancel), ExitStack() as case:
                    case.enter_context(patch.object(dk_net, "spool", side_effect=error))
                    with self.assertRaisesRegex(RuntimeError, f"RyzenAdj download failed: {error}"):
                        tdp_backend._download_archive(io.BytesIO(), cancel)
            with self.subTest(error=error, cancel="set"), patch.object(dk_net, "spool", side_effect=error):
                with self.assertRaisesRegex(InterruptedError, "cancelled"):
                    tdp_backend._download_archive(io.BytesIO(), lambda: True)

    def test_only_a_verified_binary_is_installed(self):
        good = archive()
        cases = (("archive", b"not the pinned archive", "archive checksum mismatch"),
                 ("binary", archive(b"\x7fELF something else"), "binary checksum mismatch"))
        for name, body, message in cases:
            with self.subTest(name), ExitStack() as case:
                case.enter_context(patch.object(tdp_backend, "RYZENADJ_SHA256", hashlib.sha256(
                    body if name == "binary" else good).hexdigest()))
                case.enter_context(patch.object(tdp_backend, "RYZENADJ_BINARY_SHA256",
                                                hashlib.sha256(HELPER).hexdigest()))
                case.enter_context(patch.object(dk_net, "spool", return_value=io.BytesIO(body)))
                with self.assertRaisesRegex(RuntimeError, message):
                    tdp_backend._download_ryzenadj()
                self.assertEqual(os.listdir(self.bin_dir), [], "nothing installed, no temporary left")
        with patch.object(tdp_backend, "RYZENADJ_SHA256", hashlib.sha256(good).hexdigest()), \
             patch.object(tdp_backend, "RYZENADJ_BINARY_SHA256", hashlib.sha256(HELPER).hexdigest()), \
             patch.object(dk_net, "spool", return_value=io.BytesIO(good)):
            tdp_backend._download_ryzenadj()
        self.assertEqual(os.listdir(self.bin_dir), ["ryzenadj"])
        self.assertEqual((self.bin_dir / "ryzenadj").read_bytes(), HELPER)


class AudioCalibrationDownloadTests(unittest.TestCase):
    def test_the_pinned_file_is_requested_with_its_bounds(self):
        with patch.object(dk_net, "get", return_value=b"calibration") as get:
            self.assertEqual(audio_backend._download_audio_calibration(), b"calibration")
        get.assert_called_once_with(audio_backend.AUDIO_CALIBRATION_URL, 64 * 1024,
                                    audio_backend.AUDIO_CALIBRATION_SHA256, hosts={"raw.githubusercontent.com"},
                                    deadline=companion_common.HARDWARE_DOWNLOAD_SECONDS)
        self.assertEqual(urlsplit(audio_backend.AUDIO_CALIBRATION_URL).hostname, "raw.githubusercontent.com")
        self.assertRegex(audio_backend.AUDIO_CALIBRATION_SHA256, "^[0-9a-f]{64}$")

    def test_transfer_errors_become_runtime_errors(self):
        for error, message in ((dk_net.TooLarge("too large"), "unexpectedly large"),
                               (dk_net.Mismatch("other file"), "checksum mismatch"),
                               (dk_net.NetError("timed out"), "audio calibration download failed: timed out")):
            with self.subTest(error=error), patch.object(dk_net, "get", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, message):
                    audio_backend._download_audio_calibration()


if __name__ == "__main__":
    unittest.main()
