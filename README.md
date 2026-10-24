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
git submodule update --init --recursive   # external/moto-vehicle-defs, pinned to v0.18.1
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
uv run moto-server derive <session_id>         # recompute the derived signals and ride summaries of a stored session
uv run moto-server derive --all                # ... of every stored session
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
| `GET /sessions/{id}/series` | Downsampled time series for the analyzer oscilloscope/replay. Query: `signals` (comma-separated `did.<NAME>`, `sensor.<name>`, `derived.<signal>`; default all available), `max_points` (10..20000, default 2000; min/max buckets so spikes survive), `from_ms`/`to_ms` (inclusive window relative to `t0_device_ms`, applied before downsampling). Returns `{session_id, t0_device_ms, max_points, series: [{name, kind, unit, points_total, downsampled, t_ms, v}]}`; `t0_device_ms` is the earliest sample of any series of the session, so calls share one time axis. `404` unknown session, `422` unknown series name or `max_points` out of range. Never contains a GPS position. |
| `GET /ws/live` (WebSocket) | Live stream for moto-analyzer (D-081). First message `{"type": "hello", "token": "<MOTO_API_TOKEN>"}` within 5 s, optional frame filters `"channels": [1, 2]` and `"ids": [...]`; the token is never put in the URL. Then `{"type": "batch", "payload": StreamBatchDto}` messages and every 5 s `{"type": "heartbeat", "payload": {"source", "dropped", "timestamp_us"}}`. Close `1008` bad hello or token, `1011` no `MOTO_API_TOKEN`. One bounded queue per client: a slow client loses its oldest batches (`dropped`), never blocks the others. |

A session upload is a zip with `meta.json`, `telemetry.csv`, `events.csv`,
`summary.json` (and optional `imu.csv`, `gps.csv` and `signals.csv`) either at the zip root or inside one
top-level folder -- see the session contract the phone app writes to.

## Storage layout

```
$MOTO_DATA_DIR/
  index.sqlite                     # one row per ingested session (has_imu, has_gps, has_vehicle_signals, has_derived)
  sessions/<session_id>/
    raw/                           # the uploaded files, verbatim
    parquet/telemetry.parquet      # decoded telemetry, one row per BLE packet (incl. CAN health, tester stats)
    parquet/imu.parquet            # decoded IMU samples (if imu.csv was present)
    parquet/gps.parquet            # decoded GPS blocks (if gps.csv was present)
    parquet/signals.parquet        # vehicle-signal records (if signals.csv was present)
    parquet/derived.parquet        # derived signals, long format (if any could be computed)
    parquet/derived_summary.parquet  # class-B ride summaries, long format (if any)
    report.json                    # validation report (see below), incl. the `derived` and `derived_summary` blocks
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

The version 3 CAN health fields are stored too, also decoded from `raw_hex`, as nullable
integers under the schema field names in snake_case: `can_bus_state`, `can_tx_error_count`,
`can_rx_error_count`, `can_bus_off_count`, `unanswered_did_count`, `can_flags` (the csv calls
the error counters `can_tec`/`can_rec`). They are null for version 2 rows.

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
| `MOTO_CORS_ORIGINS` | *(empty)* | Comma-separated browser origins allowed to call the API (e.g. `http://localhost:5173` for moto-analyzer). Empty: no CORS middleware. Methods GET/POST, headers Authorization + Content-Type, no credentials. |
| `MOTO_LIVE_SOURCE` | `none` | Producer of `/ws/live`: `none` (heartbeats only) or `mock` (synthetic frames built from defs by `mock_stream.py`; the heartbeat says `"source": "mock"`). The real source (rt-core via the Raspi, D-077) is not built yet. |

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

## Derived signals (D-070 item 2)

`moto_defs.vehicle_derived` defines the derived signals (class A live on rt-core, B per ride,
C across rides, D server only). Until rt-core exists the Python reference in
`src/moto_server/derived/` computes them offline from a stored session:

- `inputs.py`: one series per input on the node's `device_time_ms`. `did.<NAME>` rows come
  from `signals.parquet` (`signal == NAME`, `value` set, at `sample_device_time_ms`; the five
  telemetry DIDs are the fallback when there is no `signals.csv`); `sensor.<name>` maps
  through `SENSORS[name]["phase0"]` (`ble.imuBlock.ax` -> `imu.parquet` `ax_g`, converted to
  m/s2 with standard gravity; `ble.gpsBlock.*` -> `gps.parquet`, speed, speed accuracy,
  heading of motion and fix type only, never a position); `const.<name>` is
  `moto_defs.vehicle_constants`. Sensors the server does not store (e.g. `lean_angle`) are
  `missing_inputs`; the tester and CAN health counters come from `telemetry.parquet`.
- `engine.py`: topological order over `derived.*` inputs; the output grid is the signal's
  `period_ms` from the span where all inputs exist. An input counts while its age is within
  its `stale_after_ms` (the signal's own for `did.*`, `SENSORS` for sensors, the producer's for
  `derived.*`); otherwise the row is null with flag `stale_input`. A null derived input gives
  `invalid_input`, too few samples in a window `insufficient_samples`, a result outside the
  defs `range` null with `out_of_range`. `window_ms` is the least-squares slope window
  (`vehicle_accel`, `vehicle_jerk`) or the moving-average window (`yaw_rate`,
  `accel_imu_residual`).
- `formulas.py`: 34 class-A signals, from vehicle-data-catalog section 5 (and each signal's
  defs description): part 1 (19, below) and part 2 (fuel economy, trims and throttle/pedal
  residuals, `altitude_change`, `road_grade`, `coolant_rate`, `oil_temp_rate`,
  `lean_crosscheck_residual`, `stop_event`). Assumptions: the ECU speed is km/h and integer; the
  IMU z axis is the yaw axis and x the longitudinal axis (no mounting or lean correction,
  gravity not removed from `accel_imu_residual`); `trip_distance`, `trip_time` and `idle_time`
  integrate from the first common sample and hold a sample for at most its stale limit, so a
  gap adds nothing; `gear_estimate` matches within 5 % of a gear ratio (else 0);
  `turn_radius` is `v / |omega|` and null below 0.5 deg/s; `air_density` is the dry ideal-gas
  density (R = 287.058 J/(kg K)) with the intake temperature; `power_correction_factor` is
  rho_ref / rho with rho_ref at the SAE J1349 reference (99 kPa, 25 degC), the density-ratio
  form rather than the full standard; `air_fuel_ratio` and `fuel_rate_maf` use the commanded
  lambda and `stoichiometric_afr` / `fuel_density_kg_per_l`. Provisional vehicle constants
  used are listed in the report block.
- Part 2 assumptions: `fuel_per_stroke` is one injection per two revolutions;
  `altitude_change` is the hypsometric equation against the first valid barometric sample of
  the session (g0 = 9.80665); `road_grade` uses the first and last samples of altitude and
  trip distance in the window (`sensor.imu_accel_x` is declared as context only, unused);
  `lean_crosscheck_residual` takes both signs as positive = right (to be confirmed on the
  bike) and needs `lean_angle`, which this server does not store (`missing_inputs`).
  New flags: `no_consumption`, `no_power`, `engine_off`.
- Event kind: a formula of kind `event` has no grid; its rows sit at the event times and its
  coverage entry has no `period_ms`. Only `stop_event` exists: a stop starts at the first speed
  0 with RPM above 0 and ends at the first later speed above 0 (value = seconds, row time =
  the end); a gap in the speed samples above 3 poll periods (D-025) or a stop still open at
  the end gives no event. The range check applies to events too.
- Not computed (`not_implemented`): the other events, the other class-A signals without a
  formula (missing thresholds or constants), and class B/C/D; signals with `ready_phase0:
  false` or `pending_inputs` (`not_ready`, never evaluated).

