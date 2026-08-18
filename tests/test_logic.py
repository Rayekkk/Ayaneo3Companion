import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from _harness import install
install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main

class LogicTests(unittest.TestCase):
    def test_charge_bypass_register_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "io"
            path.write_bytes(bytes(256))
            old_path = main._ec_io_path
            old_ensure = main.ensure_charge_control
            old_supported = main.supported_device
            try:
                main._ec_io_path = lambda: path
                main.ensure_charge_control = lambda: path
                main.supported_device = lambda: True
                main.write_charge_bypass(True)
                self.assertTrue(main.read_charge_bypass())
                self.assertEqual(path.read_bytes()[main.EC_CHARGE_REGISTER], main.EC_CHARGE_INHIBIT)
                main.write_charge_bypass(False)
                self.assertFalse(main.read_charge_bypass())
                self.assertEqual(path.read_bytes()[main.EC_CHARGE_REGISTER], main.EC_CHARGE_AUTO)
            finally:
                main._ec_io_path = old_path
                main.ensure_charge_control = old_ensure
                main.supported_device = old_supported

    def test_tdp_clamps_and_orders(self):
        self.assertEqual(main.normalize_tdp({"spl": 2, "sppt": 1, "fppt": 99}), {"spl": 5, "sppt": 5, "fppt": 45})
        self.assertEqual(main.normalize_tdp({"spl": 35, "sppt": 99, "fppt": 99}), {"spl": 35, "sppt": 40, "fppt": 45})
        self.assertEqual(main.PRESETS["Max"], {"spl": 35, "sppt": 38, "fppt": 40})

    def test_controller_normalization(self):
        value = main.normalize_controller({"vibration": "wat", "rgb_mode": "wat", "color": "oops", "brightness": 999})
        self.assertEqual(value, {"vibration": "high", "rgb_mode": "solid", "color": "6600ff", "brightness": 100})

    def test_controller_packet_shape_and_checksum(self):
        packet = main.controller_command({"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50})
        self.assertEqual(len(packet), 65)
        self.assertEqual(packet[3:5], bytes([0x21, 0x09]))
        self.assertEqual(packet[24], 0x10)
        self.assertEqual(int.from_bytes(packet[1:3], "little"), sum(packet[7:]))

    def test_off_packet(self):
        packet = main.controller_command({"vibration": "off", "rgb_mode": "off", "color": "ffffff", "brightness": 100})
        self.assertEqual(packet[8], 0xff)
        self.assertEqual(packet[12], 0xff)
        self.assertEqual(packet[24], 0x40)

    def test_download_url_allowlist(self):
        self.assertEqual(main._checked_download_url(main.RYZENADJ_URL), main.RYZENADJ_URL)
        with self.assertRaises(RuntimeError):
            main._checked_download_url("https://example.com/ryzenadj")

    def test_display_script_is_gamma22(self):
        self.assertTrue(main._is_our_display_script(main.LUA_SOURCE))
        self.assertIn("gamescope.eotf.gamma22", main.LUA_SOURCE.read_text())
        self.assertNotIn("gamescope.eotf.pq", main.LUA_SOURCE.read_text())

    def test_ayaneo_edid_maxcll_patch(self):
        base = bytearray(128)
        base[:12] = b"\x00\xff\xff\xff\xff\xff\xff\x00\x07\x21\x13\x01"
        base[126] = 1
        base[127] = (-sum(base[:127])) & 0xff
        cta = bytearray(128)
        cta[0:3] = bytes([2, 3, 15])
        cta[4:8] = bytes([0xe3, 5, 128, 0])
        cta[8:15] = bytes([0xe6, 6, 5, 1, 138, 96, 7])
        cta[127] = (-sum(cta[:127])) & 0xff
        original = bytes(base + cta)
        patched = main.patch_ayaneo_edid(original)
        self.assertIsNotNone(patched)
        self.assertEqual(patched[140], 128)
        self.assertEqual(sum(patched[128:256]) & 0xff, 0)
        self.assertEqual(patched[:140], original[:140])
        self.assertEqual(patched[141:255], original[141:255])
        self.assertAlmostEqual(main._published_edid_nits(patched), 800.0)

    def test_edid_patch_rejects_other_display(self):
        self.assertIsNone(main.patch_ayaneo_edid(bytes(256)))

    def test_hidraw_response_layout(self):
        response = bytes.fromhex("0000000800000c016600ff016600ff00000002000033223000000000000000014450000064640000000000000000000000000000000000000000000000000000")
        old = main._hid_exchange
        old_path = main._vendor_hidraw
        old_open, old_close = main.os.open, main.os.close
        old_nonblock = getattr(main.os, "O_NONBLOCK", None)
        try:
            main._vendor_hidraw = lambda: "/dev/fake"
            main.os.open = lambda *_: 1
            main.os.close = lambda *_: None
            main.os.O_NONBLOCK = 0
            main._hid_exchange = lambda *_: response
            value = main.read_controller()
        finally:
            main._hid_exchange = old
            main._vendor_hidraw = old_path
            main.os.open, main.os.close = old_open, old_close
            if old_nonblock is None:
                del main.os.O_NONBLOCK
            else:
                main.os.O_NONBLOCK = old_nonblock
        self.assertEqual(value["vibration"], "high")
        self.assertEqual(value["rgb_mode"], "solid")
        self.assertEqual(value["color"], "3c00ff")

if __name__ == "__main__": unittest.main()
