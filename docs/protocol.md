# Ariston Bridgenet eBUS: protocol notes and findings

What we learned decoding the bus of an Ariston Nimbus Plus 110 M T NET system (heat pump, indoor
energy manager, Sensys NET thermostat, Light Gateway, Multifunctional Kit) from weeks of passive
captures, the Sensys menus, the Ariston cloud and attended tests. Statements are marked
**confirmed** (Sensys display, cloud, manual, independent measurement or a reproduced action) or
*hypothesis*; everything unmarked is directly visible in the captures. Per-register evidence is in
`registers/registers.yaml`.

## Framing

- Raw lines (ebusd `--lograwdata`, message mode): `2026-04-09 23:29:17.477 <7f18200102dc283b00030500...`,
  timestamp, `<`, the escaped wire bytes. `>` marks bytes ebusd itself sent.
- **Unescape first**: `a9 00` means `a9`, `a9 01` means `aa` (SYN).
- Layout: `QQ ZZ PB SB NN data CRC`, then for a slave target `ACK NN data CRC ACK`. Broadcast target
  is `fe`; a master target gets only an ACK.
- CRC-8, polynomial `0x9b`, initial value 0, over the **escaped** byte sequence.
- Lines shorter than a telegram (a lone `10`, `30`, `77`) are collision bytes: the wired AND of two
  masters that started in the same SYN slot (`10` = `13` AND `70`, `30` = `31` AND `70`, `77` = `7f`
  AND `f7`). They are not devices.
- Worked example (a `2001` read of `dc28`): `70 18 2001 02 dc28 | crc | ack | 03 xx yy zz | crc | ack`
  (reply: value, min, max).

## Addresses

| Device | Master | Slave |
|---|---|---|
| Energy manager (indoor unit) | `13` | `18` |
| Heat pump (outdoor unit) | `03` | `1e` (not master + 5) |
| Sensys thermostat | `70` | `75` |
| Light Gateway | `7f` | `84` |
| Multifunctional Kit | `f7` | `fc` |
| ebusd | `31` | `36` |
| bus scanner | `00` | |

Upstream configurations label `70` as the gateway and `7f` as the Sensys. On this system it is the
other way round: `70` reads the Wi-Fi status registers from `84`, and `7f` announces `84` as its slave.

## Commands (PBSB)

| PBSB | Use | Data |
|---|---|---|
| `2000` | read values | request: N register ids (2 bytes each). Reply: a mask byte, then the values. A full reply has mask `2^N - 1`; mask `00` without values = none available. Only full and `00` masks have been seen (*hypothesis*: a per-register bitmap) |
| `2001` | read one value with limits | request: one id. Reply: value, min, max (no mask) |
| `200e` | broadcast answer to a `2001` sent to `fe` | id, value, min, max |
| `200f`, `2010` | value broadcasts | repeated: id, value, one flag byte (always `00` so far) |
| `2020` | to `fe`: settings broadcast; to a slave: write (empty reply) | repeated: id, value |
| `2004` | error broadcast, about hourly from the energy manager | error code, then two bytes (open). `69` = 105 = error 411 "room sensor not available" (**confirmed** against the Sensys error history) |
| `2031` | serial number broadcast | 12-character string |
| `2038` | bus control? | 5 bytes, not decoded |
| `203a` / `203b` | bus scan by `00` / reply with the own slave address | **never poll or send.** Polling `203b` triggered a full address scan that crashed and rebooted another user's boiler |
| `2050`, `2051` | time programs, cycled by the Sensys | `2051`: byte 0 = day 0-6 for 00:00-12:00 or 16 + day for 12:00-24:00, byte 1 = channel, 12 bytes of 2-bit levels per quarter hour, high bits first (level 1 = comfort, 0 = reduced; **confirmed** against the cloud). `2050` not understood |
| `2070` | date and time from the Sensys, every ~20 s | minute, hour, day, month, weekday (1 = Monday), year − 2000, constant `0x11`; plain binary |
| `2071` | 7 zero bytes from the Sensys every 5 min | |
| `2073` | zone override end time (**confirmed** by behaviour) | 9 bytes: temperature ×10 (2 bytes LE), end minute, hour, day, month, year (2 digits), active flag, zone |
| `2002` | per upstream the error history query | rare |

