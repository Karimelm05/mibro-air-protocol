"""
Mibro Air 0xAB protocol — packet builders, fragment reassembly, record parsing.

Pure functions only: no Bluetooth, no database. Everything here can be tested
against captured hex with no watch present. The wire format is documented in
README.md §V; this module turns it into typed records.

Frame:  AB 00 LEN FF CMD SUB [payload...]    LEN = bytes from FF onward
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional, Union

UUID_WRITE = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
UUID_NOTIFY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
MTU_PAYLOAD = 20          # bytes per notification; longer frames are split


def hx(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def unhx(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# ---------------------------------------------------------------- builders --
def frame(cmd: int, payload: bytes = b"", sub: int = 0x80) -> bytes:
    body = bytes([0xFF, cmd, sub]) + payload
    return bytes([0xAB, 0x00, len(body)]) + body


def date_bytes(t: dt.datetime) -> bytes:
    """year-2000, month, day, hour, minute."""
    return bytes([t.year - 2000, t.month, t.day, t.hour, t.minute])


def phone_info() -> bytes:
    """0x20 client hello — the watch ignores queries until it gets this."""
    return frame(0x20, bytes([0x01, 0x00, 0x00]))


def time_sync(t: dt.datetime) -> bytes:
    """0x93 set clock. Two-byte big-endian year, unlike every other date."""
    return frame(0x93, bytes([0x00, (t.year >> 8) & 0xFF, t.year & 0xFF,
                              t.month, t.day, t.hour, t.minute, t.second]))


def req_activity(start: dt.datetime, end: dt.datetime) -> bytes:
    return frame(0x51, bytes([0x00]) + date_bytes(start) + date_bytes(end))


def req_sleep(day: dt.datetime) -> bytes:
    return frame(0x52, bytes([0x00]) + date_bytes(day))


def vibrate() -> bytes:
    return frame(0x71)


def parse_request(data: bytes) -> Optional[tuple]:
    """Decode an outgoing request we built — gives ingest the context of what
    was asked (e.g. which day a 0x51 day-summary belongs to)."""
    if len(data) < 6 or data[0] != 0xAB:
        return None
    cmd = data[4]
    try:
        if cmd == 0x51 and len(data) >= 17:
            return ("activity", _ts(data[7:12]), _ts(data[12:17]))
        if cmd == 0x52 and len(data) >= 12:
            return ("sleep", _ts(data[7:12]))
    except ValueError:
        return None
    return (f"cmd_{cmd:02x}",)


def _ts(b: bytes) -> dt.datetime:
    return dt.datetime(2000 + b[0], b[1], b[2], b[3], b[4])


# ------------------------------------------------------------- reassembly --
class Reassembler:
    """
    Joins frames that the 20-byte MTU split across notifications.

    Rule: a notification is a continuation of the previous frame when
      - the previous frame is shorter than its own LEN says it should be, or
      - it does not start with 0xAB and the previous frame filled the MTU.
    Continuations are appended whole and never truncated to LEN: the 0x51
    activity record declares LEN=0x16 (25 bytes) yet 26 arrive, and the 26th
    byte carries data (README §V.6, "byte 5" of the tail).

    A frame is emitted when the next frame starts, or on flush().
    """

    def __init__(self, mtu: int = MTU_PAYLOAD):
        self.mtu = mtu
        self.pending: Optional[bytearray] = None
        self.parts = 0

    @staticmethod
    def expected_len(buf: bytes) -> int:
        return buf[2] + 3 if len(buf) >= 3 else 0

    def feed(self, data: bytes) -> list[bytes]:
        data = bytes(data)
        out: list[bytes] = []
        if self.pending is not None:
            short = len(self.pending) < self.expected_len(self.pending)
            if short or data[:1] != b"\xAB":
                self.pending += data
                self.parts += 1
                return out
            out += self.flush()
        if data[:1] == b"\xAB" and len(data) >= self.mtu:
            # MTU-full: a continuation may follow; hold it.
            self.pending = bytearray(data)
            self.parts = 1
        else:
            out.append(data)        # complete frame, or an orphan fragment
        return out

    def flush(self) -> list[bytes]:
        if self.pending is None:
            return []
        f = bytes(self.pending)
        self.pending, self.parts = None, 0
        return [f]


# ---------------------------------------------------------------- records --
@dataclass
class Battery:
    percent: int


@dataclass
class Version:
    ota: str
    band: int


@dataclass
class DaySummary:
    steps: int
    calories: int


@dataclass
class ActivityHour:
    hour: dt.datetime            # start of the hour, watch-local time
    steps_cum: int               # cumulative within the day
    cal_cum: int
    hr: Optional[int]            # None when the watch took no reading
    tail: bytes = b""            # bytes past offset 20 — partly decoded
    byte10: int = 0              # undecoded

    @property
    def tail_deep_min(self) -> Optional[int]:
        """Hypothesis (README §V.6): tail bytes 3-4 = cumulative deep sleep."""
        return int.from_bytes(self.tail[3:5], "big") if len(self.tail) >= 5 else None


@dataclass
class SleepSegment:
    start: dt.datetime
    stage: int                   # 1 light, 2 deep
    minutes: int
    flag: int = 0

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(minutes=self.minutes)

    @property
    def stage_name(self) -> str:
        return {1: "light", 2: "deep"}.get(self.stage, f"stage{self.stage}")


@dataclass
class Unknown:
    cmd: Optional[int]
    raw: bytes = field(repr=False)
    reason: str = ""


Record = Union[Battery, Version, DaySummary, ActivityHour, SleepSegment, Unknown]


def parse(data: bytes) -> Record:
    """One reassembled frame -> one record. Never raises; unknown stays raw."""
    if len(data) < 5 or data[0] != 0xAB:
        return Unknown(None, data, "not an AB frame (orphan fragment?)")
    cmd, sub = data[4], data[5] if len(data) > 5 else None
    try:
        if cmd == 0x91 and len(data) > 7:
            return Battery(data[7])
        if cmd == 0x92 and len(data) > 10:
            return Version(f"{data[6]}.{data[7]:02d}",
                           ((data[9] & 0x7F) << 8) + data[10])
        if cmd == 0x51 and sub == 0x08 and len(data) >= 12:
            return DaySummary(int.from_bytes(data[7:9], "big"),
                              int.from_bytes(data[9:12], "big"))
        if cmd == 0x51 and sub == 0x20 and len(data) >= 17:
            hour = dt.datetime(2000 + data[6], data[7], data[8], data[9])
            return ActivityHour(
                hour=hour,
                steps_cum=int.from_bytes(data[11:13], "big"),
                cal_cum=int.from_bytes(data[13:16], "big"),
                hr=data[16] or None,
                tail=data[20:],
                byte10=data[10],
            )
        if cmd == 0x52 and len(data) >= 14:
            start = dt.datetime(2000 + data[6], data[7], data[8],
                                data[9], data[10])
            return SleepSegment(start, data[11], data[13], data[12])
    except ValueError as exc:          # impossible date — keep it raw
        return Unknown(cmd, data, f"bad field: {exc}")
    return Unknown(cmd, data, "undecoded")


# ---------------------------------------------------------- sleep algebra --
def night_of(t: dt.datetime) -> dt.date:
    """Nights run noon-to-noon: 23:14 on the 16th and 07:02 on the 17th are
    both the night of the 16th."""
    return (t - dt.timedelta(hours=12)).date()


def union_minutes(intervals: list[tuple[dt.datetime, dt.datetime]]) -> int:
    """Total covered minutes. Segments overlap (deep sits inside light), so
    summing durations over-counts — README §V.5."""
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return int(total // 60)


def night_summary(segments: list[SleepSegment]) -> dict:
    if not segments:
        return {"asleep_min": 0, "deep_min": 0, "light_min": 0,
                "start": None, "end": None, "segments": 0}
    iv = [(s.start, s.end) for s in segments]
    deep = union_minutes([(s.start, s.end) for s in segments if s.stage == 2])
    asleep = union_minutes(iv)
    return {
        "asleep_min": asleep,
        "deep_min": deep,
        "light_min": asleep - deep,
        "start": min(s for s, _ in iv),
        "end": max(e for _, e in iv),
        "segments": len(segments),
    }
