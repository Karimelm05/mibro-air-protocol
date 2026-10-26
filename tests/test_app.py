"""
App tests — reassembly, parsing, storage, sync conversation, dashboard API.
No watch, no Bluetooth, no bleak needed. Packets are real captures from
TESTING.md unless a docstring says otherwise.

    python3 -m unittest discover tests -v
"""

import asyncio
import datetime as dt
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mibro import protocol as P  # noqa: E402
from mibro.store import Store  # noqa: E402
from mibro.sync import converse, make_plan, run_sync  # noqa: E402

h = P.unhx

GREETING = [
    h("AB 00 05 FF 91 80 00 4B"),
    h("AB 00 11 FF 92 C0 02 0A 00 81 3D 00 00 00 00 28 00 62 07 80"),
    h("AB 00 11 FF 9B 00 0F 01 00 00 00 00 00 00 00 00 00 00 00 00"),
]
# 02:00 record of 2026-09-17, both fragments (TESTING.md §7)
ACT_0200 = [h("AB 00 16 FF 51 20 1A 09 11 02 00 00 39 00 00 03 4A 00 00 00"),
            h("00 01 2B 00 0E 01")]

# The captured night of 2026-09-16, in the watch's storage order (TESTING.md §6)
NIGHT = [("2026-09-16 23:14", 1, 44), ("2026-09-17 00:46", 2, 8),
         ("2026-09-17 00:02", 1, 53), ("2026-09-17 01:42", 2, 6),
         ("2026-09-17 02:22", 2, 3), ("2026-09-17 02:59", 2, 24),
         ("2026-09-17 03:57", 2, 3), ("2026-09-17 04:34", 2, 13),
         ("2026-09-17 00:59", 1, 1), ("2026-09-17 05:21", 1, 30),
         ("2026-09-17 06:36", 2, 26), ("2026-09-17 05:52", 1, 70)]


def sleep_pkt(start, stage, minutes, flag=0):
    t = dt.datetime.fromisoformat(start)
    return P.frame(0x52, P.date_bytes(t) + bytes([stage, flag, minutes]))


def act_pkts(t, steps, cal, hr, tail=b"\x00" * 6):
    """Synthetic activity record in the captured layout, split at the MTU."""
    full = (h("AB 00 16 FF 51 20") + bytes([t.year - 2000, t.month, t.day, t.hour, 0])
            + steps.to_bytes(2, "big") + cal.to_bytes(3, "big") + bytes([hr, 0, 0, 0]) + tail)
    return [full[:20], full[20:]]


class TestSleepPacketHelper(unittest.TestCase):
    def test_helper_reproduces_a_captured_packet(self):
        self.assertEqual(sleep_pkt("2026-09-16 23:14", 1, 44),
                         h("AB 00 0B FF 52 80 1A 09 10 17 0E 01 00 2C"))


class TestReassembly(unittest.TestCase):
    def test_split_activity_record_is_joined(self):
        ra = P.Reassembler()
        out = ra.feed(ACT_0200[0]) + ra.feed(ACT_0200[1]) + ra.flush()
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), 26)
        self.assertEqual(out[0][20:], ACT_0200[1])

    def test_mtu_full_frame_followed_by_new_frame_is_not_merged(self):
        """The 20-byte version push is complete; the next AB frame is new."""
        ra = P.Reassembler()
        out = []
        for p in GREETING:
            out += ra.feed(p)
        out += ra.flush()
        self.assertEqual(out, GREETING)

    def test_short_frame_absorbs_continuation_even_if_it_starts_with_AB(self):
        """LEN says more is coming, so the next notification is a tail no
        matter what its first byte is. (Synthetic tail.)"""
        ra = P.Reassembler()
        out = ra.feed(ACT_0200[0]) + ra.feed(h("AB 01 2B 00 0E 01")) + ra.flush()
        self.assertEqual(len(out), 1)

    def test_orphan_fragment_survives(self):
        ra = P.Reassembler()
        self.assertEqual(ra.feed(h("00 01 2B")), [h("00 01 2B")])