`2000` and `2001` sent to `fe` are broadcast requests; the owner of the register answers with `200f`
or `200e`.

## Register ids

- Two bytes, written in wire order (`6e13`, `c94b`).
- **The first byte encodes the value width:**

  | First byte | Width |
  |---|---|
  | `00-1f`, `c0-df` | 1 byte |
  | `40-5f` | 2 bytes per id; some pairs form 32-bit counters (low word at the even id), unsigned |
  | `60-7f` | 2 bytes |
  | `20-3f` | not seen (*hypothesis*: 1 byte, as upstream types the Multifunction Kit settings) |
  | `80-bf`, `e0-ff` | not seen |

- 16-bit values are little-endian and signed; temperatures in tenths of a degree. `0x7fff` = not
  available; `0x8000`/`0x7fff` also appear as limits of unbounded values.
- Clock-time settings use one byte for minutes and one for hours (`00 14` = 20:00).
- **The second byte is a family; for zone settings it encodes the zone**: `71-77`, `79-7f`, `81-87`
  (and `90`, `97`) are zones 1-7. The low five bits of the first byte index within the family.
- **Register ids are per device**: `d140` returns different values from different devices. Always
  key a register by (device, id).

## Who talks, and how often

About 4.5 telegrams per second.

- **Energy manager (`13`)**: every 5 s a command block to the heat pump (`2020` to `1e`: `c33e`,
  `c446` frequency cap, `cd46` requested mode, `6a47` flow setpoint) and reads of flow, mode and
  frequency; every ~25 s heat pump temperatures and power, buffer temperatures, setpoints and
  per-zone values; every 120 s outside temperature and inverter capacity; **every 30 min a settings
  broadcast of all seven zones** (`2020` to `fe`, drifting a few seconds per half hour).
- **Sensys (`70`)**: reads the energy manager and the gateway about every 10 s, broadcasts the clock,
  the time programs and the room temperature (`7118`). It reads a circular list of energy-manager
  settings in chunks of up to four ids, and the chunk boundaries move now and then (after a power
  loss, or when the list changes), so a given setting arrives in changing request layouts.
- **Gateway (`7f`)**: broadcast requests that the energy manager answers, plus the reads the cloud
  asks for: a cloud read of a property makes the gateway fetch it on the bus within a second (the
  cloud does not answer from a cache). Without cloud clients, many settings are therefore never
  read.
- **Multifunctional Kit (`f7`)**: broadcasts `1191`-`1391` every 5 s.

The energy manager rotates registers through its broadcast frames, and a register sometimes sits
in another frame for minutes. A decoder that keys one layout per message (as ebusd does: PBSB plus
at most 6 data bytes) misses those updates unless it also decodes the other layouts.

## Heat pump control

State registers (values as seen; *hypotheses* unless marked):

| Register | Meaning, values |
|---|---|
| `c94b` (energy manager) | status: 0 idle, 2 heat pump charging, 22 heat request served from the buffer (before a charge and after it, while the request is on) |
| `cd46` (written to the heat pump) | requested mode: 0, 3 heating |
| `ca46` (heat pump) | actual mode: 15 idle, 3 heating, 11 timeguard pause, 1 wind-down |
| `d09c` | requested state: 0, 2 pump only, 10 compressor |
| `d84b` | sub-state: 19 idle, 12 heating, 11 pause, 13-15 transitions |
| `0120` | heating enabled (**confirmed**) |
| `c200` | circulator on/off |
| `0191` | zone 1 heat request |

**Charge cycle** (**confirmed** by behaviour):

1. With a zone heat request the house is first fed from the buffer (`c94b` 22).
2. A charge starts when the buffer high sensor `759e` is `672e` (here 5 K) below the flow target
   (`d09c` 2, then 10; compressor ~1 min later).
3. The heat pump ramps to 40-56 Hz, overshoots, modulates down to its 18 Hz minimum and still stops
   on a 2-3 K flow overshoot (`ca46` 11, 3-minute timeguard), then restarts.
