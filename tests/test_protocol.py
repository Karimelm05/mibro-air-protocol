"""
Protocol tests — no watch, no Bluetooth, no bleak required.

Every packet here is real traffic captured from a Mibro Air, or a request
byte-for-byte identical to one that was confirmed to work on the device.
The docstrings cite where each came from.

    python3 -m unittest discover tests -v
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mibro_lab2 import (  # noqa: E402
    date_bytes, decode, frame, phone_info, req_activity, req_sleep, time_sync,
)


def h(s):
    """'AB 00 03' -> bytes"""
    return bytes.fromhex(s.replace(" ", ""))


class TestFraming(unittest.TestCase):
    """AB 00 LEN FF CMD SUB — LEN counts from FF onward."""

    def test_length_byte_counts_from_ff(self):
        pkt = frame(0x71)
        self.assertEqual(pkt, h("AB 00 03 FF 71 80"))
        self.assertEqual(pkt[2], len(pkt) - 3)

    def test_length_invariant_holds_for_every_builder(self):
        now = datetime.datetime(2026, 9, 17, 16, 0, 0)
        builders = [
            phone_info(),
            time_sync(now),
            req_activity(now - datetime.timedelta(days=1), now),
            req_sleep(now),
            frame(0xA7, bytes([0x00])),
            frame(0xA8),
        ]
        for pkt in builders:
            with self.subTest(pkt=pkt.hex()):
                self.assertEqual(pkt[0], 0xAB)
                self.assertEqual(pkt[2], len(pkt) - 3)

    def test_date_bytes_encoding(self):
        """year-2000, month, day, hour, minute."""
        dt = datetime.datetime(2026, 9, 16, 23, 14)
        self.assertEqual(date_bytes(dt), h("1A 09 10 17 0E"))


class TestRequests(unittest.TestCase):
    """Requests confirmed against the hardware."""

    def test_client_hello(self):
        self.assertEqual(phone_info(), h("AB 00 06 FF 20 80 01 00 00"))

    def test_time_sync_matches_the_write_that_moved_the_clock(self):
        """This exact packet set the watch to 16:00 on 2026-09-17.
        0x07EA = 2026, big-endian, two bytes."""
        dt = datetime.datetime(2026, 9, 17, 16, 0, 0)
        self.assertEqual(
            time_sync(dt),
            h("AB 00 0B FF 93 80 00 07 EA 09 11 10 00 00"))

    def test_activity_request_shape(self):
        start = datetime.datetime(2026, 9, 16, 0, 0)
        end = datetime.datetime(2026, 9, 17, 23, 59)
        pkt = req_activity(start, end)
        self.assertEqual(pkt[:6], h("AB 00 0E FF 51 80"))
        self.assertEqual(pkt[7:12], date_bytes(start))
        self.assertEqual(pkt[12:17], date_bytes(end))

    def test_sleep_request_shape(self):
        day = datetime.datetime(2026, 9, 16, 0, 0)
        pkt = req_sleep(day)
        self.assertEqual(pkt[:6], h("AB 00 09 FF 52 80"))
        self.assertEqual(pkt[7:12], date_bytes(day))


class TestDecodeCaptured(unittest.TestCase):
    """Real notifications captured from the watch."""

    def test_battery(self):
        """Greeting packet; the watch face read 76% at the time."""
        self.assertEqual(decode(h("AB 00 05 FF 91 80 00 4C")),
                         "BATTERY = 76%")

    def test_activity_record(self):
        """Hourly record, 2026-09-16 23:00. Values are cumulative for the day:
        0x45AF = 17839 steps, 0x0003E3 = 995 cal, 0x68 = 104 bpm."""
        out = decode(h("AB 00 16 FF 51 20 1A 09 10 17 00 45 AF "
                       "00 03 E3 68 00 00 00"))
        self.assertIn("2026-09-16 23:00", out)
        self.assertIn("steps=17839", out)
        self.assertIn("HR=104 bpm", out)
        self.assertIn("cal=995", out)

    def test_activity_record_with_no_heart_rate(self):
        """0x00 in the HR byte means no measurement was taken that hour."""
        out = decode(h("AB 00 16 FF 51 20 1A 09 10 03 00 00 64 "
                       "00 00 0A 00 00 00 00"))
        self.assertIn("HR=--", out)

    def test_sleep_segment(self):
        """One segment: 2026-09-16 23:14, light sleep, 0x2C = 44 minutes."""
        out = decode(h("AB 00 0B FF 52 80 1A 09 10 17 0E 01 00 2C"))
        self.assertIn("2026-09-16 23:14", out)
        self.assertIn("light", out)
        self.assertIn("44 min", out)

    def test_deep_sleep_stage(self):
        out = decode(h("AB 00 0B FF 52 80 1A 09 11 01 00 02 00 1E"))
        self.assertIn("deep", out)
        self.assertIn("30 min", out)

    def test_unknown_packets_are_never_discarded(self):
        """The 0x9B status push is undecoded; the raw payload must survive
        so it can be re-parsed from the log later."""
        out = decode(h("AB 00 11 FF 9B 00 0F 01 00 00 00 00 00 "
                       "00 00 00 00 00 00 00"))
        self.assertIn("status push", out)
        self.assertIn("payload=00 0F 01", out)

    def test_wholly_unrecognised_command_still_reports_its_bytes(self):
        out = decode(h("AB 00 05 FF 7E 80 00 01"))
        self.assertIn("0x7E", out)
        self.assertIn("payload=", out)

    def test_second_fragment_is_flagged_not_dropped(self):
        """The 20-byte MTU splits 26-byte activity records. The tail does not
        start with 0xAB and is reported as such rather than mis-parsed."""
        out = decode(h("00 00 00 00 00 00"))
        self.assertTrue(out.startswith("non-AB packet"))


if __name__ == "__main__":
    unittest.main()
