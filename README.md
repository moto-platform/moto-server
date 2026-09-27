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
git submodule update --init --recursive   # external/moto-vehicle-defs, pinned to v0.1.0
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
`summary.json` (and optional `imu.csv`) either at the zip root or inside one
top-level folder -- see the session contract the phone app writes to.

## Storage layout

```
$MOTO_DATA_DIR/
  index.sqlite                     # one row per ingested session
  sessions/<session_id>/
    raw/                           # the uploaded files, verbatim
    parquet/telemetry.parquet      # decoded telemetry, one row per BLE packet
    parquet/imu.parquet            # decoded IMU samples (if imu.csv was present)
    report.json                    # validation report (see below)
```

Parquet column names follow `<defs signal>_<unit>` (e.g. `engine_speed_rpm`,
`vehicle_speed_kmh`, `coolant_temp_degc`, `throttle_pos_pct`,
`battery_voltage_v`), with `<signal>_age_ms` and `<signal>_valid` alongside
each. Units come from `moto_defs.vehicle_cl250.DIDS`, never hand-written
(platform rule: signals only come from moto-vehicle-defs).

### Validation report

Every ingested session gets a `report.json` (and `render_text()` summary)
covering: which telemetry layout (v2/v3) and packet versions were seen, the
defs version used to decode it, a raw_hex re-decode vs. the app's own decoded
columns (consistency check), packet loss recomputed from `seq`, rx/device
time ordering and gaps, per-signal range and validity checks against
`moto_defs.vehicle_cl250.DIDS`, CAN health (v3), and IMU sample-loss/scale
checks (recomputed from `sample_index` and `*_raw`, including
`sensorReconfigured` blocks). Overall status is `ok` / `warn` / `fail`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MOTO_DATA_DIR` | `./data` | Root of all stored session data and the SQLite index. |
| `MOTO_API_TOKEN` | *(none)* | Bearer token for every endpoint except `/health`. Protected endpoints return `503` if unset. |
| `MOTO_MAX_UPLOAD_MB` | `100` | Maximum accepted `POST /sessions` upload size. |

## BLE schema

`src/moto_server/schemas/ble_telemetry_packet_schema.json` is a verbatim copy
of moto-connectivity-node's `docs/ble_telemetry_packet_schema.json` (the
single source of truth for the wire format). Update it with:

```bash
scripts/sync_ble_schema.sh [path-to-moto-connectivity-node]   # defaults to ../moto-connectivity-node
```

CI checks the copy hasn't drifted whenever it can reach moto-connectivity-node
(optional `MOTO_CONN_READ_TOKEN` secret); locally, set `MOTO_CONN_SCHEMA` to
that file's path to run the same check (`MOTO_SCHEMA_DRIFT_REQUIRED=1` makes
it a hard failure instead of a skip).

## Location data

The phone app does not record GPS yet. When it does, any location column
added here **must be stored encrypted at the disk/DB level** (platform rule:
raw GPS never goes to the cloud unencrypted) -- do not add a plaintext
location column without addressing that first.

## Development

```bash
uv run pytest              # tests, including tests/fixtures/{v2,v3}_session
uv run ruff check .
uv run ruff format --check .
uv run python tests/make_fixtures.py   # regenerate the committed fixtures
```

Fixtures are generated from the schema itself (`ble_schema.pack_fields`), not
hand-written byte offsets; regenerate and commit them if the fixture
scenarios need to change.
