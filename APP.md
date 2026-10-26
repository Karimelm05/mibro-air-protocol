# `mibro` — the laptop app

Phase two of this project: the protocol from [README.md](README.md), turned into
something that runs on its own. Every hour it connects to the watch, pulls whatever
is new, and appends it to a local SQLite log. A dashboard on `127.0.0.1` shows the
result. No account, no cloud, no network traffic beyond Bluetooth.

## Install (Arch)

```bash
sudo pacman -S --needed bluez bluez-utils python
sudo systemctl enable --now bluetooth
bluetoothctl                       # pair + trust the watch once, then `devices`

git clone https://github.com/Karimelm05/mibro-air-protocol && cd mibro-air-protocol
python -m venv ~/.local/share/mibro/venv
~/.local/share/mibro/venv/bin/pip install -e .
ln -s ~/.local/share/mibro/venv/bin/mibro ~/.local/bin/mibro   # or add to PATH
```

## First run

```bash
mibro setup D4:73:XX:XX:XX:XX      # from `bluetoothctl devices`
mibro sync --vibrate               # buzzes when connected, then pulls data
mibro today                        # hour by hour
mibro sleep                        # last night
mibro install-timer --web          # hourly sync + dashboard, both via systemd --user
xdg-open http://127.0.0.1:8765
```

`install-timer` writes `mibro-sync.service` / `.timer` (and `mibro-web.service`
with `--web`) to `~/.config/systemd/user/` and enables them. Check on it with
`systemctl --user list-timers mibro-sync.timer` and `journalctl --user -u mibro-sync -e`.
`--every 30min` changes the interval.

Already have `mibro_session.log` files from the lab script? Bring them in:

```bash
mibro import-log mibro_session.log --date 2026-09-17
```

## Commands

| Command | Does |
|---|---|
| `mibro sync` | connect, send client hello + time sync, request activity and sleep, store, ingest |
| `mibro today` / `mibro day 2026-09-17` | hourly steps (de-cumulated) and heart rate |
| `mibro sleep [NIGHT]` | one night's segments; total is the **union**, not the sum |
| `mibro week [--days N]` | daily totals with the night before each day |
| `mibro serve [--open]` | the dashboard |
| `mibro status` | battery, firmware, last syncs (also what `mibro` alone prints) |
| `mibro reparse` | drop every parsed table and rebuild it from raw packets |
| `mibro tail-check [DAY]` | tests the §V.6 hypothesis against that day's logged sleep |
| `mibro vibrate` | find the watch |

## How a sync works

```
subscribe 6e400003 → greeting (battery, version, 0x9B)  ── wait until quiet
0x20 client hello → 0x93 time sync                      ── README §IV.5
0x51 activity  from 00:00 of the last day already stored → now
0x52 sleep     one request per night since the last good sync (≥ last night)
                                                        ── each waits until the watch goes quiet
ingest: reassemble fragments → parse → upsert
```

- **Incremental.** The first sync fetches yesterday 00:00 → now (the shape confirmed
  in TESTING.md §5). After that it re-fetches only from the start of the last day it
  holds, so the in-progress hour gets refreshed. Records are stable history, so the
  overlap is harmless.
- **Ends on silence, not a fixed sleep.** Each request waits until no notification
  has arrived for 2.5 s (6 s if nothing arrives at all, 40 s cap).
- **One at a time.** A lock file stops the timer and a manual sync talking to the
  watch together.
- **Crash-safe.** Every packet is written to `raw_packets` the moment it arrives.
  A sync that dies halfway is marked `error` and whatever did arrive is still parsed.

## Storage

`~/.local/share/mibro/mibro.db` (override with `$MIBRO_DB` or `db_path` in
`~/.config/mibro/config.toml`).

| Table | Holds |
|---|---|
| `syncs` | one row per sync or imported log: when, result, packet count |
| `raw_packets` | every byte in both directions, in order — **the source of truth** |
| `activity_hourly` | per hour: cumulative steps and kcal, HR (NULL = no reading), the raw tail |
| `day_summary` | the `0x51/0x08` header, kept only for the day it was received |
| `sleep_segments` | start, stage, minutes, flag, and the noon-to-noon night it belongs to |
| `battery`, `device_info` | from the connection greeting |
| `unparsed` | every frame the decoder didn't recognise (e.g. `0x9B`) |

Everything except `syncs` and `raw_packets` is derived. When the decoder learns
something new, `mibro reparse` rebuilds it all from the raw bytes — the same "decode,
but never discard" rule as the lab script, applied to a database.

The `.gitignore` already excludes `*.db` and `*.sqlite`; the database never belongs in
the repo.

## Two protocol details the app had to settle

**Fragment reassembly.** A frame that fills the 20-byte MTU is held until the next
notification arrives; a following packet that does not start with `0xAB` is appended
to it. A frame shorter than its own `LEN` absorbs the next packet regardless of its
first byte. The joined frame is **not** truncated to `LEN`: the activity record says
`LEN = 0x16` (25 bytes total) but 26 arrive, and the 26th byte is the one that moves
in steps (`01 01 01 01 01 03 03 04 04`, §V.6). Either `LEN` is off by one for this record
or the tail carries an extra byte; the app keeps all 26 either way.

**Which day the summary belongs to.** The `0x51/0x08` header carries no date. It
matched today's final hour when the request ended today, so it is stored only in that
case. For any other request its day is unknown, and it stays in `raw_packets` only.

## Dashboard

`mibro serve` binds to `127.0.0.1:8765` only. It checks the `Host` header (so a
malicious page can't reach it through DNS rebinding) and the one action that changes
anything, **Sync now**, needs a custom header that a cross-site request can't send.
The page loads nothing from the network.

**Live refresh.** The page asks `/api/version` every 4 seconds. That returns a
fingerprint of the database (latest sync, row counts, latest battery reading), which
changes whenever *any* process adds data — the hourly timer, or `mibro sync` in a
terminal. When it changes the page redraws in place, keeping its scroll position.
Nothing is polled while the window is hidden, and if midnight passes while you are
looking at "today" the view moves on to the new day.

It shows a 30-hour ribbon for the selected day — the evening before through midnight —
with steps and heart rate above the axis and sleep stages below, then the last 30
days, the night's segments, and sync history. Click any day in the 30-day strip, or
use ← and →.

## Not yet confirmed on hardware

- Backfill longer than 2 days (`mibro sync --days N`): the request shape is the same,
  but nobody has checked how far back the watch keeps records.
- Sleep requests for nights older than last night (`now − k days` for k > 1).
- Whether the watch tolerates an hourly sync overnight without noticeable battery
  cost. The `battery` table will show it after a few days.

## Tests

```bash
python3 -m unittest discover tests -v      # 41 tests, no watch, no bleak
```

`tests/test_app.py` drives a complete sync against a fake watch that replays captured
traffic — including ignoring every request sent before the client hello, as the real
one does — and checks the captured night still comes out at 247 minutes after going
through the database.
