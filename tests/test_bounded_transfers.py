# SPDX-License-Identifier: BSD-3-Clause
"""Downloads and audio transfers keep an overall deadline and size limits."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import support  # noqa: F401  (the Decky harness before any backend import)
import main
import audio_backend
import tdp_backend
import _harness
import companion_common


class SlowResponse:
    def __init__(self, clock, chunks, seconds_per_chunk=0, length=None):
        self.clock, self.chunks, self.seconds = clock, list(chunks), seconds_per_chunk
        self.headers = {"Content-Length": str(length)} if length is not None else {}
        self.closed = False
        self.bulk_reads = 0
        self.incremental_reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def geturl(self):
        return tdp_backend.RYZENADJ_URL

    def next(self):
        self.clock[0] += self.seconds
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, _size):
        self.bulk_reads += 1
        return self.next()

    def read1(self, _size):
        self.incremental_reads += 1
        return self.next()

class HardwareTransferTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.clock = [0.0]
        self.stack.enter_context(patch.object(main.time, "monotonic", side_effect=lambda: self.clock[0]))
        self.stack.enter_context(patch.object(tdp_backend, "_download_archive", _harness.REAL_DOWNLOAD_ARCHIVE))

    def download(self):
        target = self.root / "download.tar.gz"
        with target.open("wb") as out:
            tdp_backend._download_archive(out)
        return target

    def response(self, **kwargs):
        response = SlowResponse(self.clock, **kwargs)
        self.stack.enter_context(patch.object(companion_common, "open_hardware_download", return_value=response))
        return response

    def test_archive_transfer_has_an_overall_deadline_and_closes_stream(self):
        response = self.response(chunks=[b"archive"], seconds_per_chunk=120)
        with self.assertRaises(TimeoutError):
            self.download()
        self.assertTrue(response.closed)

    def test_audio_transfer_has_an_overall_deadline_even_with_matching_digest(self):
        data = b"calibration"
        response = self.response(chunks=[data], seconds_per_chunk=120)
        with patch.object(audio_backend, "AUDIO_CALIBRATION_SHA256", hashlib.sha256(data).hexdigest()):
            with self.assertRaises(TimeoutError):
                audio_backend._download_audio_calibration()
        self.assertTrue(response.closed)

    def test_archive_uses_incremental_reads_and_preserves_complete_payload(self):
        response = self.response(chunks=[b"first", b"second"], seconds_per_chunk=0.01)
        target = self.download()
        self.assertEqual(target.read_bytes(), b"firstsecond")
        self.assertEqual(response.bulk_reads, 0)
        self.assertGreater(response.incremental_reads, 0)
        self.assertTrue(response.closed)

    def test_download_size_limits_still_apply_without_content_length(self):
        self.response(chunks=[b"too-large"])
        with patch.object(tdp_backend, "MAX_DOWNLOAD_BYTES", 4), self.assertRaises(RuntimeError):
            self.download()


if __name__ == "__main__":
    unittest.main()
