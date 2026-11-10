# ebusd-ariston-nimbus

ebusd configuration and Home Assistant packages for an **Ariston Nimbus Plus 110 M T NET** heat pump
system (Bridgenet eBUS) with a Sensys NET thermostat, plus a register database with evidence for
every field and the tooling used to decode the bus.

What you get in Home Assistant: heat pump and energy manager states, temperatures, compressor
frequency, power, counters and COP as sensors; a zone 1 climate entity (mode, setpoint, timed
override) that writes over the eBUS; and an alert when ebusd cannot reach the bus.

Status: in daily use on one installation since 2026-10-15 (ebusd 26.1 as a Home Assistant add-on).
Read "This installation" and "Before you enable reads and writes" before using it on yours.

## Contents

| Path | Content |
|---|---|
| `config/` | ebusd configuration: `ariston.csv` (passive messages), `polled.csv` (the reads ebusd may send), `writes.csv` (the writes ebusd may send), `_templates.csv`, `mqtt-hassio.cfg` (HA discovery). Generated, do not edit by hand |
| `ha/` | Home Assistant packages and a dashboard view (below) |
| `registers/` | register database (`registers.yaml`, keyed by device and register id, with status and evidence), the whitelists `polled.yaml` and `writes.yaml`, and the frame layouts seen on the bus |
| `src/ebusnimbus/`, `scripts/`, `tests/` | parser, capture and analysis tools, config generator, tests |
| `deploy/` | systemd units for the capture services |
| `docs/protocol.md` | protocol notes and findings: framing, addresses, commands, the register width rule, who talks when, heat pump control, the write telegrams |
| `reference/claytonn73/` | claytonn73's Nimbus configuration CSVs and templates (MIT), which the generator uses for field types |

## This installation

The configuration was decoded on, and tested against, one system. These parts assume it:

| Assumption | Here | Where it matters |
|---|---|---|
| Devices and master addresses | energy manager `13`/`18`, heat pump `03`/`1e`, Sensys `70`/`75`, Light Gateway `7f`/`84`, Multifunctional Kit `f7`/`fc`, ebusd `31`/`36` | every `QQ`/`ZZ` in the CSVs. Upstream configs label `70` as the gateway and `7f` as the Sensys; here it is the other way round |
| Zones | one heating zone (zone 1); zones 2–7, DHW and cooling are unused | zones 2–7, DHW and cooling are in `ariston.csv` but commented out, under headers by reason. The climate entity and the writes are zone 1 only |
| Hydraulics | the heat pump charges a 100 l buffer (one sensor); the Multifunctional Kit ("3 direct zones") switches the circulation pump on a zone 1 heat request | the buffer and kit entities |
| Heating mode | heat only; the backup heater is never used | DHW, cooling and backup heater registers are untested |
| eBUS adapter | ESP-based adapter (enhanced protocol), **arbitration delay 300 µs** | with the default 200 µs ebusd and the Sensys started together and collided repeatedly |
| Energy meter | one meter for the whole heating system (here a Shelly Pro 3EM) | `ha/cop.yaml`: replace `SHELLY_TOTAL_ACTIVE_POWER` with your meter |

Register ids are per device, and several upstream names are wrong for this system. Each register in
`registers/registers.yaml` has a status: `confirmed` (Sensys display, cloud, manual or measurement),
`hypothesis` or `unknown`. The status is in every field comment of `config/ariston.csv`.

## Install

### 1. ebusd

Copy the files of `config/` into ebusd's configuration directory (in the HA add-on: its config
folder, with the add-on's config path option pointing there). ebusd loads every CSV in that
directory **and its subdirectories**, so keep backups elsewhere.

Command-line options (in the add-on: its command-line options field):

* `--scanconfig=off`, always. In ebusd 26.1 `--scanconfig=none` still runs the auto scan, and a
  writable ebusd then sends an identification request to every device it sees.
* For a purely passive ebusd add `--readonly` and leave out `polled.csv` and `writes.csv`. The heat
  pump diagnostics and the climate entity need ebusd without `--readonly`.
* Do not use `--pollinterval=0` with a scan config other than `off`.
* MQTT in JSON mode, with `config/mqtt-hassio.cfg` as the integration file. Keep `filter-seen = 1`
  in it.

**Never poll `203a`/`203b`, and never run an ebusd bus scan.** Another Bridgenet user's boiler
crashed and rebooted after a full address scan that polling `203b` triggered.

