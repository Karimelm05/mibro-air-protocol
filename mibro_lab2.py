#!/usr/bin/env python3
"""
Mibro Air protocol lab  0xAB family.

The Mibro Air speaks the OLDER Xiaoxun protocol (0xAB framing), not the
newer 0x88 one. No handshake or authentication is required: the watch
pushes notifications as soon as you subscribe.

Packet format (confirmed empirically):
    AB 00 LEN FF CMD 80 [payload...]
    LEN = number of bytes from the FF onward

See docs/protocol.md for the full reference.

    pip install bleak
    export MIBRO_ADDR=AA:BB:CC:DD:EE:FF
    python3 mibro_lab2.py [--vibrate] [--steps] [--sleep] [--listen]
"""

import argparse
import asyncio
import datetime
import os
import sys

DEVICE_NAME = "Mibro Air"          # fallback if the random address rotated
LOG_PATH = "mibro_session.log"

# Set MIBRO_ADDR in the environment, or pass --address. Find yours with:
#   bluetoothctl devices
ADDRESS = os.environ.get("MIBRO_ADDR", "")

UUID_WRITE = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
UUID_NOTIFY = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

_log_file = None


def log(msg):
    """Print and append to the session log. The file is opened on first use,
    so importing this module (e.g. from tests) writes nothing."""
    global _log_file
    line = f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line)
    if _log_file is None:
        _log_file = open(LOG_PATH, "a")
    _log_file.write(line + "\n")
    _log_file.flush()


def hx(data):
    return " ".join(f"{b:02X}" for b in data)


# ---- packet construction (pure, no BLE dependency) ------------------------
def frame(cmd, payload=b"", sub=0x80):
    """AB 00 LEN FF CMD SUB [payload] — LEN counts from FF onward."""
    body = bytes([0xFF, cmd, sub]) + payload
    return bytes([0xAB, 0x00, len(body)]) + body


def date_bytes(dt):
    """year-2000, month, day, hour, minute — as the app encodes dates."""
    return bytes([dt.year - 2000, dt.month, dt.day, dt.hour, dt.minute])


CMD = {
    "vibrate":  lambda: frame(0x71),                       # find band
    "mtu":      lambda: frame(0xA7, bytes([0x00])),
    "serial":   lambda: frame(0xA8),
}


def req_activity(start, end):
    """0x51 — steps / heart rate over a date range."""
    return frame(0x51, bytes([0x00]) + date_bytes(start) + date_bytes(end))


def req_sleep(day):
    """0x52 — sleep data for one day."""
    return frame(0x52, bytes([0x00]) + date_bytes(day))


def phone_info():
    """0x20 — 'a real client is connected'. The app sends this first."""
    return frame(0x20, bytes([0x01, 0x00, 0x00]))


def time_sync(dt):
    """0x93 — set the watch clock. Year is two bytes, big-endian."""
    return frame(0x93, bytes([0x00,
                              (dt.year >> 8) & 0xFF, dt.year & 0xFF,
                              dt.month, dt.day,
                              dt.hour, dt.minute, dt.second]))


# ---- response decoding (pure, no BLE dependency) --------------------------
def decode(data):
    """Decode what we understand so far; log the rest raw for later."""
    if len(data) < 5 or data[0] != 0xAB:
        return f"non-AB packet: {hx(data)}"

    length, cmd = data[2], data[4]
    known = {
        0x91: "battery",
        0x92: "version info",
        0x9B: "status push (unknown)",
        0xD1: "activity data?",
        0xD2: "sleep data?",
    }
    label = known.get(cmd, f"unknown cmd 0x{cmd:02X}")

    if cmd == 0x91 and len(data) > 7:
        return f"BATTERY = {data[7]}%"

    if cmd == 0x92 and len(data) > 10:
        ota = data[6] + data[7] / 100.0
        band = ((data[9] & 0x7F) << 8) + data[10]
        return f"VERSION ota={ota:.2f} band={band}"

    # ---- activity record: steps + heart rate + calories, one per hour ----
    if cmd == 0x51 and len(data) >= 17:
        sub = data[5]
        if sub == 0x20:
            ts = f"20{data[6]:02d}-{data[7]:02d}-{data[8]:02d} {data[9]:02d}:00"
            steps = int.from_bytes(data[11:13], "big")
            cal = int.from_bytes(data[13:16], "big")
            hr = data[16]
            hr_txt = f"{hr} bpm" if hr else "--"
            return f"{ts}  steps={steps:<6} HR={hr_txt:<8} cal={cal}"
        if sub == 0x08:
            steps = int.from_bytes(data[7:9], "big")
            cal = int.from_bytes(data[9:12], "big")
            return f"DAY SUMMARY  steps={steps}  cal={cal}"

    # ---- sleep segment ----
    if cmd == 0x52 and len(data) >= 14:
        ts = (f"20{data[6]:02d}-{data[7]:02d}-{data[8]:02d} "
              f"{data[9]:02d}:{data[10]:02d}")
        stage = {1: "light", 2: "deep"}.get(data[11], f"stage{data[11]}")
        return f"SLEEP {ts}  {stage:<6} {data[13]} min"

    return f"{label}  len={length}  payload={hx(data[5:])}"


