# Testing and validation

What was actually run against the hardware, what each run established, and where the
results contradicted earlier assumptions. Transcripts are trimmed to the meaningful
lines; timestamps are as logged.

Two layers of testing:

- **Offline** — `tests/test_protocol.py`, 15 unit tests over the pure functions,
  needs no watch.
- **On-device** — the sessions below, run against a paired Mibro Air on Arch Linux.

---

## 1. Offline tests

`frame()`, `date_bytes()` and `decode()` have no Bluetooth dependency, so captured
hex can be replayed against them with no device present.

```bash
$ python3 -m unittest discover tests -v
...............
----------------------------------------------------------------------
Ran 15 tests in 0.001s

OK
```

Every packet in those tests is real traffic from the watch, or a request
byte-for-byte identical to one confirmed to work on it. The docstrings say which.
Two are worth calling out:

- `test_unknown_packets_are_never_discarded` — the undecoded `0x9B` push must keep
  its raw payload in the output.
- `test_second_fragment_is_flagged_not_dropped` — the tail of a split record must be
  reported as non-`AB`, not mis-parsed as a frame.

---

## 2. Getting connected

Three failed runs before anything worked.

```
2026-09-17T13:48:37 === connecting to D4:73:XX:XX:XX:XX ===
2026-09-17T13:49:07 ERROR: Device with address D4:73:XX:XX:XX:XX was not found.
```

Meanwhile BlueZ was perfectly happy:

```
$ bluetoothctl info D4:73:XX:XX:XX:XX | grep Connected
        Connected: yes
```

Disconnecting first made no difference — BlueZ reconnected within seconds. The cause
is that a **connected** BLE peripheral stops advertising, so `bleak`'s scan-based
discovery can never see it. Fixed by attaching via the BlueZ D-Bus object path
instead of scanning (README §IV.4).

One hiccup while implementing that fix, worth recording because the error message is
unhelpful:

```
15:33:00 ERROR: BLEDevice.__init__() takes 4 positional arguments but 5 were given
```

`bleak` 3.x dropped the trailing RSSI argument that older versions took. Removing it
resolved it.

---

## 3. Connection greeting — battery and version

Every session opens the same way. The watch announces itself the moment a client
subscribes, with no request sent:

```
15:35:36 connected
15:35:36 subscribed — listening for unsolicited pushes (5s)
15:35:36 RX  AB 00 05 FF 91 80 00 4B
15:35:36     -> BATTERY = 75%
15:35:37 RX  AB 00 11 FF 92 C0 02 0A 00 81 3D 00 00 00 00 28 00 62 07 80
15:35:37     -> VERSION ota=2.10 band=317
15:35:37 RX  AB 00 11 FF 9B 00 0F 01 00 00 00 00 00 00 00 00 00 00 00 00
15:35:37     -> status push (unknown)  len=17  payload=00 0F 01 00 ...
```

**Establishes:** no authentication is required on this model, and the `0xAB` frame
format read from the decompiled source is correct against live bytes.

Battery tracked the watch face across sessions — 76%, then 75%, then 74% over
several hours. `0x9B` remains undecoded; its payload is preserved in the log.

---

## 4. Vibration — two-way control

```
15:57:57 TX  AB 00 03 FF 71 80   (find band / vibrate)
15:58:00 done — 3 notifications received
```

The watch vibrated. No response packet is sent, hence only the three greeting
notifications.

**Establishes:** writes to `6e400002` reach the device and are acted on. Bytes built
from decompiled constants drive physical hardware.

---

## 5. Activity records — the first full decode

Before the decoder understood subtype `0x20`, the same run produced only this:

```
15:35:50 RX  AB 00 16 FF 51 20 1A 09 10 00 00 00 00 00 00 00 46 00 00 00
15:35:50     -> unknown cmd 0x51  len=22  payload=20 1A 09 10 00 ...
```

After implementing the field offsets:

```
16:00:35 TX  AB 00 0E FF 51 80 00 1A 09 11 00 00 1A 09 11 10 00   (activity 2026-09-17 00:00 .. 16:00)
16:00:35 RX  AB 00 0E FF 51 08 00 12 B6 00 01 0B 00 00 00 00 00
16:00:35     -> DAY SUMMARY  steps=4790  cal=267
16:00:39     -> 2026-09-17 01:00  steps=57     HR=--       cal=3
16:00:39     -> 2026-09-17 02:00  steps=57     HR=74 bpm   cal=3
...
16:00:40     -> 2026-09-17 15:00  steps=4790   HR=72 bpm   cal=267
```

**The decisive check:** the day summary arrives *first*, from a different subtype
(`0x08`), and reports 4790 steps / 267 cal. The final hourly record, parsed with
entirely different byte offsets, reports **4790 steps / 267 cal**. Two independent
paths through the data agree exactly.

That is the strongest evidence in the whole project that the activity field offsets
are right, and it is why they are graded *high* rather than *hypothesis*.

A later run the same evening confirms the records accumulate as expected:

```
22:27:36     -> DAY SUMMARY  steps=9630  cal=537
22:27:41     -> 2026-09-17 22:00  steps=9591   HR=76 bpm   cal=535
...
22:27:47 --- HEART RATE: 21 readings ---
22:27:47   min 52 / avg 77 / max 100 bpm
```

Values are cumulative within the day — 57 steps at 01:00 rising to 9591 at 22:00 —
and the 06:00 minimum of 52 bpm during sleep is physiologically sensible.

Re-running six minutes later returned identical values for every past hour, with
only the summary advancing (9630 → 9683). **The records are stable stored history,
not recomputed on each request.**

---

## 6. Sleep records — and where the documentation was wrong