What ebusd sends without `--readonly`, and when:

* `polled.csv`: four heat pump diagnostic reads (pressures, fan speeds, compressor, valve and heater
  flags). They have no poll priority: ebusd sends one only when HA publishes its `.../get`
  (`ha/polled_reads.yaml`).
* `writes.csv`: six zone 1 write messages, each a copy of what the Light Gateway or the Sensys
  broadcasts for the same action: mode (off / manual / scheduled), comfort temperature, and a timed
  override (temperature, flag, end time, cancel). ebusd sends one only on an MQTT `.../set`. Every
  field is a value list or a constant (10.0–30.0 °C in 0.5 steps, ...), so ebusd refuses anything
  else.

### 2. MQTT broker

Without `--readonly`, ebusd acts on any client's `ebusd/<circuit>/<name>/get`, `.../set` and
`ebusd/list`. Allow only ebusd and Home Assistant to publish below `ebusd/`, and Home Assistant only
the `get` topics of `polled.csv`, the `set` topics of `writes.csv` and `ebusd/list`.
`ebus-mqttacl` (see Tools) checks this from outside.

### 3. Home Assistant

Enable packages (`homeassistant: packages: !include_dir_named packages`) and copy the ones you want
into `<config>/packages/`. ebusd's entities are in the area "Heating"; recent HA versions put it in
front of new entity ids (`sensor.heating_ebusd_...`). The packages use those ids: check them under
the ebusd devices.

| Package | What it does | Needs |
|---|---|---|
| `cop.yaml` | heat output from flow and ΔT, heat and electrical energy, COP over 10 minutes and per day | your energy meter's power entity |
| `merged.yaml` | one sensor each for values decoded from several frame layouts (buffer temperature, zone 1 flow target, flow setpoint, heat pump air temperature, condenser outlet, curve offset, fixed flow temperature) | |
| `polled_reads.yaml` | requests the heat pump diagnostics once a minute while the heat pump is active and on each status change | `polled.csv`, no `--readonly` |
| `climate_zone1.yaml` | the zone 1 climate entity: off / heat (Manual) / auto (time program), setpoint, timed override in auto, `script.zone1_override`. Changes show at once and revert, with a notification, if the energy manager does not confirm them | `writes.csv`, no `--readonly` |
| `ebusd_list_on_start.yaml` | after an HA restart, asks ebusd to re-send its cached values (ebusd does not retain them) | |
| `ebus_health.yaml` | notification when ebusd cannot send (three rounds of diagnostic reads without a reply), is stopped, or has no bus signal | `polled_reads.yaml` for the first case |
| `dashboard_heating.yaml` | a "Heating" dashboard view (paste into the raw configuration editor) | the packages above |

## Before you enable reads and writes on another system

1. Run ebusd with `--readonly` first and compare the entities with your Sensys display. Check the
   addresses in the table above: if your devices sit elsewhere, the CSVs do not match.
2. Capture your bus (Tools) and check that the telegrams the writes copy look the same on your
   system: make each change once in the Ariston app or on the Sensys and compare with `writes.csv`.
   Another energy manager firmware may use other registers or ranges.
3. Before every change to ebusd's command line, run it against a fake bus that records what ebusd
   writes (`ebusnimbus.fakebus`, as in `tests/test_writes_ebusd.py`), and confirm that nothing but the
   expected telegrams goes out.
4. Restrict the MQTT topics (Install, step 2) before dropping `--readonly`.
5. If ebusd collides with the Sensys (lone `30` bytes in the raw log, reads failing with
   "arbitration lost"), raise the adapter's arbitration delay.

## Operation

* **After a power loss, check that ebusd can still send.** Here, after an outage the adapter kept
  receiving (every broadcast arrived, the signal stayed on) but wrote nothing for two days; every
  read failed with "arbitration lost", and the adapter's status page showed 0 bytes written
  ("eBUS/Host r/w"). Restarting ebusd did not help; restarting the adapter did. `ebus_health.yaml`
  raises a notification for this.
* **Settings that may stay stale.** Some energy-manager settings (CH mode, AUX/HV input
  configuration and state, quiet mode enabled) are on the bus only when the Light Gateway reads them
  for the Ariston cloud, or when the Sensys's rotating read happens to start at the decoded
  register. They rarely change; their entities may show an old value or none.
