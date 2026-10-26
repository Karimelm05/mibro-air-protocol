"""mibro — log a Mibro Air watch to SQLite and look at it.

  mibro setup AA:BB:CC:DD:EE:FF     save the watch address
  mibro sync                        pull new data from the watch
  mibro today | day 2026-09-17      hourly steps and heart rate
  mibro sleep [NIGHT]               last night's sleep, segment by segment
  mibro week                        last 7 days at a glance
  mibro serve [--open]              dashboard on http://127.0.0.1:8765
  mibro install-timer               sync automatically every hour (systemd)
  mibro import-log FILE --date D    import an old mibro_session.log
  mibro reparse                     rebuild parsed tables from raw packets
  mibro tail-check [DAY]            test the tail-bytes = deep-sleep hypothesis
  mibro vibrate                     buzz the watch
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as C
from . import protocol as P
from .store import Store


def _store(cfg) -> Store:
    return Store(cfg.db_path)


def _need_address(cfg):
    if not cfg.address:
        sys.exit("No watch address. Run:  mibro setup AA:BB:CC:DD:EE:FF\n"
                 "(find it with:  bluetoothctl devices)")


def _log(msg: str):
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def _bar(v, vmax, width=24, ch="█"):
    if not v or not vmax:
        return ""
    return ch * max(1, round(v / vmax * width))


def _fmt_min(m):
    return f"{m // 60}h{m % 60:02d}" if m else "—"


# --------------------------------------------------------------- commands --
def cmd_setup(cfg, a):
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", a.address):
        sys.exit(f"'{a.address}' doesn't look like a Bluetooth address")
    path = C.save_address(a.address, a.adapter)
    print(f"saved to {path}\ndata will go to {cfg.db_path}\n"
          f"next:  mibro sync   then   mibro install-timer")


def cmd_sync(cfg, a):
    _need_address(cfg)
    from .sync import BleLink, run_sync
    st = _store(cfg)
    link = BleLink(cfg.address, cfg.adapter, use_path=not a.scan)
    try:
        res = asyncio.run(run_sync(st, link, cfg.lock_path,
                                   backfill_days=a.days or cfg.backfill_days,
                                   vibrate=a.vibrate, log=_log))
    except RuntimeError as e:
        sys.exit(str(e))
    if not res["ok"]:
        sys.exit(1)


def cmd_vibrate(cfg, a):
    _need_address(cfg)
    from .sync import BleLink, single_instance

    async def go():
        async with BleLink(cfg.address, cfg.adapter) as l:
            await l.write(P.phone_info())
            await asyncio.sleep(1.5)
            await l.write(P.vibrate())
            await asyncio.sleep(1)
    with single_instance(cfg.lock_path):
        asyncio.run(go())
    print("buzzed")


def cmd_day(cfg, a):
    st = _store(cfg)
    day = a.day or dt.date.today().isoformat()
    hours = st.day_hours(day)
    t = st.day_totals(day)
    if not hours:
        print(f"no activity logged for {day}"
              + ("" if st.known_days() else " — run  mibro sync  first"))
        return
    print(f"\n  {day}   {t['steps']} steps · {t['calories']} kcal", end="")
    if t["hr_avg"]:
        print(f" · HR {t['hr_min']}/{t['hr_avg']}/{t['hr_max']} bpm "
              f"(min/avg/max, {t['hr_readings']} readings)")
    else:
        print()
    smax = max(h["steps"] for h in hours)
    print(f"\n  {'hour':5}  {'steps':>6}  {'HR':>4}")
    for h in hours:
        hr = f"{h['hr']}" if h["hr"] else "·"
        print(f"  {h['hour']}  {h['steps']:>6}  {hr:>4}  {_bar(h['steps'], smax)}")
    print()


def cmd_sleep(cfg, a):
    st = _store(cfg)
    night = a.night or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    n = st.night(night)
    if not n["segments"]:
        print(f"no sleep logged for the night of {night}")
        return
    print(f"\n  night of {night}:  {_fmt_min(n['asleep_min'])} asleep "
          f"({n['start'][11:16]} → {n['end'][11:16]}),  "
          f"deep {_fmt_min(n['deep_min'])}, light {_fmt_min(n['light_min'])}")
    print("  (total is the union of segments — deep overlaps light)\n")
    for s in n["items"]:
        print(f"  {s['start'][11:16]}–{s['end'][11:16]}  {s['stage']:<5} "
              f"{s['minutes']:>3} min  {_bar(s['minutes'], 90, 30, '▓' if s['stage'] == 'deep' else '░')}")
    print()


def cmd_week(cfg, a):
    st = _store(cfg)
    rows = st.overview(a.days)
    smax = max((r["steps"] for r in rows), default=0)
    print(f"\n  {'day':10}  {'steps':>6}  {'kcal':>5}  {'HR avg':>6}  {'sleep':>6}")
    for r in rows:
        d = dt.date.fromisoformat(r["day"])
        print(f"  {d:%a %d %b}  {r['steps'] or '·':>6}  {r['calories'] or '·':>5}  "
              f"{r['hr_avg'] or '·':>6}  {_fmt_min(r['sleep_min']):>6}  "
              f"{_bar(r['steps'], smax, 20)}")
    print()


def cmd_status(cfg, a):
    st = _store(cfg)
    print(f"watch    {cfg.address or '(not set — mibro setup ADDR)'}")
    print(f"database {cfg.db_path}")
    dev = st.device()
    if dev:
        print(f"firmware ota {dev.get('ota_version')} · band {dev.get('band_version')}")
    bat = st.battery_series(1)
    if bat:
        print(f"battery  {bat[-1]['percent']}% at {bat[-1]['ts']}")
    s = st.syncs(5)
    if s:
        print("\nrecent syncs")
        for r in s:
            extra = f"  {r['error']}" if r["error"] else ""
            print(f"  #{r['id']:<4} {r['started']}  {r['source']:6} {r['status']:7} "
                  f"{r['rx_count'] or 0:>4} packets{extra}")
    else:
        print("\nno syncs yet — run  mibro sync")


def cmd_reparse(cfg, a):
    st = _store(cfg)
    c = st.reparse()
    print(f"rebuilt from raw packets: {c}")


LOG_LINE = re.compile(r"^(\d\d:\d\d:\d\d)\s+(RX|TX)\s+((?:[0-9A-Fa-f]{2}\s?)+)")


def cmd_import_log(cfg, a):
    """Import mibro_session.log files written by mibro_lab2.py. Each
    'connecting to' line starts a new session; the clock going backwards
    moves to the next day."""
    path = Path(a.file)
    day = dt.date.fromisoformat(a.date) if a.date else \
        dt.date.fromtimestamp(path.stat().st_mtime)
    sessions, cur, prev = [], [], None
    for line in path.read_text(errors="replace").splitlines():
        if "connecting to" in line and cur:
            sessions.append(cur)
            cur = []
        m = LOG_LINE.match(line)
        if not m:
            continue
        hms = m.group(1)
        if prev and hms < prev and (int(prev[:2]) - int(hms[:2])) >= 6:
            day += dt.timedelta(days=1)
        prev = hms
        cur.append((f"{day} {hms}", m.group(2).lower(), P.unhx(m.group(3).strip())))
    if cur:
        sessions.append(cur)
    st = _store(cfg)
    for s in sessions:
        sid = st.import_lines(s, s[0][0])
        c = st.ingest(sid)
        print(f"session from {s[0][0]}: {len(s)} packets → sync #{sid}  {c}")
    st.close()
    if not sessions:
        print("no RX/TX lines found")


def cmd_tail_check(cfg, a):
    """README §V.6 hypothesis: activity tail bytes 3-4 = cumulative deep-sleep
    minutes. Compare against deep minutes elapsed from the sleep log."""
    st = _store(cfg)
    day = a.day or dt.date.today().isoformat()
    night = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
    deep = [(dt.datetime.fromisoformat(s["start"]), dt.datetime.fromisoformat(s["end"]))
            for s in st.night(night)["items"] if s["stage"] == "deep"]
    rows = st.tail_rows(day)
    if not rows or not deep:
        print(f"need both activity for {day} and sleep for the night of {night}")
        return
    print(f"\n  {day}: tail bytes 3-4 vs deep minutes elapsed by that hour\n")
    match = total = 0
    for r in rows:
        tail = P.unhx(r["tail_hex"] or "")
        if len(tail) < 5:
            continue
        h = dt.datetime.fromisoformat(r["hour"])
        if h.hour > 12:
            break
        tv = int.from_bytes(tail[3:5], "big")
        el = sum(max(0, (min(e, h) - s).total_seconds() // 60) for s, e in deep)
        ok = tv == el
        match += ok
        total += 1
        print(f"  {r['hour'][11:16]}  tail={tv:>4}  deep={int(el):>4}  "
              f"{'✓' if ok else '✗'}   tail[5]={tail[5] if len(tail) > 5 else '-'}")
    print(f"\n  {match}/{total} match\n")


UNITS = {
    "mibro-sync.service": """[Unit]