class TestParse(unittest.TestCase):
    def test_activity_with_tail(self):
        rec = P.parse(ACT_0200[0] + ACT_0200[1])
        self.assertIsInstance(rec, P.ActivityHour)
        self.assertEqual(rec.hour, dt.datetime(2026, 9, 17, 2))
        self.assertEqual((rec.steps_cum, rec.cal_cum, rec.hr), (57, 3, 74))
        self.assertEqual(rec.tail_deep_min, 14)       # README §V.6 table

    def test_zero_hr_is_none(self):
        rec = P.parse(h("AB 00 16 FF 51 20 1A 09 10 03 00 00 64 00 00 0A 00 00 00 00"))
        self.assertIsNone(rec.hr)

    def test_day_summary(self):
        rec = P.parse(h("AB 00 0E FF 51 08 00 12 B6 00 01 0B 00 00 00 00 00"))
        self.assertEqual((rec.steps, rec.calories), (4790, 267))

    def test_battery_and_version(self):
        self.assertEqual(P.parse(GREETING[0]).percent, 75)
        v = P.parse(GREETING[1])
        self.assertEqual((v.ota, v.band), ("2.10", 317))

    def test_unknown_keeps_bytes(self):
        rec = P.parse(GREETING[2])
        self.assertIsInstance(rec, P.Unknown)
        self.assertEqual(rec.raw, GREETING[2])

    def test_impossible_date_does_not_raise(self):
        rec = P.parse(h("AB 00 0B FF 52 80 1A 0D 20 17 0E 01 00 2C"))   # month 13
        self.assertIsInstance(rec, P.Unknown)

    def test_parse_request_round_trip(self):
        a, b = dt.datetime(2026, 9, 16, 0, 0), dt.datetime(2026, 9, 17, 16, 0)
        self.assertEqual(P.parse_request(P.req_activity(a, b)), ("activity", a, b))


class TestSleepAlgebra(unittest.TestCase):
    def segs(self):
        return [P.parse(sleep_pkt(*s)) for s in NIGHT]

    def test_captured_night_is_247_minutes_not_281(self):
        segs = self.segs()
        self.assertEqual(sum(s.minutes for s in segs), 281)
        n = P.night_summary(segs)
        self.assertEqual(n["asleep_min"], 247)
        self.assertEqual(n["start"], dt.datetime(2026, 9, 16, 23, 14))
        self.assertEqual(n["end"], dt.datetime(2026, 9, 17, 7, 2))

    def test_whole_night_belongs_to_the_evening_date(self):
        self.assertEqual({P.night_of(s.start) for s in self.segs()},
                         {dt.date(2026, 9, 16)})