* **Second layouts.** The energy manager rotates registers through its broadcast frames, and ebusd
  decodes one layout per message. A few key values are therefore also decoded from other layouts,
  as diagnostic entities "... (alt. layout)", and `merged.yaml` combines them. The zone 1 curve
  offset and fixed flow temperature come every 30 minutes from the energy manager's settings
  broadcast.
* **The timed override** is held by the Sensys: it adopts an override at its next minute tick and
  ends it by itself at the end time. Starting or cancelling one shows within a minute.

## Tools

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest      # tests that need an ebusd binary are skipped without $EBUSD / $EBUSD_MQTT
```

Config generation (`config/*.csv` are generated; `mqtt-hassio.cfg` is ebusd's stock file with
changes marked "Ariston Nimbus"):

```
python scripts/gen_config.py        # config/*.csv from registers/ (polled.csv and writes.csv from the whitelists)
scripts/check_config.sh             # ebusd's own config check
```

`registers/registers.yaml` is the source of truth here. It was built from the Sensys menu sheets
and the upstream configurations, which are not part of this publication (nor are the scripts that
merged them); edit `registers.yaml` (or `registers/labels.yaml`, `polled.yaml`, `writes.yaml`)
directly.

Every config change is checked with ebusd's config check and with `ebus-replay` (below): decoded
values must equal the parser's.

How the generator lays out messages: ebusd keys a message on PBSB plus at most 6 data bytes, so
reads are grouped by their first three register ids and broadcasts by their first id, and only
positions that cannot be misread are decoded. A frame with another, shorter layout is rejected by
ebusd (an "invalid pos" log line), never decoded wrongly. `mqtt-hassio.cfg` passes only passive
messages and the reads to HA discovery (`filter-direction = r|^u$`), so write messages never become
entities.

**Capture.** Let ebusd log raw telegrams (`--lograwdata --lograwdatafile=/share/ebusd-raw.log
--lograwdatasize=100000`: message mode, size in kB; ebusd truncates the file on restart and keeps
one rotated copy) where the capture service can read it, then:

```
cp config.example.toml config.local.toml    # git-ignored: file path, time zone, meter address
ebus-capture --config config.local.toml     # captures/YYYY-MM-DD.log, timestamps with UTC offset
ebus-sidechannel --config config.local.toml # captures/YYYY-MM-DD.side.csv from a Shelly Pro 3EM
```

The capture follows rotation and truncation, resumes after its own restart, handles the repeated
hour at the end of daylight saving time, and marks gaps (`# gap`). `source = "cmd"` follows any
command's output instead of a file (for example `docker logs -f`). `deploy/*.service` run both as
systemd units. The Shelly is polled over its local RPC without authentication: keep it on the LAN.
Captures contain serial-number broadcasts: keep them private (`captures/` is
git-ignored).

| Command | Purpose |
|---|---|
| `ebus-census captures/*.log` | coverage report: registers seen, named and confirmed; unparsed telegrams; never-seen PBSB codes; gaps |
| `ebus-replay captures/*.log` | decodes captures with a private, read-only ebusd on a fake device and compares every value with the parser (`$EBUSD` or `--ebusd`) |
| `ebus-updates captures/*.log` | how often each entity is actually updated, against how often its register is on the bus |
| `ebus-cycles captures/*.log` | compressor runs and pauses, frequency, temperatures, energy, heat and COP per run, and why each run stopped |
| `ebus-hasim` | evaluates `ha/cop.yaml` offline on replayed captures, with HA's rules |
| `ebus-mqttacl` | checks the broker's restrictions on ebusd's request topics with a test user, anonymously and as HA (users from `.env`, see `.env.example`); refusal probes are chosen so that a wrongly accepted one sends nothing |
| `ebus-cloud` | read-only Ariston cloud client (allowlist of read endpoints; `pip install -e '.[cloud]'`), used to match cloud values with bus registers. Credentials in `.env` (git-ignored); at most `--budget` requests per run, `--interval` s apart, each logged. The cloud blocks accounts that poll too often |

Protocol notes and findings (framing, CRC, the register width rule, commands, the write telegrams)
are in `docs/protocol.md`.

## Credits and licence

The configuration builds on public work: wrongisthenewright's Bridgenet configuration (with
claytonn73's pull request #53), hesselonline, ysard, tweetgeek, komw, Syax89, claytonn73's Nimbus
configuration, and ebusd by john30. Licences, commits and the MIT notices are in
`THIRD_PARTY_NOTICES.md`; attribution per register is in `registers/registers.yaml`.

This repository is licensed under the GNU GPLv3 (`LICENSE`).
