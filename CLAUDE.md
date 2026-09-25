# CLAUDE.md — moto-server

## What this repo is

The server side (Python, D-010): receives, stores, and serves ride data coming from the vehicle.
- Ingestion: rt-core's binary log files (Wi-Fi sync) + live streaming from Kuksa over MQTT/Zenoh.
- Conversion: binary → **MDF4** (archive/sharing) and **Parquet** (for ML). Conversion happens here, not on the vehicle.
- Storage: start simple (files + Python), move to InfluxDB + Grafana if it grows. The Eclipse Fleet Management pattern is used as a reference.
- API: `query_ride_history`, ride summaries, firmware packages (OTA source).

## What this repo is NOT

- No model training (`moto-ml` reads data from here).
- Never sends commands to the vehicle. It only **serves** the OTA package; distribution is done by the Raspi.

## Rules

- The data schema and metadata match `../moto-vehicle-defs/docs/phase0-data-collection-plan.md` §3 exactly. A session ID is present in every record (ML's per-session split relies on this).
- Signal names/scales come from `gen/python/` or the DBC. The binary log decoder is selected based on the defs version; the defs version is written in the log header.
- Location history is stored encrypted at the disk/DB level. Externally exposed endpoints require TLS + authentication.

## Dependencies

`external/moto-vehicle-defs` (tagged).

## Build

`uv` + `ruff` + `pytest` (D-007), FastAPI. Local services run via `docker compose`.

## Context

ARCHITECTURE §7 · `../moto-vehicle-defs/docs/phase0-data-collection-plan.md` §3, §8 · `../moto-vehicle-defs/docs/hardware-architecture.md` §5b.8.