class TestStore(unittest.TestCase):
    def setUp(self):
        self.st = Store(":memory:")

    def tearDown(self):
        self.st.close()

    def feed(self, packets, ts="2026-09-17 16:00:35", tx=None):
        sid = self.st.begin_sync("ble", ts)
        if tx:
            self.st.add_packet(sid, "tx", tx, ts)
        for p in packets:
            self.st.add_packet(sid, "rx", p, ts)
        self.st.finish_sync(sid)
        return sid, self.st.ingest(sid)

    def test_summary_is_kept_only_for_the_day_it_was_received(self):
        req = P.req_activity(dt.datetime(2026, 9, 17, 0, 0), dt.datetime(2026, 9, 17, 16, 0))
        summ = h("AB 00 0E FF 51 08 00 12 B6 00 01 0B 00 00 00 00 00")
        _, c = self.feed([summ], tx=req)
        self.assertEqual(c["summary"], 1)
        self.assertEqual(self.st.day_totals("2026-09-17")["steps"], 4790)
        # same summary, but the request ended on a different day: unknown day
        old = P.req_activity(dt.datetime(2026, 9, 15), dt.datetime(2026, 9, 16, 23, 0))
        _, c = self.feed([summ], tx=old, ts="2026-09-18 10:00:00")
        self.assertEqual(c["summary"], 0)

    def test_hourly_deltas_from_cumulative_counters(self):
        pk = []
        for hr, steps in ((0, 10), (1, 57), (2, 57), (3, 500)):
            pk += act_pkts(dt.datetime(2026, 9, 17, hr), steps, steps // 20, 70 if hr else 0)
        self.feed(pk)
        hours = self.st.day_hours("2026-09-17")
        self.assertEqual([x["steps"] for x in hours], [10, 47, 0, 443])
        self.assertIsNone(hours[0]["hr"])

    def test_ingest_is_idempotent_and_reparse_rebuilds(self):
        pk = GREETING + ACT_0200 + [sleep_pkt(*s) for s in NIGHT]
        sid, c1 = self.feed(pk)
        c2 = self.st.ingest(sid)
        self.assertEqual(c1["activity"], 1)
        self.assertEqual(c1["sleep"], 12)
        self.assertEqual(c1["unparsed"], 1)            # the 0x9B status push
        self.assertEqual(self.st.night("2026-09-16")["asleep_min"], 247)
        self.st.reparse()
        self.assertEqual(self.st.night("2026-09-16")["asleep_min"], 247)
        n = self.st.db.execute("SELECT COUNT(*) FROM sleep_segments").fetchone()[0]
        self.assertEqual(n, 12)
        self.assertEqual(c1, c2)
        self.assertEqual(self.st.device()["ota_version"], "2.10")


class TestFingerprint(unittest.TestCase):
    """The dashboard redraws when this changes — including when the change is
    made by a different process (the timer), i.e. a different connection."""

    def test_changes_when_another_connection_adds_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "f.db"
            watcher, writer = Store(path), Store(path)
            before = watcher.fingerprint()
            self.assertEqual(before, watcher.fingerprint())      # stable at rest
            sid = writer.begin_sync("ble", "2026-09-17 16:00:00")
            for p in ACT_0200:
                writer.add_packet(sid, "rx", p, "2026-09-17 16:00:00")
            writer.finish_sync(sid)
            writer.ingest(sid)
            after = watcher.fingerprint()
            self.assertNotEqual(before, after)
            # a second sync that rewrites the same hour is also a change
            sid2 = writer.begin_sync("ble", "2026-09-17 17:00:00")
            for p in ACT_0200:
                writer.add_packet(sid2, "rx", p, "2026-09-17 17:00:00")
            writer.finish_sync(sid2)
            writer.ingest(sid2)
            self.assertNotEqual(after, watcher.fingerprint())
            watcher.close()
            writer.close()


class FakeWatch:
    """Replays captured traffic: greeting on subscribe, records on request."""

    def __init__(self, activity, sleep):
        self.activity, self.sleep, self.cb, self.writes = activity, sleep, None, []
        self.hello = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def subscribe(self, cb):
        self.cb = cb
        for p in GREETING:
            cb(p)

    async def write(self, data):
        self.writes.append(data)
        cmd = data[4]
        if cmd == 0x20:
            self.hello = True
        if not self.hello:
            return                      # README §IV.5: ignored before hello
        if cmd == 0x51:
            for p in self.activity:
                await asyncio.sleep(0.005)
                self.cb(p)
        if cmd == 0x52:
            for p in self.sleep:
                await asyncio.sleep(0.005)
                self.cb(p)


FAST = {"greet": 0.3, "gap": 0.05, "first": 0.4, "idle": 0.25, "cap": 3}


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "t.db")
        self.lock = Path(self.tmp.name) / "t.lock"

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def test_first_plan_is_yesterday_midnight_to_now(self):
        now = dt.datetime(2026, 10, 26, 11, 18, 7)
        p = make_plan(self.st, now)
        self.assertEqual(p.activity_start, dt.datetime(2026, 10, 25, 0, 0))
        self.assertEqual(p.activity_end, dt.datetime(2026, 10, 26, 11, 18))
        self.assertEqual([d.date() for d in p.sleep_days], [dt.date(2026, 10, 25)])

    def test_full_sync_against_fake_watch(self):
        pk = []
        for hr in range(0, 16):
            pk += act_pkts(dt.datetime(2026, 9, 17, hr), 300 * hr, 15 * hr, 60 + hr)
        watch = FakeWatch(pk, [sleep_pkt(*s) for s in NIGHT])
        res = asyncio.run(run_sync(self.st, watch, self.lock, log=lambda m: None,
                                   timing=FAST, now=dt.datetime(2026, 9, 17, 16, 0)))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["activity"], 16)
        self.assertEqual(res["sleep"], 12)
        cmds = [w[4] for w in watch.writes]
        self.assertEqual(cmds[:2], [0x20, 0x93])          # opening sequence first
        self.assertIn(0x51, cmds)
        self.assertEqual(self.st.night("2026-09-16")["asleep_min"], 247)
        self.assertEqual(self.st.day_totals("2026-09-17")["steps"], 4500)
        # second plan resumes from the stored day
        p = make_plan(self.st, dt.datetime(2026, 9, 17, 17, 0))
        self.assertEqual(p.activity_start, dt.datetime(2026, 9, 17, 0, 0))

    def test_failed_sync_keeps_what_arrived(self):
        class Broken(FakeWatch):
            async def write(self, data):
                await super().write(data)
                if data[4] == 0x51:
                    raise OSError("link lost")
        w = Broken(ACT_0200, [])
        res = asyncio.run(run_sync(self.st, w, self.lock, log=lambda m: None,
                                   timing=FAST, now=dt.datetime(2026, 9, 17, 16, 0)))
        self.assertFalse(res["ok"])
        self.assertEqual(res["activity"], 1)
        self.assertEqual(self.st.syncs(1)[0]["status"], "error")

    def test_second_sync_is_refused_while_one_runs(self):
        from mibro.sync import single_instance
        with single_instance(self.lock):
            with self.assertRaises(RuntimeError):
                with single_instance(self.lock):
                    pass