`parquet/derived.parquet` is long format: `session_id` (every row),
`device_time_ms`, `signal`, `value` (float64, null when invalid), `unit`, `valid` (bool) and
`flag` (null, or why the row is invalid). The report's `derived` block holds the defs version
used, `summary` (count per status), `rows` / `valid_rows` and, per signal of the defs, its
`status`: `computed` (with `rows`, `valid_rows`, `flag_counts`), `not_implemented`,
`not_ready` (`reason` `ready_phase0` / `pending_inputs`), `missing_inputs` (`missing`) or
`error` (`message`). It is computed during ingest after the parquet step; an exception is
recorded as `derived: {"error": ...}`, leaves the ingest status unchanged and writes no
`derived.parquet`. `moto-server derive` recomputes it after a defs bump or a formula change.

### Ride summaries (class B, part 3)

`src/moto_server/derived/summary.py` evaluates the class-B signals after the class-A pass.
Phase 0 has no ride segmentation: **one stored session is one ride** (the report says
`ride_definition: "session"`). A summary reads the valid rows of its class-A inputs, the scalar
results of its class-B inputs (`ride_fuel_consumption` follows `ride_fuel_used` and
`ride_distance`, dependency order) and the raw `did.*` / `sensor.*` series. Time integrals and
time-in-cell use the class-A hold rule (`formulas.hold_segments`): a value is held between
samples but never beyond the input's freshness (`did.*` of a class-B signal: 3 poll periods,
D-025; sensors: `SENSORS` `stale_after_ms`), so a gap adds nothing. A value outside the defs
`range` is null with `out_of_range` (each cell too); statuses are the class-A ones.

- Implemented (19 of the 46 class-B signals): `ride_distance`, `ride_time`, `ride_idle_time`
  (last valid `trip_distance` / `trip_time` / `idle_time`), `ride_stop_count` (valid
  `stop_event` rows), `ride_fuel_used` (integral of the fuel rate, L), `ride_fuel_consumption`
  (`ride_fuel_used / ride_distance * 100`; distance <= 0 is `standstill`), `ride_ltft_mean`,
  `ride_stft_std` (population standard deviation, `insufficient_samples` below 2),
  `operating_point_time` ((ENGINE_SPEED, ENGINE_LOAD) cells), `ride_coolant_time_at_temp`,
  `ride_oil_time_at_temp`, `ride_coolant_rate_max`, `ride_start_coolant_temp`,
  `ride_end_coolant_temp`, `ride_start_intake_temp`, `ride_distance_gps` (integral of the GNSS
  ground speed, km; speed only, never a position), `ride_bus_off_count`, `ride_tester_rtt_max`,
  `ride_tester_step_gap_max`.
- Bins: uniform `[min, max)` per axis from the defs `bins`; the cell index is row-major with
  the first `bins` entry as the major axis (`cell = bin0 * count1 + bin1`). The time of a held
  value goes to its cell; a held value outside an axis range is in no cell and counted in the
  report's `outside_s`.
- `ride_bus_off_count` sums the increments of the cumulative counter between consecutive
  samples (the first sample is the baseline); a decrease means the node restarted and the new
  value is the increment. It reads `can_bus_off_count` of `telemetry.parquet`.
  `ride_distance_gps` also needs `sensor.gps_fix_type` (declared input) next to the speed.
- Choices the defs leave open (assumptions of this reference): `ride_bus_off_count` counts
  only bus-offs seen within the session and treats a decrease as a restart; `ride_ltft_mean` is
  a sample mean and `ride_stft_std` a population standard deviation (each DID has a fixed poll
  period, so a sample mean is a time average). A session that contains a key-off and key-on is
  still one "ride" here, so its class-B values mix both; `ride_stop_count` ignores a stop
  still open at the end of the data (the class-A `stop_event` rule).
