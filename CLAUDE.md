# CLAUDE.md — moto-server

@.claude/PLATFORM-RULES.md

## What this repo is

The server side (Python, D-010): receives, stores, and serves ride data coming from the vehicle.

### v0 as built

- **Ingestion:** `POST /sessions` (FastAPI, `src/moto_server/api.py`). Multipart field `archive` = a session `.zip` written by the phone app (`moto-mobile`, BLE path of D-032). The zip holds `meta.json`, `telemetry.csv`, `events.csv`, `summary.json` and optional `imu.csv`, `gps.csv` (D-060) and `signals.csv` (the D-066 item 4 vehicle-signal block), at the zip root or in one top-level folder. Responses: `201` created, `200` identical re-upload (idempotent), `409` same session id with different content, `422` invalid session, `413` over `MOTO_MAX_UPLOAD_MB`. The same ingest runs from the CLI (`moto-server import <folder-or-zip>`).
- **Read API:** `GET /sessions` (list), `GET /sessions/{id}/report` (`?format=text` for a summary), `GET /health` (no auth). Everything else needs a bearer token (`MOTO_API_TOKEN`; 503 if unset).
- **Validation and gap report (D-032, D-045):** every session gets a `report.json` with the telemetry layout/packet versions, the defs version used to decode, a raw_hex re-decode check, packet loss recomputed from `seq`, rx/device-time ordering and gaps, per-signal range/validity checks against `moto_defs`, CAN health (v3), tester stats (v4: last step gap, last per-DID round-trip record, D-058), IMU sample-loss/scale checks and a GPS block summary (D-060: blocks lost or MTU-skipped from `seq`, fix types, share usable for the speed check, node parse/UART errors) and a vehicle-signal summary (D-066: blocks lost from `block_seq`, per DID the states seen, valid value range, NRCs, unknown DIDs). Status is `ok` / `warn` / `fail`.
- **Storage:** files + SQLite index under `MOTO_DATA_DIR`: raw upload verbatim, decoded **Parquet** (`telemetry.parquet`, `imu.parquet`, `gps.parquet`, `signals.parquet`, `derived.parquet` under `parquet/`), `report.json`, `index.sqlite` (`has_imu`, `has_gps`, `has_vehicle_signals`, `has_derived`; new columns are added to an existing index on open). The decoded telemetry Parquet also carries the 8 version 4 tester columns (nullable integers, same names as in `telemetry.csv`). Decoding uses the BLE schema from the defs submodule (`moto_defs.ble.SCHEMA`, D-061; no copy in this repo, no drift test) and signals from `moto_defs`, never hand-written. `signals.csv` rows are re-decoded from `did` + `raw` through the generated vehicle catalog (`moto_defs.vehicle_catalog`, D-066): a DID promoted from the candidate catalog needs no change here, only a defs bump.
- **Derived signals (D-070 item 2, parts 1 and 2):** `src/moto_server/derived/` is the Python reference of `moto_defs.vehicle_derived`: `inputs.py` builds per-input series from the session's parquet files (device_time_ms axis; DIDs from `signals.parquet`, the five telemetry DIDs as fallback; sensors by field-name conversion of `SENSORS[...]["phase0"]`; GPS speed/heading only, never a position), `engine.py` evaluates in dependency order on the defs' `period_ms` grid (stale input -> null, result outside `range` -> null + `out_of_range`, never a held value), `formulas.py` holds the arithmetic. 34 class-A signals: part 1 = 19 (gearbox ratio, gear estimate, vehicle accel/jerk, IMU residual, trip distance/time, idle time, yaw rate, lateral accel, turn radius, engine/demand torque, torque gap, engine power, air density, power correction, AFR, MAF fuel rate), part 2 = fuel economy, trims, throttle/pedal residuals, altitude change, road grade, coolant/oil rates, lean cross-check and the `stop_event` event (formula kind `event`, no grid); the other events, class B/C/D and the other class-A signals are `not_implemented`, `ready_phase0: false` / `pending_inputs` signals are `not_ready`. Output `derived.parquet` (long format) + the `derived` block of `report.json`; computed after the parquet step, an exception is recorded as `derived.error` and never fails the ingest. `moto-server derive <session-id>|--all` recomputes stored sessions.

### Planned (not built yet)

- Ingestion of rt-core's binary log files and live streaming from Kuksa over MQTT/Zenoh. (Persistent on-vehicle logging is rt-core's job, D-045.)
- **MDF4** export for archive/sharing (`mdf4.py` is a skeleton that raises `NotImplementedError`).
- Moving storage to InfluxDB + Grafana if it grows (the Eclipse Fleet Management pattern is a reference).
- API: `query_ride_history`, ride summaries, firmware packages (OTA source).

## What this repo is NOT

- No model training (`moto-ml` reads data from here).
- Never sends commands to the vehicle. It only **serves** the OTA package; distribution is done by the Raspi.

## Rules

- The data schema and metadata match `../moto-vehicle-defs/docs/phase0-data-collection-plan.md` §3 exactly. A session ID is present in every record (ML's per-session split relies on this).
- Signal names/scales come from `moto_defs` (the pinned defs submodule), never hand-written. The report records the defs version used to decode. (Planned with the rt-core binary log: pick the decoder by the defs version in the log header.)
- Location history is stored encrypted at the disk/DB level. Externally exposed endpoints require TLS + authentication.

## Dependencies

`external/moto-vehicle-defs` (tagged).

## Build

`uv` + `ruff` + `pytest` (D-007), FastAPI. Local services run via `docker compose`.

```bash
git submodule update --init --recursive   # external/moto-vehicle-defs, pinned to v0.14.0
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run moto-server serve      # needs MOTO_API_TOKEN
MOTO_API_TOKEN=... docker compose up --build
```

See README.md for the CLI, HTTP API, storage layout and environment variables.

## Context

ARCHITECTURE §7 · `../moto-vehicle-defs/docs/phase0-data-collection-plan.md` §3, §8 · `../moto-vehicle-defs/docs/hardware-architecture.md` §5b.8.