class Session:
    def __init__(self):
        self.count = 0
        self.hr = []        # (timestamp, bpm) for every non-zero reading

    def on_notify(self, _sender, data):
        data = bytes(data)
        self.count += 1
        log(f"RX  {hx(data)}")
        log(f"    -> {decode(data)}")
        # stash heart-rate readings out of the activity records
        if len(data) >= 17 and data[0] == 0xAB and data[4] == 0x51 \
                and data[5] == 0x20 and data[16]:
            ts = f"20{data[6]:02d}-{data[7]:02d}-{data[8]:02d} {data[9]:02d}:00"
            self.hr.append((ts, data[16]))

    def report(self):
        if not self.hr:
            log("no heart-rate readings in this batch")
            return
        bpm = [b for _, b in self.hr]
        log("")
        log(f"--- HEART RATE: {len(bpm)} readings ---")
        for ts, b in sorted(self.hr):
            log(f"  {ts}   {b} bpm")
        log(f"  min {min(bpm)} / avg {sum(bpm)//len(bpm)} / max {max(bpm)} bpm")


def device_by_path(address, adapter="hci0"):
    """
    Build a BLEDevice straight from the BlueZ object path, skipping discovery.
    Needed because BlueZ auto-reconnects trusted devices, and a connected
    peripheral stops advertising — so a scan will never find it.
    """
    from bleak.backends.device import BLEDevice
    path = f"/org/bluez/{adapter}/dev_" + address.upper().replace(":", "_")
    return BLEDevice(address, DEVICE_NAME, {"path": path, "props": {}})


async def resolve_address(address):
    """Return a connectable address, coping with rotating random addresses."""
    from bleak import BleakScanner
    log("scanning...")
    devices = await BleakScanner.discover(timeout=12.0)
    for d in devices:
        if address and d.address.upper() == address.upper():
            log(f"found by address: {d.address}")
            return d.address
    for d in devices:
        if d.name and DEVICE_NAME.lower() in d.name.lower():
            log(f"found by name at {d.address} (address rotated)")
            return d.address
    log("not found in scan — trying the configured address anyway")
    return address


async def main(args):
    from bleak import BleakClient

    address = args.address or ADDRESS
    if not address:
        log("No device address. Set MIBRO_ADDR or pass --address "
            "(find it with: bluetoothctl devices)")
        sys.exit(2)

    if args.path:
        target = device_by_path(address)
        log("using BlueZ object path directly (no scan)")
    else:
        target = await resolve_address(address)
    log(f"connecting to {address}")

    async with BleakClient(target) as client:
        log("connected")
        session = Session()
        await client.start_notify(UUID_NOTIFY, session.on_notify)
        log("subscribed — listening for unsolicited pushes (5s)")
        await asyncio.sleep(5)

        async def send(label, pkt):
            log(f"TX  {hx(pkt)}   ({label})")
            await client.write_gatt_char(UUID_WRITE, pkt, response=False)
            await asyncio.sleep(3)

        if args.init or args.steps or args.sleep or args.hr or args.mtu:
            await send("phone info / client hello", phone_info())
            await send("time sync", time_sync(datetime.datetime.now()))

        if args.vibrate:
            await send("find band / vibrate", CMD["vibrate"]())

        if args.mtu:
            await send("get MTU", CMD["mtu"]())
            await send("get serial", CMD["serial"]())

        if args.steps or args.hr:
            end = datetime.datetime.now()
            days = 0 if args.hr and not args.steps else 1
            start = (end - datetime.timedelta(days=days)).replace(
                hour=0, minute=0) if args.hr else end - datetime.timedelta(days=1)
            await send(f"activity {start:%Y-%m-%d %H:%M} .. {end:%H:%M}",
                       req_activity(start, end))
            await asyncio.sleep(8)

        if args.sleep:
            day = datetime.datetime.now() - datetime.timedelta(days=1)
            await send(f"sleep for {day:%Y-%m-%d}", req_sleep(day))
            await asyncio.sleep(8)

        if args.listen:
            log("idle listening for 60s...")
            await asyncio.sleep(60)

        log(f"done — {session.count} notifications received")
        if args.steps or args.hr:
            session.report()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--address", default="",
                   help="watch BLE address (else $MIBRO_ADDR)")
    p.add_argument("--vibrate", action="store_true",
                   help="make the watch buzz (safest proof of two-way control)")
    p.add_argument("--path", action="store_true", default=True,
                   help="attach via BlueZ object path instead of scanning")
    p.add_argument("--scan", dest="path", action="store_false",
                   help="force discovery by scanning instead")
    p.add_argument("--init", action="store_true",
                   help="send phone-info + time sync only")
    p.add_argument("--mtu", action="store_true", help="ask for MTU + serial")
    p.add_argument("--steps", action="store_true", help="request activity data")
    p.add_argument("--hr", action="store_true",
                   help="today's heart rate (same records as --steps)")
    p.add_argument("--sleep", action="store_true", help="request sleep data")
    p.add_argument("--listen", action="store_true", help="idle and log pushes")
    args = p.parse_args(argv)
    ignored = {"path", "address"}
    if not any(v for k, v in vars(args).items() if k not in ignored):
        args.listen = True
    return args


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
