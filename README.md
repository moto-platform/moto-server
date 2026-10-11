# moto-server

The server side of the moto-platform SDV stack (D-010): ingests ride sessions
recorded by the phone app (`moto-mobile`), validates and decodes them against
the BLE telemetry schema, and stores them as Parquet + a queryable index for
`moto-ml` and analysis. Part of the [moto-platform](https://github.com/moto-platform)
organization; see `moto-vehicle-defs/docs/ARCHITECTURE.md` for the platform picture.

## What this repo is not

- No model training (`moto-ml` reads data from here).
- Never sends commands to the vehicle; it only stores and serves data.
- No MDF4 export yet (skeleton only, see `mdf4.py`).

## Setup

```bash
git submodule update --init --recursive   # external/moto-vehicle-defs, pinned to v0.11.0
uv sync                                    # installs runtime + dev dependencies
```

## Run

```bash
export MOTO_API_TOKEN=some-long-random-token   # required; the API refuses (503) without it
uv run moto-server serve                       # http://0.0.0.0:8000
```

Or with Docker Compose (builds the image, mounts a named volume for `/data`):

```bash
MOTO_API_TOKEN=some-long-random-token docker compose up --build
```

A deployment exposed outside a trusted network needs TLS in front of it (e.g.
a reverse proxy) -- the API itself only does bearer-token auth, not TLS.

## CLI

```bash
uv run moto-server import <folder-or-zip>      # ingest a session, print its report
uv run moto-server report <session_id>         # re-print a stored session's report
uv run moto-server report <session_id> --format json
uv run moto-server list                        # one line per ingested session
uv run moto-server serve [--host] [--port] [--reload]
```

`import` and `report` exit 0 for status `ok`/`warn`, 1 for `fail` or an
invalid session, 2 for a session-id conflict (same id, different content).

## HTTP API

All endpoints except `GET /health` require `Authorization: Bearer $MOTO_API_TOKEN`.

| Method & path | Description |
|---|---|
| `GET /health` | Liveness check, no auth. |
| `POST /sessions` | Multipart field `archive` (a session `.zip`). `201` created, `200` identical re-upload (idempotent), `409` same id/different content, `422` invalid session, `413` over `MOTO_MAX_UPLOAD_MB`. |
| `GET /sessions` | List ingested sessions from the index. |
| `GET /sessions/{id}/report` | The session's `report.json`. Add `?format=text` for a human-readable summary. `404` for an unknown id. |

A session upload is a zip with `meta.json`, `telemetry.csv`, `events.csv`,
`summary.json` (and optional `imu.csv`, `gps.csv` and `signals.csv`) either at the zip root or inside one
top-level folder -- see the session contract the phone app writes to.

## Storage layout

```
$MOTO_DATA_DIR/
  index.sqlite                     # one row per ingested session
  sessions/<session_id>/
    raw/                           # the uploaded files, verbatim
    parquet/telemetry.parquet      # decoded telemetry, one row per BLE packet (incl. tester stats)
    parquet/imu.parquet            # decoded IMU samples (if imu.csv was present)
    parquet/gps.parquet            # decoded GPS blocks (if gps.csv was present)
    parquet/signals.parquet        # vehicle-signal records (if signals.csv was present)
    report.json                    # validation report (see below)
```

Parquet column names follow `<defs signal>_<unit>` (e.g. `engine_speed_rpm`,
`vehicle_speed_kmh`, `coolant_temp_degc`, `throttle_pos_pct`,
`battery_voltage_v`), with `<signal>_age_ms` and `<signal>_valid` alongside
each. Units come from `moto_defs.vehicle_cl250.DIDS`, never hand-written
(platform rule: signals only come from moto-vehicle-defs).

Telemetry version 4 (D-058) adds eight tester-statistics columns at the end of
`telemetry.csv` (`step_gap_max_ms`, `step_gap_over_count`, `rtt_did`, `rtt_min_ms`,
`rtt_max_ms`, `rtt_sum_ms`, `rtt_count`, `rtt_nrc78_count`; 43 columns in total). They
are stored under the same names as nullable integer Parquet columns, decoded from
`raw_hex` (null for version 2/3 rows). A `telemetry.csv` recorded before them (21 or
35 columns) is still accepted and gets null columns.

### Validation report

Every ingested session gets a `report.json` (and `render_text()` summary)
covering: which telemetry layout (v2/v3/v4) and packet versions were seen, the
defs version used to decode it, a raw_hex re-decode vs. the app's own decoded
columns (consistency check), packet loss recomputed from `seq`, rx/device
time ordering and gaps, per-signal range and validity checks against
`moto_defs.vehicle_cl250.DIDS`, CAN health (v3), tester stats (v4), IMU
sample-loss/scale checks (recomputed from `sample_index` and `*_raw`, including
`sensorReconfigured` blocks), a GPS summary and a vehicle-signal summary (see below). Overall status is
`ok` / `warn` / `fail`.

The `tester_stats` section (absent when the session has no version 4 packet) holds the
last `step_gap_max_ms` / `step_gap_over_count` and, per `rtt_did` (keyed in hex, e.g.
`0xF40C`), its last record: min, max, avg (`sum / count`, null when the count is 0 or the
sum or count is saturated at `0xFFFFFFFF`), count and NRC 0x78 count. These are
measurements of the temporary vehicle-bus tester (D-058), not vehicle signals; they do
not change the session status.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MOTO_DATA_DIR` | `./data` | Root of all stored session data and the SQLite index. |
| `MOTO_API_TOKEN` | *(none)* | Bearer token for every endpoint except `/health`. Protected endpoints return `503` if unset. |
| `MOTO_MAX_UPLOAD_MB` | `100` | Maximum accepted `POST /sessions` upload size. |

## BLE schema

The BLE packet layouts (telemetry versions 2, 3 and 4, the IMU, GPS and vehicle-signal blocks) come from
moto-vehicle-defs: `moto_defs.ble.SCHEMA`, generated from its `ble/ble_schema.json`
(D-061). This repo keeps no copy and no drift test; `ble_schema.load_default_schema()`
returns a deep copy of it and the decoder dispatches on the packet's `version` byte. A
layout change is a defs release; bump the `external/moto-vehicle-defs` pin to take it.

## GPS (speed and heading only)

The phone records the GPS block of connectivity-node (D-060, BLE schema
`gpsBlock`) to an optional `gps.csv`, one row per block: receive time, `seq`,
`lost_since_prev`, `raw_hex` (26 bytes, authoritative), the raw fields
(`device_time_ms`, `ground_speed`, `heading_of_motion`, `speed_accuracy`,
`heading_accuracy`, `fix_type`, `num_sv`, `flags`), their scaled values (`*_mps`,
`*_deg`) and the flag bits (`gnss_fix_ok`, `parse_error`, `uart_overflow`). The
server re-decodes `raw_hex` and stores `gps.parquet` from it, with the same
columns plus `fix_type_name`.

The report's `gps` section (null without `gps.csv`) gives the block count, the
blocks **lost or MTU-skipped** from `seq` gaps (the node also advances `seq` for a
block it skips because the MTU is too small, D-062, so this is not a radio-loss
figure), the effective rate from `device_time_ms`, the fix types seen, the share
usable for the CAN speed check (fix3d or gnssDeadReckoning with `gnssFixOk`,
schema `fixType.rule`) and the blocks flagged with a node parse error or UART
overflow (each flag covers the interval since the previous block). A GPS row
whose `raw_hex` does not decode fails the session; GPS gaps, node errors, app/
server mismatches or no usable block make it `warn`.

**No position, ever.** Latitude, longitude and height never leave
connectivity-node (D-060 item 3, invariant 7). The `gps.csv` header must match
exactly, so a session with any extra column (a position included) is rejected.
A speed + heading series can still rebuild the route's shape by dead reckoning
(D-060 item 5, accepted residual risk): in Phase 0 this server runs on the
user's own machine, sessions with `gps.csv` must not be uploaded to a server
that is not local, and real sessions are never committed (D-033).

## Vehicle signals (D-066 item 4)

Every DID of the vehicle-bus tester's table travels in the generic BLE vehicle-signal
block (schema `vehicleSignalBlock`, characteristic `vehicleSignals`; records keyed by DID,
at most 3 blocks per 200 ms). The phone writes them to an optional `signals.csv`, one row
per **record**; the block header (`block_seq`, `device_time_ms`, `total_records`,
`first_record`, and the phone's receive time) is repeated on every row of a block. The
wire values `did`, `state` (`BleVehicleSignalState`: 0 neverReceived, 1 valid, 2 stale,
3 skipped, 4 negativeResponse), `age_ms` (sentinels `65535` = no positive answer since
boot, `65534` = saturated) and `raw` (the data bytes as an unsigned big-endian number for
state 1 and 2, the NRC for state 4) are authoritative. `signal` and `value` are the app's
catalog decode. The contract source is moto-mobile's `docs/session-format.md`; the header
must match exactly (any other column is rejected).

The server validates the wire values against the schema field sizes (`did` and `age_ms`
u16, `raw` u32, `state` a known value) and re-derives `signal` and `value` from `did` +
`raw` with the generated vehicle catalog (`moto_defs.vehicle_catalog`: `BY_DID`, `SIGNALS`,
`decode`), `raw * factor_num / factor_den + offset`, only for state 1 or 2 and a raw that
fits the DID's `length`. The app's columns are a consistency check (a mismatch makes the
session `warn`). A DID missing from the pinned catalog (a sender built from a newer defs)
is kept with its raw value and an empty signal/value. A DID promoted from the candidate
catalog (D-066 item 3) is handled by the catalog alone: bump the defs pin, no code change.

`signals.parquet` has one row per record: `rx_utc`, `rx_mono_ms`, the block header,
`did`, `state`, `state_name`, `age_ms`, `raw`, `signal`, `unit`, `value` and
`sample_device_time_ms` (`device_time_ms - age_ms`: the node time of the DID's last
positive answer, null for the sentinel ages and for states without one, so a record
repeated by several blocks can be deduplicated). `value` is set for state `valid` only
(a stale value is shown as unavailable; `raw` is always kept). A row whose wire values
fail validation keeps its receive time and a `decode_error`, and fails the session.

The report's `vehicle_signals` section (null without `signals.csv`) gives the block and
record counts, the blocks lost from `block_seq` gaps (`sequenceRule`), and per DID (keyed
in hex): the catalog name and unit, the states seen, the valid-value range, the last
valid value and the NRCs. Lost blocks, app/server mismatches, a raw that does not fit its
DID or a `vehicle_signal_block_version` in `meta.json` that differs from the pinned defs
make the session `warn` (the key is absent in sessions from older app builds). Negative
responses, stale, skipped and unknown DIDs are normal and do not. `signals.csv` holds
engine data only (no position), so the D-063 local-only rule for `gps.csv` does not apply.

## Development

```bash
uv run pytest              # tests, including tests/fixtures/{v2,v3,v4,v4_gps,v4_vsig}_session
uv run ruff check .
uv run ruff format --check .
uv run python tests/make_fixtures.py   # regenerate the committed fixtures
```

Fixtures are generated from the schema itself (`ble_schema.pack_fields`), not
hand-written byte offsets; regenerate and commit them if the fixture
scenarios need to change.

## License

MIT, see `LICENSE` (D-036).