4. The energy manager ends the charge when the buffer reaches the flow target (`d09c` 10 → 2),
   followed by 3 min of circulator run-on.

So one charge is usually two compressor starts. In this setup (setpoint mode Variable) the buffer is
not kept warm without a zone request: every compressor start comes from a heat request.

**Heating curve, zone 1** (fit 0.045 K, the rounding of the inputs):

    6197 = 20 + offset + slope × (Tset − Tout) + 10 × (Tset − Troom),   clipped to [min, max] flow

with offset `6471`, slope `6a71`/100, Tset `6996`, Tout `7647` (the energy manager's outdoor probe;
the heat pump's own air sensor `7810` fits worse) and Troom `7118`; room influence `c279` was 20, its
maximum. The room term dominates: a 0.3 K room error moves the target 3 K. `6047` (written to the
heat pump as `6a47`) follows `6197` while a request is on and is frozen when it ends.

**Quiet mode** (**confirmed**): `c446` (frequency cap) drops from 100 to 75 and `1ef3` goes 0 → 1 at
the start of the configured quiet period, and back at its end. The unit of `c446` (Hz or %) is open.

## Measurements

- `6e13` water flow: raw ÷ 10 = l/min (**confirmed**).
- `7c4f` (heat pump): outdoor unit electrical power in W, read by the energy manager every ~25 s
  (**confirmed**: r = 0.993 against an external meter of the whole system). While running, meter ≈
  0.944 × `7c4f` + 240 W; about 100-150 W (fan, electronics, indoor unit) are not in `7c4f`, so a COP
  from it reads high. `6347`, with the same upstream label, stays 0.
- Buffer (**confirmed** against the Sensys menu): `759e` high sensor; `769e` "low sensor" follows the
  heat pump return `7710` one cycle later (there is no low sensor in this buffer); `779e` = 0.
- Counters on the energy manager (**confirmed**): `40d8`/`41d8` compressor starts, `40d1` compressor
  hours, `55d1` heating hours, `47d1` defrost hours (plain counts; the Sensys shows hours /10),
  `52d9`/`53d9` outdoor unit electrical energy, 32-bit, in units of 10 kJ (matches the cloud's
  consumption series to the Wh; the cloud reports it in 2-hour slots).
- Heat pump diagnostics, not broadcast (read on request, `config/polled.csv`): evaporator and
  condenser pressure `6c4f`/`6d4f`, fan speeds `6e4f`/`6f4f`, compressor on `1145`, compressor
  preheating `1245`, 4-way valve `1345`, fan status `1445`/`1545`, base heater `1645`, inverter
  protection `1045`, condenser outlet temperature `7117`. Compressor preheating switches on during
  long or cold pauses and costs about 70 W.
- `6197`-`6797` are per-zone flow targets (upstream: hot water targets). `649f` duplicates `6047`.
  `6c96`-`6f96` are room setpoints of zones 4-7.
- Registers the energy manager requests by broadcast but nobody answers (`0119`-`0719`, `6997`-`6f97`,
  `7997`-`7f97`, `7618`, `7718`) belong to absent zone devices, except that the Sensys broadcasts
  `6997` (zone 1 override temperature) while an override runs.

## Settings and writes (zone 1)

Every action from the Ariston app or the Sensys is a **broadcast** (`2020` or `2073` to `fe`), never a
write to a slave; the energy manager adopts the value within 6-10 s. The energy manager also accepts
these broadcasts from ebusd (`31`) (**confirmed**: each reproduced action had the same effect).

| Action | Telegram(s) |
|---|---|
| zone mode Scheduled / Manual / Off | `2020 03 c679 03` / `02` / `00`; the app sends `2020 03 0990 00` (override off) before Manual or Off |
| Manual setpoint | the comfort temperature: `2020 04 6271 <temp ×10 LE>` (there is no separate manual setpoint) |
| plant mode | three enable flags: `0120` heating, `0220` DHW, `0f23` cooling (Off = all three 0) |
| timed override | `6997` temperature, `0990` = 1, and `2073` with the end time, sent together |
| cancel an override | `2020 03 0990 00`, then `2073` all zero for zone 1 |

- **The Sensys owns a running override.** It adopts the three telegrams at its next minute tick,
  re-broadcasts them every minute, and ends the override itself at the end time (any minute, not
  only quarter hours). The duration needs nothing from outside. `0990` = 0 alone does not cancel: the
  Sensys restores the override at its next tick; the zero `2073` for zone 1 is what ends it. The
  gateway's periodic zero `2073` covers zones 2-6 only.
- The cloud can set (Galevo plant): plant mode, zone mode (Manual, Time program), comfort and economy
  temperatures 10-30 °C in 0.5 steps, fixed flow temperature 20-45 °C, curve offset −7..7, automatic
  thermoregulation, quiet mode, holiday, DHW settings, cooling flow. Its own "override" property is a
  bare temperature without a duration.
- `c194` (energy manager, every ~26 s) follows the zone mode: 0 off, 1 time program, 2 manual
  (*hypothesis*).
- In time-program mode `6996` shows the active target, so the schedule's effect is visible without
  decoding the program.

## ebusd specifics (ebusd 26.1)

- `--scanconfig=none` does not disable scanning: the auto scan stays on and, once ebusd may write,
  sends `07 04` to every device it sees. Use `--scanconfig=off`. `off` also loads CSVs from
  subdirectories of the config path.
- Over MQTT ebusd acts on `<prefix>/<circuit>/<name>/get` (one read for an active message),
  `.../set` (one write), `.../list` and `<prefix>/list` (re-publish cached values), and a `?N` suffix
  (sets poll priority N, after which ebusd polls by itself). There is no scan request over MQTT. A
  broker ACL can restrict topics but not payloads, so requests must keep an empty payload.
- A failed MQTT read publishes nothing; a successful one always publishes, even if the value is
  unchanged. ebusd's `global/running` is its MQTT last will; `global/signal` turns false only when
  the adapter receives nothing, not when sending fails.
- State topics are not retained; after an HA restart, a publish to `ebusd/list` re-sends the cache
  without bus traffic.
- Poll priorities ignore message conditions: a conditional `r1` line is polled all the time.
- A frame whose layout differs from the definition keyed on the same PBSB and id gives "invalid
  position" in the log and is not decoded (never misread). Example: the energy manager's 5 s command
  block normally starts `c33e+c446+cd46+6a47`, for a few minutes at a time `c33e+c046+c446+cd46`.

## Bus behaviour

- The ESP adapter's default arbitration delay (200 µs) made ebusd start together with the Sensys
  (206-230 µs) and collide in lockstep. At 300 µs collisions with the Sensys fell elevenfold, with no
  cost to other masters.
- Reads spaced 5 s apart, started at a second where the Sensys is quiet, took under 1 % of the bus
  and did not measurably damage other devices' telegrams (0.03-0.09 % damaged with or without them).
- After a power loss the adapter can keep receiving but stop transmitting until it is restarted
  itself; restarting ebusd does not help. After power-ups the share of damaged telegrams can also be
  high for a few hours.

## Open questions

1. What makes up the ~240 W the meter reads above `7c4f` while the compressor runs?
2. Why does the flow reading fall by about one count every 5 s during steady heating?
3. Whether opening the app also triggers the energy manager's settings broadcast (besides the
   30-minute period).
4. The four 1-byte registers that read a constant 160 (`da46`, `dc46`, `de46`, `da9c`).
5. `c33e` counts down about 1/s and is refilled every ~30 s (keepalive?); `c74b` and `d84b` are
   sub-state codes whose exact meaning is open.
6. Whether the Multifunctional Kit's `1191`-`1391` ever change.
7. The hourly `2004` error 411: a live fault or a re-broadcast of a stored one? Bytes 2-3 (`01 00`).
8. Upstream width conflicts `762e`, `782d`, `7547` (1 byte upstream against the 2-byte rule) never
   occur on this bus.
9. Not yet observed: defrost, backup heater, DHW cycle, `2002` replies, partial `2000` masks.