Description=Sync Mibro Air watch to the local activity log
After=bluetooth.target

[Service]
Type=oneshot
ExecStart={exe} -m mibro sync
TimeoutStartSec=300
""",
    "mibro-sync.timer": """[Unit]
Description=Sync Mibro Air every hour

[Timer]
OnBootSec=3min
OnUnitActiveSec={every}
Persistent=true
RandomizedDelaySec=2min

[Install]
WantedBy=timers.target
""",
    "mibro-web.service": """[Unit]
Description=Mibro Air dashboard on http://127.0.0.1:{port}

[Service]
ExecStart={exe} -m mibro serve
Restart=on-failure

[Install]
WantedBy=default.target
""",
}


def cmd_install_timer(cfg, a):
    _need_address(cfg)
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") \
        / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    names = ["mibro-sync.service", "mibro-sync.timer"] + (["mibro-web.service"] if a.web else [])
    for n in names:
        (unit_dir / n).write_text(UNITS[n].format(exe=sys.executable,
                                                  every=a.every, port=cfg.port))
        print(f"wrote {unit_dir / n}")
    if not shutil.which("systemctl"):
        print("systemctl not found — enable the units yourself")
        return
    cmds = [["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "mibro-sync.timer"]]
    if a.web:
        cmds.append(["systemctl", "--user", "enable", "--now", "mibro-web.service"])
    for c in cmds:
        print("$ " + " ".join(c))
        subprocess.run(c, check=False)
    print("\ncheck it with:  systemctl --user list-timers mibro-sync.timer\n"
          "logs:           journalctl --user -u mibro-sync -e")


def cmd_serve(cfg, a):
    from .web import serve
    serve(cfg, port=a.port or cfg.port, open_browser=a.open)


# ------------------------------------------------------------------- main --
def main(argv=None):
    p = argparse.ArgumentParser(prog="mibro", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", help="save the watch address")
    s.add_argument("address")
    s.add_argument("--adapter", default="hci0")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("sync", help="pull new data from the watch")
    s.add_argument("--days", type=int, default=0,
                   help="first-sync backfill in days (default 1; >1 untested on hardware)")
    s.add_argument("--vibrate", action="store_true", help="buzz when connected")
    s.add_argument("--scan", action="store_true", help="find by scanning, not BlueZ path")
    s.set_defaults(fn=cmd_sync)

    sub.add_parser("today", help="today, hour by hour").set_defaults(fn=cmd_day, day=None)
    s = sub.add_parser("day", help="one day, hour by hour")
    s.add_argument("day", nargs="?", help="YYYY-MM-DD")
    s.set_defaults(fn=cmd_day)

    s = sub.add_parser("sleep", help="one night's sleep")
    s.add_argument("night", nargs="?", help="evening date, YYYY-MM-DD (default: last night)")
    s.set_defaults(fn=cmd_sleep)

    s = sub.add_parser("week", help="daily totals")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(fn=cmd_week)

    s = sub.add_parser("serve", help="local dashboard")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--open", action="store_true", help="open in the browser")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("install-timer", help="hourly background sync via systemd --user")
    s.add_argument("--every", default="1h", help="systemd time span (default 1h)")
    s.add_argument("--web", action="store_true", help="also keep the dashboard running")
    s.set_defaults(fn=cmd_install_timer)

    s = sub.add_parser("import-log", help="import a mibro_session.log")
    s.add_argument("file")
    s.add_argument("--date", help="date of the first session (default: file mtime)")
    s.set_defaults(fn=cmd_import_log)

    sub.add_parser("reparse", help="rebuild parsed tables from raw packets").set_defaults(fn=cmd_reparse)

    s = sub.add_parser("tail-check", help="test the tail-bytes hypothesis")
    s.add_argument("day", nargs="?")
    s.set_defaults(fn=cmd_tail_check)

    sub.add_parser("vibrate", help="buzz the watch").set_defaults(fn=cmd_vibrate)
    sub.add_parser("status", help="watch, battery, recent syncs").set_defaults(fn=cmd_status)

    a = p.parse_args(argv)
    cfg = C.load()
    (getattr(a, "fn", None) or cmd_status)(cfg, a)


if __name__ == "__main__":
    main()