class TestImportLog(unittest.TestCase):
    def test_lab_log_is_imported(self):
        from mibro import cli, config
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "mibro_session.log"
            log.write_text(
                "23:21:40 connecting to D4:73:00:00:00:00\n"
                "23:21:41 RX  AB 00 05 FF 91 80 00 4A\n"
                "23:21:41     -> BATTERY = 74%\n"
                "23:21:52 TX  AB 00 09 FF 52 80 00 1A 09 10 17 15   (sleep for 2026-09-16)\n"
                + "".join(f"23:21:53 RX  {P.hx(sleep_pkt(*s))}\n" for s in NIGHT))
            cfg = config.Config(db_path=Path(d) / "x.db")
            cli.cmd_import_log(cfg, type("A", (), {"file": str(log), "date": "2026-09-17"}))
            st = Store(cfg.db_path)
            self.assertEqual(st.night("2026-09-16")["asleep_min"], 247)
            self.assertEqual(st.battery_series()[-1]["percent"], 74)
            st.close()


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mibro import config
        from mibro.web import make_handler
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = config.Config(db_path=Path(cls.tmp.name) / "w.db", address="")
        cls.st = Store(cfg.db_path)
        sid = cls.st.begin_sync("import", "2026-09-17 16:00:00")
        for p in GREETING + ACT_0200 + [sleep_pkt(*s) for s in NIGHT]:
            cls.st.add_packet(sid, "rx", p, "2026-09-17 16:00:00")
        cls.st.finish_sync(sid)
        cls.st.ingest(sid)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, cls.st, 0))
        cls.port = cls.srv.server_address[1]
        cls.srv.RequestHandlerClass = make_handler(cfg, cls.st, cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.st.close()
        cls.tmp.cleanup()

    def req(self, method, path, host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        hd = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        c.request(method, path, headers=hd)
        r = c.getresponse()
        return r.status, r.read()

    def test_page_and_day_api(self):
        s, body = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn(b"Mibro", body)
        s, body = self.req("GET", "/api/day?date=2026-09-17")
        d = json.loads(body)
        self.assertEqual(d["night"]["asleep_min"], 247)
        self.assertEqual(d["hours"][0]["hr"], 74)

    def test_version_endpoint(self):
        s, body = self.req("GET", "/api/version")
        d = json.loads(body)
        self.assertEqual(s, 200)
        self.assertIn("|", d["v"])
        self.assertEqual(d["today"], dt.date.today().isoformat())
        s, _ = self.req("GET", "/api/version", host="evil.example:80")
        self.assertEqual(s, 403)                 # same Host check as the rest

    def test_foreign_host_is_refused(self):
        s, _ = self.req("GET", "/api/status", host="evil.example:80")
        self.assertEqual(s, 403)

    def test_sync_needs_custom_header(self):
        s, _ = self.req("POST", "/api/sync")
        self.assertEqual(s, 403)
        s, body = self.req("POST", "/api/sync", headers={"X-Mibro": "1"})
        self.assertEqual(s, 400)                     # no address configured
        self.assertIn(b"mibro setup", body)

    def test_bad_date_is_400(self):
        s, _ = self.req("GET", "/api/day?date=nope")
        self.assertEqual(s, 400)


if __name__ == "__main__":
    unittest.main()