```
23:21:52 TX  AB 00 09 FF 52 80 00 1A 09 10 17 15   (sleep for 2026-09-16)
23:21:53     -> SLEEP 2026-09-16 23:14  light  44 min
23:21:53     -> SLEEP 2026-09-17 00:46  deep   8 min
23:21:54     -> SLEEP 2026-09-17 00:02  light  53 min
23:21:54     -> SLEEP 2026-09-17 01:42  deep   6 min
23:21:54     -> SLEEP 2026-09-17 02:22  deep   3 min
23:21:54     -> SLEEP 2026-09-17 02:59  deep   24 min
23:21:54     -> SLEEP 2026-09-17 03:57  deep   3 min
23:21:54     -> SLEEP 2026-09-17 04:34  deep   13 min
23:21:54     -> SLEEP 2026-09-17 00:59  light  1 min
23:21:54     -> SLEEP 2026-09-17 05:21  light  30 min
23:21:54     -> SLEEP 2026-09-17 06:36  deep   26 min
23:21:54     -> SLEEP 2026-09-17 05:52  light  70 min
```

Note the ordering: 23:14, then 00:46, then 00:02. **Records arrive in storage order,
not chronological order.** A client that assumes chronology will build nonsense.

**The correction.** Sorting these and laying them on a timeline shows two deep
segments sitting entirely inside light segments:

```
00:02 light 53 min  →  00:02–00:55
00:46 deep   8 min  →  00:46–00:54    contained
05:52 light 70 min  →  05:52–07:02
06:36 deep  26 min  →  06:36–07:02    contained
```

An earlier draft of the documentation reported "twelve segments totalling 281
minutes" — the sum of all durations. That figure is wrong as a sleep total. The
union of the intervals is **247 minutes (4h 07m)** across 23:14 → 07:02.

Light and deep are therefore not mutually exclusive in this encoding. Any consumer
must compute the union, not the sum.

---

## 7. The trailing fragment — from noise to a lead

Each 26-byte activity record splits across two notifications. The tail was logged
and ignored:

```
16:00:39 RX  AB 00 16 FF 51 20 1A 09 11 02 00 00 39 00 00 03 4A 00 00 00
16:00:39     -> 2026-09-17 02:00  steps=57  HR=74 bpm  cal=3
16:00:39 RX  00 01 2B 00 0E 01
16:00:39     -> non-AB packet: 00 01 2B 00 0E 01
```

Collecting those tails across one day and comparing bytes 3–4 (big-endian) against
the sleep data fetched separately:

| Hour | Tail bytes 3–4 | Cumulative deep minutes |
|---|---|---|
| 01:00 | `00 08` = 8 | 8 |
| 02:00 | `00 0E` = 14 | 14 |
| 03:00 | `00 14` = 20 | 18 |
| 04:00 | `00 2C` = 44 | 44 |
| 05:00 | `00 39` = 57 | 57 |
| 06:00 | `00 39` = 57 | 57 |

Five of six match exactly. The 03:00 row is off by two; at 07:00 the field jumps to
279 and then holds.

Byte 5 moves in a separate pattern — `01 01 01 01 01 03 03 04 04` — which looks like
a state or mode index rather than a counter.

**Status: hypothesis from one night.** To confirm, capture `--hr` and `--sleep` for
the same date across several nights and check whether the relationship holds. This
is the main open thread.

The lesson is about the logging policy rather than the protocol: these bytes were
visible for days before anyone looked at them. Had `decode()` dropped what it could
not parse, the lead would not exist.

---

## 8. An invalid negative result

`--mtu` was run twice and produced nothing:

```
15:57:12 TX  AB 00 04 FF A7 80 00   (get MTU)
15:57:15 TX  AB 00 03 FF A8 80   (get serial)
15:57:18 done — 3 notifications received
```

Three notifications: the greeting, and nothing else.

**This result should not be trusted.** The condition guarding the opening sequence
in the script at the time was:

```python
if args.init or args.steps or args.sleep or args.hr:
```

`--mtu` is absent from it. So `0xA7` and `0xA8` were sent *before* the client hello
— the exact state in which §IV.5 of the README establishes that the watch ignores
everything. The null result says nothing about those commands; it only re-confirms
the opening-sequence requirement.

The condition has since been corrected to include `args.mtu`. Both commands remain
marked **unconfirmed** in the reference until re-tested.

Recorded rather than quietly deleted, because a negative result obtained under a
broken precondition is a specific and common way to be wrong, and the fix is
invisible unless the mistake is written down.

---

## 9. Summary of on-device sessions

| Run | Result |
|---|---|
| `--vibrate` | Watch vibrated. Two-way control confirmed. |
| `--steps` | 82 notifications. Activity records retrieved and parsed. |
| `--hr` (16:00) | 14 HR readings; day summary cross-checked the offsets. |
| `--hr` (22:27) | 21 readings, min 52 / avg 77 / max 100 bpm. |
| `--hr` (22:33) | Identical past hours — records are stable history. |
| `--sleep` | 12 segments, 23:14 → 07:02, overlap discovered. |
| `--listen` | Greeting only; no unsolicited pushes over 60 s idle. |
| `--mtu` | No response — **invalid, see §8**. |

## 10. Still to test

1. `--init --mtu`, to obtain a valid result for `0xA7` and `0xA8`.
2. Same-day `--hr` plus `--sleep` over three or more nights, to confirm or kill the
   tail-byte hypothesis.
3. Reassembly of the two fragments before parsing, so the full 26-byte record is
   available.
4. `0x96` continuous heart rate — read from source, never sent. Low priority: it
   costs battery and blocks nothing.