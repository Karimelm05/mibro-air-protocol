"""
SQLite log of everything the watch has said.

Two layers, deliberately:

  raw_packets      every byte sent and received, per sync, in order — the
                   source of truth. Never edited.
  parsed tables    activity_hourly, sleep_segments, battery, ... — derived
                   from raw_packets by ingest(). Can be dropped and rebuilt
                   at any time with reparse(), e.g. after the decoder learns
                   a new field.

All timestamps are watch-local naive ISO strings ("YYYY-MM-DD HH:MM:SS").
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from . import protocol as P

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS syncs (
    id            INTEGER PRIMARY KEY,
    started       TEXT NOT NULL,
    finished      TEXT,
    source        TEXT NOT NULL DEFAULT 'ble',   -- ble | import
    status        TEXT NOT NULL DEFAULT 'running', -- running | ok | error
    error         TEXT,
    rx_count      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS raw_packets (
    id        INTEGER PRIMARY KEY,
    sync_id   INTEGER NOT NULL REFERENCES syncs(id),
    ts        TEXT NOT NULL,
    dir       TEXT NOT NULL CHECK (dir IN ('rx', 'tx')),
    hex       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_by_sync ON raw_packets(sync_id, id);

CREATE TABLE IF NOT EXISTS activity_hourly (
    hour       TEXT PRIMARY KEY,     -- start of hour
    day        TEXT NOT NULL,
    steps_cum  INTEGER NOT NULL,
    cal_cum    INTEGER NOT NULL,
    hr         INTEGER,              -- NULL = no reading that hour
    byte10     INTEGER,
    tail_hex   TEXT,                 -- partly decoded, kept for research
    sync_id    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS act_by_day ON activity_hourly(day);

CREATE TABLE IF NOT EXISTS day_summary (
    day       TEXT PRIMARY KEY,
    steps     INTEGER NOT NULL,
    calories  INTEGER NOT NULL,
    seen_at   TEXT NOT NULL,
    sync_id   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sleep_segments (
    start     TEXT NOT NULL,
    stage     INTEGER NOT NULL,
    minutes   INTEGER NOT NULL,
    flag      INTEGER,
    night     TEXT NOT NULL,         -- noon-to-noon night it belongs to
    sync_id   INTEGER NOT NULL,
    PRIMARY KEY (start, stage)
);
CREATE INDEX IF NOT EXISTS sleep_by_night ON sleep_segments(night);

CREATE TABLE IF NOT EXISTS battery (
    ts       TEXT PRIMARY KEY,
    percent  INTEGER NOT NULL,
    sync_id  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS device_info (
    key      TEXT PRIMARY KEY,
    value    TEXT NOT NULL,
    updated  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unparsed (
    id       INTEGER PRIMARY KEY,
    sync_id  INTEGER NOT NULL,
    ts       TEXT NOT NULL,
    cmd      INTEGER,
    hex      TEXT NOT NULL,
    reason   TEXT
);
"""

DERIVED = ["activity_hourly", "day_summary", "sleep_segments", "battery",
           "device_info", "unparsed"]