- `not_implemented` with a `reason`: `ride_warmup_time` (threshold: operating coolant
  temperature), `ride_thermal_cycle_count` (threshold: cold/hot coolant temperatures),
  `ride_fan_on_temperature` (threshold: fan voltage step), `throttle_torque_lag` (definition:
  throttle step and lag search range), `ride_final_drive_ratio` (threshold: steady cruise),
  `speedometer_error` and `effective_tyre_radius` (threshold: steady speed and GNSS accuracy),
  `ride_aggressiveness_score` and `ride_cornering_aggressiveness` (definition: score weights),
  `ride_can_error_count` (definition: TEC/REC are levels, not error counts),
  `ride_unanswered_did_count` (definition: sensor and BLE field differ),
  `ride_tester_nrc78_count` (definition: BLE rttNrc78Count belongs to the rotating rttDid
  record) and the signals whose class-A input the reference does not compute
  (`input not implemented: derived.<name>`): `ride_heat_soak_max`, `ride_cranking_dip`,
  `ride_rest_voltage`, `ride_cold_abuse_time`, `ride_over_rev_time`, `ride_lugging_time`,
  `ride_idle_rpm_std`, `ride_idle_rpm_drift`, `ride_shift_count`, `ride_launch_count`,
  `coast_down_cda`, `coast_down_rolling_resistance`. `ready_phase0: false` / `pending_inputs`
  signals are `not_ready`; absent inputs are `missing_inputs` (with the list).

`parquet/derived_summary.parquet` is long format, one row per scalar or per cell: `session_id`,
`record` (e.g. `RIDE_USAGE`), `did` (int64, from `RIDE_SUMMARY_RECORDS`), `signal`, `cell`
(int64, null for scalars), `bin0_low`, `bin0_high`, `bin1_low`, `bin1_high` (float64, null when
absent), `value` (float64, null when invalid), `unit`, `valid`, `flag`. The report's
`derived_summary` block: `{defs_version, ride_definition, summary: {status: count}, records:
{RECORD_NAME: {did, signals: {name: entry}}}}`; a scalar entry has `status` / `value` / `unit` /
`flag`, a binned one `status` / `unit` / `cells` / `nonzero_cells` / `total_s` / `outside_s`
(no per-cell values), the others `reason` / `missing` / `message`. It is computed right after
the class-A step; an exception is recorded as `derived_summary: {"error": ...}`, writes no file
and never fails the ingest. The file is removed when there is no row. `moto-server derive`
recomputes both files and prints one more line per session. There is no index column.

## Golden vectors for rt-core (D-070 item 2)

`src/moto_server/derived/golden.py` runs the Python reference over four synthetic
scenarios (`idle_warmup`, `accel_gears`, `stale_gap`, `edge_values`; no GPS, no real ride,
no randomness) and writes the inputs and the expected outputs as C99 headers for the
moto-rt-core host tests:

```bash
uv run moto-server golden --out <moto-rt-core>/tests/host/vectors   # also python -m moto_server.derived.golden
```

- Defs version: the headers record the defs version they were generated from (v0.18.1 now).
  The expected outputs use that version's derived `stale_after_ms` (the age limit of a
  signal's DID inputs), which changed in v0.14.0, so rt-core's derived-module test needs a
  defs pin with the same derived table (v0.14.0 or later); against v0.13.0 the torque and
  power rows would differ. The indices are the same in v0.13.0.
- Location in rt-core: `tests/host/vectors/` (`derived_vectors.h` types + one
  `derived_<scenario>.h` per scenario). The headers are committed there; rt-core never
  depends on this repo, the host tests read no files and parse no strings. The headers use
  `VEHICLE_CL250_IDX_<NAME>` / `VEHICLE_CL250_DERIVED_<NAME>` of the generated defs code.
- Selection rule: class A, a formula in `FORMULAS`, `ready_rt_core` true, no `sensor.*`
  input (a GPS name is asserted never to be an input). Point and series signals have every
  output row; `stop_event` has one row per event at the event time (value = duration, s).
  Inputs are only the DIDs of those signals (plus `BATTERY_VOLTAGE`) at their own
  `poll_period_ms`, quantised to the DID's raw grid and float32. Invalid rows carry `valid =
  false`, a flag and value `0.0f`. The command fails if a selected signal is never valid.
- Tolerance: `abs_tol` = the defs `resolution` / 2 per signal (`derived_<scenario>_tol[]`).
- Drift check: `tests/golden/MANIFEST.sha256` lists the content-sha256 of every file (the
  hash in the banner covers the body after it, so it does not change with the server commit)
  and the defs version. `tests/test_golden.py` regenerates and compares; after a formula or
  defs-pin change, rerun the command above, commit the headers in rt-core and the manifest here.

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
