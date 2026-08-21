import datetime as dt
import importlib.util
import pathlib
import struct
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "build_audio_calibration_efi.py"
SPEC = importlib.util.spec_from_file_location("build_audio_calibration_efi", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class AudioCalibrationEfiTests(unittest.TestCase):
    def make_blob(self):
        blob = bytearray(52)
        struct.pack_into("<III", blob, 0, 7, 48, 2)
        MODULE.CAL_RECORD.pack_into(blob, 12, 0x1111, 1, 20, 1, 10000)
        MODULE.CAL_RECORD.pack_into(blob, 32, 0x2222, 2, 21, 1, 11000)
        return bytes(blob)

    def test_candidate_preserves_header_and_target_uids(self):
        timestamp = dt.datetime(2026, 8, 21, 12, 36, 56, tzinfo=dt.timezone.utc)
        candidate = MODULE.build_candidate(self.make_blob(), (11956, 11477), 23, timestamp)
        size, count, records = MODULE.decode_payload(candidate)

        self.assertEqual((size, count), (48, 2))
        self.assertEqual([record[0] for record in records], [0x1111, 0x2222])
        self.assertEqual([record[2:] for record in records], [(23, 1, 11956), (23, 1, 11477)])
        self.assertEqual(records[0][1], records[1][1])

    def test_rejects_invalid_original(self):
        with self.assertRaisesRegex(ValueError, "expected 52 bytes"):
            MODULE.decode_payload(b"invalid")

    def test_rejects_empty_cal_r(self):
        timestamp = dt.datetime.now(dt.timezone.utc)
        with self.assertRaisesRegex(ValueError, "CAL_R values"):
            MODULE.build_candidate(self.make_blob(), (0, 11477), 23, timestamp)


if __name__ == "__main__":
    unittest.main()
