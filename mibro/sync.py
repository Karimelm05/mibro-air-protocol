"""
One sync = connect, greet, ask for what is missing, log every byte, ingest.

The conversation is written against a tiny Link interface (subscribe / write)
so it can be driven by a fake watch in tests; BleLink is the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import protocol as P
from .store import Store, iso

DEVICE_NAME = "Mibro Air"


# ------------------------------------------------------------------ plan --
@dataclass
class Plan:
    activity_start: dt.datetime
    activity_end: dt.datetime
    sleep_days: list[dt.datetime] = field(default_factory=list)


def make_plan(store: Store, now: dt.datetime, backfill_days: int = 1,
              max_days: int = 7) -> Plan:
    """
    Activity: from 00:00 of the last day we already hold (its in-progress hour
    needs refreshing) to now. First run: `backfill_days` back. Records are
    stable history (TESTING.md §5) so re-fetching overlaps is harmless.

    Sleep: one request per night since the last good sync, at least last
    night. Request date = now - k days, the exact shape confirmed for k=1.
    """
    floor = (now - dt.timedelta(days=max_days)).replace(hour=0, minute=0,
                                                         second=0, microsecond=0)
    last = store.last_activity_hour()
    if last is None:
        start = (now - dt.timedelta(days=backfill_days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    else:
        start = last.replace(hour=0, minute=0)
    start = max(start, floor)

    last_ok = store.last_ok_sync()
    if last_ok is None:
        nights = max(1, backfill_days)
    else:
        nights = (now.date() - last_ok.date()).days + 1
    nights = max(1, min(nights, max_days))
    sleep_days = [now - dt.timedelta(days=k) for k in range(nights, 0, -1)]
    return Plan(start, now.replace(second=0, microsecond=0), sleep_days)


# ------------------------------------------------------------- transport --
class BleLink:
    """bleak connection to the watch."""

    def __init__(self, address: str, adapter: str = "hci0", use_path: bool = True):
        self.address, self.adapter, self.use_path = address, adapter, use_path
        self.client = None

    def _device_by_path(self):
        # A connected peripheral stops advertising and BlueZ keeps reconnecting
        # it, so scanning never finds it. Build the device from its D-Bus path.
        from bleak.backends.device import BLEDevice
        path = (f"/org/bluez/{self.adapter}/dev_"
                + self.address.upper().replace(":", "_"))
        details = {"path": path, "props": {}}
        try:
            return BLEDevice(self.address, DEVICE_NAME, details)
        except TypeError:                         # older bleak wants rssi
            return BLEDevice(self.address, DEVICE_NAME, details, -60)

    async def __aenter__(self):
        from bleak import BleakClient
        target = self._device_by_path() if self.use_path else self.address
        self.client = BleakClient(target, timeout=20.0)
        await self.client.connect()
        return self

    async def __aexit__(self, *exc):
        with contextlib.suppress(Exception):
            await self.client.disconnect()

    async def subscribe(self, callback: Callable[[bytes], None]):
        await self.client.start_notify(P.UUID_NOTIFY, lambda _s, d: callback(bytes(d)))

    async def write(self, data: bytes):
        await self.client.write_gatt_char(P.UUID_WRITE, data, response=False)


# ---------------------------------------------------------- conversation --
class Conversation:
    def __init__(self, store: Store, sync_id: int, link,
                 log: Callable[[str], None] = lambda s: None):
        self.store, self.sync_id, self.link, self.log = store, sync_id, link, log
        self.rx_count = 0
        self.last_rx = 0.0

    def on_rx(self, data: bytes):
        self.store.add_packet(self.sync_id, "rx", data)
        self.rx_count += 1
        self.last_rx = time.monotonic()

    async def send(self, label: str, pkt: bytes):
        self.log(f"→ {label}")
        self.store.add_packet(self.sync_id, "tx", pkt)
        await self.link.write(pkt)

    async def settle(self, first: float = 6.0, idle: float = 2.5, cap: float = 40.0):
        """Wait until the watch has gone quiet. Ends after `first` seconds if
        nothing at all arrives, otherwise `idle` seconds after the last
        packet, and never later than `cap`."""
        t0 = time.monotonic()
        n0 = self.rx_count
        while True:
            await asyncio.sleep(0.2)
            t = time.monotonic()
            if t - t0 > cap:
                return
            if self.rx_count == n0:
                if t - t0 > first:
                    return
            elif t - self.last_rx > idle:
                return


async def converse(store: Store, sync_id: int, link, plan: Plan,
                   log: Callable[[str], None] = print, vibrate: bool = False,
                   timing: Optional[dict] = None):
    t = {"greet": 4.0, "gap": 1.5, "first": 6.0, "idle": 2.5, "cap": 40.0}
    t.update(timing or {})
    c = Conversation(store, sync_id, link, log)
    await link.subscribe(c.on_rx)
    await c.settle(first=t["greet"], idle=t["gap"], cap=t["cap"])   # greeting
    await c.send("client hello", P.phone_info())
    await asyncio.sleep(t["gap"])
    await c.send("time sync", P.time_sync(dt.datetime.now()))
    await asyncio.sleep(t["gap"])
    if vibrate:
        await c.send("vibrate", P.vibrate())
        await asyncio.sleep(t["gap"])
    await c.send(f"activity {plan.activity_start:%Y-%m-%d %H:%M} → "
                 f"{plan.activity_end:%Y-%m-%d %H:%M}",
                 P.req_activity(plan.activity_start, plan.activity_end))
    await c.settle(t["first"], t["idle"], t["cap"])
    for day in plan.sleep_days:
        await c.send(f"sleep for {day:%Y-%m-%d}", P.req_sleep(day))
        await c.settle(t["first"], t["idle"], t["cap"])
    return c.rx_count


@contextlib.contextmanager
def single_instance(lock_path: Path):
    """The timer and a manual `mibro sync` must not talk to the watch at once."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise RuntimeError("another sync is already running")
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


async def run_sync(store: Store, link, lock_path: Path, *, backfill_days: int = 1,
                   vibrate: bool = False, log: Callable[[str], None] = print,
                   timeout: float = 240.0, timing: Optional[dict] = None,
                   now: Optional[dt.datetime] = None) -> dict:
    with single_instance(lock_path):
        now = now or dt.datetime.now()
        plan = make_plan(store, now, backfill_days)
        sid = store.begin_sync("ble")
        log(f"sync #{sid}: activity from {iso(plan.activity_start)}, "
            f"{len(plan.sleep_days)} night(s)")
        try:
            async def go():
                async with link as l:
                    log("connected")
                    return await converse(store, sid, l, plan, log, vibrate, timing)
            rx = await asyncio.wait_for(go(), timeout)
        except BaseException as exc:            # incl. cancellation / Ctrl-C
            msg = f"{type(exc).__name__}: {exc}"
            store.finish_sync(sid, "error", msg)
            counts = store.ingest(sid)          # keep whatever did arrive
            log(f"sync failed: {msg}")
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            return {"sync_id": sid, "ok": False, "error": msg, **counts}
        store.finish_sync(sid, "ok")
        counts = store.ingest(sid)
        log(f"done: {rx} packets — {counts['activity']} hourly records, "
            f"{counts['sleep']} sleep segments")
        return {"sync_id": sid, "ok": True, "rx": rx, **counts}