def now_s() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self):
        self.db.close()

    # ------------------------------------------------------------ writing --
    def begin_sync(self, source: str = "ble", started: Optional[str] = None) -> int:
        cur = self.db.execute(
            "INSERT INTO syncs(started, source) VALUES (?, ?)",
            (started or now_s(), source))
        self.db.commit()
        return cur.lastrowid

    def add_packet(self, sync_id: int, direction: str, data: bytes,
                   ts: Optional[str] = None) -> None:
        """Written immediately, so a crash mid-sync still keeps the bytes."""
        self.db.execute(
            "INSERT INTO raw_packets(sync_id, ts, dir, hex) VALUES (?,?,?,?)",
            (sync_id, ts or now_s(), direction, P.hx(data)))
        self.db.commit()

    def finish_sync(self, sync_id: int, status: str = "ok",
                    error: Optional[str] = None) -> None:
        rx = self.db.execute(
            "SELECT COUNT(*) FROM raw_packets WHERE sync_id=? AND dir='rx'",
            (sync_id,)).fetchone()[0]
        self.db.execute(
            "UPDATE syncs SET finished=?, status=?, error=?, rx_count=? WHERE id=?",
            (now_s(), status, error, rx, sync_id))
        self.db.commit()

    # ------------------------------------------------------------- ingest --
    def ingest(self, sync_id: int) -> dict:
        """Parse one sync's raw packets into the derived tables. Idempotent;
        later syncs overwrite earlier values for the same hour/segment."""
        rows = self.db.execute(
            "SELECT ts, dir, hex FROM raw_packets WHERE sync_id=? ORDER BY id",
            (sync_id,)).fetchall()
        counts = {"activity": 0, "sleep": 0, "battery": 0, "summary": 0,
                  "unparsed": 0}
        ra = P.Reassembler()
        last_req = None

        def handle(frame: bytes, ts: str):
            rec = P.parse(frame)
            if isinstance(rec, P.ActivityHour):
                self.db.execute(
                    "INSERT OR REPLACE INTO activity_hourly VALUES (?,?,?,?,?,?,?,?)",
                    (iso(rec.hour), rec.hour.date().isoformat(), rec.steps_cum,
                     rec.cal_cum, rec.hr, rec.byte10, P.hx(rec.tail), sync_id))
                counts["activity"] += 1
            elif isinstance(rec, P.SleepSegment):
                self.db.execute(
                    "INSERT OR REPLACE INTO sleep_segments VALUES (?,?,?,?,?,?)",
                    (iso(rec.start), rec.stage, rec.minutes, rec.flag,
                     P.night_of(rec.start).isoformat(), sync_id))
                counts["sleep"] += 1
            elif isinstance(rec, P.Battery):
                self.db.execute(
                    "INSERT OR REPLACE INTO battery VALUES (?,?,?)",
                    (ts, rec.percent, sync_id))
                counts["battery"] += 1
            elif isinstance(rec, P.Version):
                for k, v in (("ota_version", rec.ota), ("band_version", str(rec.band))):
                    self.db.execute(
                        "INSERT OR REPLACE INTO device_info VALUES (?,?,?)", (k, v, ts))
            elif isinstance(rec, P.DaySummary):
                # Only trust the summary for "today": confirmed on hardware when
                # the request ends on the day it was received. Otherwise which
                # day it describes is unknown — it stays in raw_packets.
                rx_day = ts[:10]
                if last_req and last_req[0] == "activity" \
                        and last_req[2].date().isoformat() == rx_day:
                    self.db.execute(
                        "INSERT OR REPLACE INTO day_summary VALUES (?,?,?,?,?)",
                        (rx_day, rec.steps, rec.calories, ts, sync_id))
                    counts["summary"] += 1
            else:
                self.db.execute(
                    "INSERT INTO unparsed(sync_id, ts, cmd, hex, reason) VALUES (?,?,?,?,?)",
                    (sync_id, ts, rec.cmd, P.hx(rec.raw), rec.reason))
                counts["unparsed"] += 1

        ts = None
        for r in rows:
            data = P.unhx(r["hex"])
            if r["dir"] == "tx":
                for f in ra.flush():
                    handle(f, ts or r["ts"])
                last_req = P.parse_request(data)
                continue
            ts = r["ts"]
            for f in ra.feed(data):
                handle(f, ts)
        for f in ra.flush():
            handle(f, ts or now_s())
        self.db.commit()
        return counts

    def reparse(self) -> dict:
        """Drop every derived table and rebuild it from raw_packets."""
        for t in DERIVED:
            self.db.execute(f"DELETE FROM {t}")
        self.db.commit()
        total: dict = {}
        for (sid,) in self.db.execute("SELECT id FROM syncs ORDER BY id").fetchall():
            for k, v in self.ingest(sid).items():
                total[k] = total.get(k, 0) + v
        return total

    # ------------------------------------------------------------ queries --
    def fingerprint(self) -> str:
        """A short string that changes whenever new data lands, from any
        process. The dashboard polls it to know when to redraw; it is much
        cheaper than re-sending every chart. Seven small aggregates; even a
        year of hourly rows (~9k) is well under a millisecond."""
        row = self.db.execute("""
            SELECT (SELECT COALESCE(MAX(id), 0)        FROM syncs),
                   (SELECT COALESCE(MAX(finished), '') FROM syncs),
                   (SELECT COALESCE(MAX(sync_id), 0)   FROM activity_hourly),
                   (SELECT COUNT(*)                    FROM activity_hourly),
                   (SELECT COALESCE(MAX(sync_id), 0)   FROM sleep_segments),
                   (SELECT COUNT(*)                    FROM sleep_segments),
                   (SELECT COALESCE(MAX(ts), '')       FROM battery)""").fetchone()
        return "|".join(str(v) for v in row)

    def last_activity_hour(self) -> Optional[dt.datetime]:
        r = self.db.execute("SELECT MAX(hour) FROM activity_hourly").fetchone()[0]
        return dt.datetime.fromisoformat(r) if r else None

    def last_ok_sync(self) -> Optional[dt.datetime]:
        r = self.db.execute(
            "SELECT MAX(started) FROM syncs WHERE status='ok' AND source='ble'"
        ).fetchone()[0]
        return dt.datetime.fromisoformat(r) if r else None

    def day_hours(self, day: str) -> list[dict]:
        """Hourly rows for one day, with per-hour deltas from the cumulative
        counters (the watch reports running totals)."""
        rows = self.db.execute(
            "SELECT hour, steps_cum, cal_cum, hr FROM activity_hourly "
            "WHERE day=? ORDER BY hour", (day,)).fetchall()
        out, ps, pc = [], 0, 0
        for r in rows:
            out.append({
                "hour": r["hour"][11:16],
                "steps": max(0, r["steps_cum"] - ps),
                "calories": max(0, r["cal_cum"] - pc),
                "steps_cum": r["steps_cum"],
                "hr": r["hr"],
            })
            ps, pc = r["steps_cum"], r["cal_cum"]
        return out

    def day_totals(self, day: str) -> dict:
        a = self.db.execute(
            "SELECT MAX(steps_cum) s, MAX(cal_cum) c, MIN(hr) hmin, "
            "AVG(hr) havg, MAX(hr) hmax, COUNT(hr) hn, COUNT(*) n "
            "FROM activity_hourly WHERE day=?", (day,)).fetchone()
        s = self.db.execute(
            "SELECT steps, calories FROM day_summary WHERE day=?", (day,)).fetchone()
        steps = max(a["s"] or 0, s["steps"] if s else 0)
        cal = max(a["c"] or 0, s["calories"] if s else 0)
        return {
            "day": day, "steps": steps, "calories": cal,
            "hr_min": a["hmin"], "hr_max": a["hmax"],
            "hr_avg": round(a["havg"]) if a["havg"] else None,
            "hr_readings": a["hn"], "hours": a["n"],
        }

    def night(self, night: str) -> dict:
        segs = [P.SleepSegment(dt.datetime.fromisoformat(r["start"]), r["stage"],
                               r["minutes"], r["flag"] or 0)
                for r in self.db.execute(
                    "SELECT * FROM sleep_segments WHERE night=? ORDER BY start",
                    (night,))]
        s = P.night_summary(segs)
        s["night"] = night
        s["start"] = iso(s["start"]) if s["start"] else None
        s["end"] = iso(s["end"]) if s["end"] else None
        s["items"] = [{"start": iso(x.start), "end": iso(x.end), "stage": x.stage_name,
                       "minutes": x.minutes} for x in segs]
        return s

    def known_days(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT day FROM activity_hourly UNION SELECT day FROM day_summary "
            "UNION SELECT night FROM sleep_segments ORDER BY 1 DESC")]

    def overview(self, days: int = 30, today: Optional[dt.date] = None) -> list[dict]:
        today = today or dt.date.today()
        out = []
        for i in range(days - 1, -1, -1):
            d = (today - dt.timedelta(days=i)).isoformat()
            row = self.day_totals(d)
            n = self.night((today - dt.timedelta(days=i + 1)).isoformat())
            row["sleep_min"] = n["asleep_min"]   # the night *before* this day
            row["deep_min"] = n["deep_min"]
            out.append(row)
        return out

    def battery_series(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT ts, percent FROM battery ORDER BY ts DESC LIMIT ?", (limit,))][::-1]

    def syncs(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM syncs ORDER BY id DESC LIMIT ?", (limit,))]

    def device(self) -> dict:
        return {r["key"]: r["value"] for r in self.db.execute("SELECT * FROM device_info")}

    def tail_rows(self, day: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT hour, tail_hex, byte10 FROM activity_hourly WHERE day=? ORDER BY hour",
            (day,))]

    def import_lines(self, lines: Iterable[tuple[str, str, bytes]],
                     started: str) -> int:
        """Import (ts, dir, bytes) triples as one 'import' sync."""
        sid = self.begin_sync("import", started)
        for ts, d, data in lines:
            self.add_packet(sid, d, data, ts)
        self.finish_sync(sid, "ok")
        return sid
