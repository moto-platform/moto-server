from __future__ import annotations

import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moto_server import series
from moto_server.api import create_app
from moto_server.config import Settings
from moto_server.defs import vehicle_catalog
from moto_server.ingest import ingest_path

from .conftest import copy_fixture

AUTH = {"Authorization": "Bearer test-token"}
BASE_MS = 20_000  # after the fixture's own samples
LONG_SAMPLES = 200  # block_seq is uint8: 20 + 200 stays below the wrap


# --- downsample_minmax ---------------------------------------------------------------------


def test_downsample_is_noop_under_the_limit():
    times, values = list(range(30)), [float(i % 7) for i in range(30)]
    assert series.downsample_minmax(times, values, 30) == (times, values)
    assert series.downsample_minmax(times, values, 100) == (times, values)


@pytest.mark.parametrize("max_points", [10, 11, 50, 99])
def test_downsample_length_is_bounded(max_points: int):
    times = list(range(1000))
    values = [math.sin(i / 7) for i in times]
    out_t, out_v = series.downsample_minmax(times, values, max_points)
    assert 0 < len(out_t) <= max_points
    assert len(out_t) == len(out_v)


def test_downsample_keeps_a_single_spike_and_time_order():
    times = list(range(5000))
    values = [1.0] * 5000
    values[3217] = 500.0
    values[4100] = -80.0
    out_t, out_v = series.downsample_minmax(times, values, 20)
    assert 500.0 in out_v and -80.0 in out_v
    assert out_t[out_v.index(500.0)] == 3217
    assert out_t == sorted(out_t) and len(set(out_t)) == len(out_t)


def test_window_is_inclusive():
    times, values = [0, 10, 20, 30], [1.0, 2.0, 3.0, 4.0]
    assert series.window(times, values, 10, 20) == ([10, 20], [2.0, 3.0])
    assert series.window(times, values, None, None) == (times, values)


# --- endpoint ------------------------------------------------------------------------------


def _append_long_engine_speed(session: Path) -> None:
    """Adds LONG_SAMPLES ENGINE_SPEED rows (one 9000 rpm spike) to the signals.csv."""
    path = session / "signals.csv"
    lines = path.read_text().splitlines()
    rows = []
    for i in range(LONG_SAMPLES):
        t = BASE_MS + i * 10
        value = 9000.0 if i == 123 else 1000.0 + i
        rows.append(
            f"2026-01-15T16:01:00.000Z,{t},{20 + i},0,{t},1,0,62476,1,0,{int(value * 4)},"
            f"ENGINE_SPEED,{value}"
        )
    path.write_text("\n".join([*lines, *rows]) + "\n")


@pytest.fixture
def session_id(settings: Settings, fixtures_dir: Path, tmp_path: Path) -> str:
    session = copy_fixture(fixtures_dir / "v4_vsig_session", tmp_path / "long")
    _append_long_engine_speed(session)
    return ingest_path(session, settings).session_id


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _get(client: TestClient, session_id: str, **params):
    return client.get(f"/sessions/{session_id}/series", headers=AUTH, params=params)


def test_series_requires_token(client: TestClient, session_id: str):
    assert client.get(f"/sessions/{session_id}/series").status_code == 401
    # The token is accepted in the header only, never as a query parameter.
    assert client.get(f"/sessions/{session_id}/series?token=test-token").status_code == 401


@pytest.mark.parametrize("bad_id", ["00000000-000000-ffff", "not-a-session", "..%2f..%2fetc"])
def test_series_unknown_or_malformed_session_is_404(client: TestClient, bad_id: str):
    assert _get(client, bad_id).status_code == 404


def test_series_full_listing_uses_defs_units(client: TestClient, session_id: str):
    resp = _get(client, session_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert body["max_points"] == series.DEFAULT_MAX_POINTS
    by_name = {s["name"]: s for s in body["series"]}
    dids = [n for n, s in by_name.items() if s["kind"] == "did"]
    assert "did.ENGINE_SPEED" in dids and "did.VEHICLE_SPEED" in dids
    for name in dids:
        entry = by_name[name]
        assert entry["unit"] == vehicle_catalog.SIGNALS[name.removeprefix("did.")]["unit"]
        assert len(entry["t_ms"]) == len(entry["v"]) == entry["points_total"]
        assert entry["t_ms"] == sorted(entry["t_ms"])
    engine = by_name["did.ENGINE_SPEED"]
    assert engine["points_total"] > LONG_SAMPLES and engine["downsampled"] is False
    assert min(s["t_ms"][0] for s in body["series"]) == 0
    assert all(isinstance(t, int) for t in engine["t_ms"])


def test_series_signals_filter(client: TestClient, session_id: str):
    body = _get(client, session_id, signals="did.ENGINE_SPEED").json()
    assert [s["name"] for s in body["series"]] == ["did.ENGINE_SPEED"]


def test_series_t0_is_stable_between_filtered_and_unfiltered(client: TestClient, session_id: str):
    full = _get(client, session_id).json()
    one = _get(client, session_id, signals="did.ENGINE_SPEED").json()
    assert one["t0_device_ms"] == full["t0_device_ms"]
    full_engine = next(s for s in full["series"] if s["name"] == "did.ENGINE_SPEED")
    assert one["series"][0]["t_ms"] == full_engine["t_ms"]


def test_series_window_is_applied_before_downsampling(client: TestClient, session_id: str):
    full = _get(client, session_id, signals="did.ENGINE_SPEED").json()["series"][0]
    start = full["t_ms"][-LONG_SAMPLES]
    body = _get(
        client, session_id, signals="did.ENGINE_SPEED", from_ms=start, to_ms=start + 99
    ).json()
    win = body["series"][0]
    assert win["points_total"] == 10
    assert win["t_ms"][0] == start and win["t_ms"][-1] == start + 90
    assert win["downsampled"] is False


def test_series_downsamples_and_keeps_the_spike(client: TestClient, session_id: str):
    body = _get(client, session_id, signals="did.ENGINE_SPEED", max_points=40).json()
    engine = body["series"][0]
    assert engine["downsampled"] is True
    assert len(engine["v"]) <= 40 < engine["points_total"]
    assert 9000.0 in engine["v"]
    assert engine["t_ms"] == sorted(engine["t_ms"])


@pytest.mark.parametrize("name", ["did.NOPE", "gps_lat", "sensor.gps_latitude", "sensor.lat"])
def test_series_unknown_name_is_422_listing_it(client: TestClient, session_id: str, name: str):
    resp = _get(client, session_id, signals=f"did.ENGINE_SPEED,{name}")
    assert resp.status_code == 422
    assert name in resp.json()["detail"]
    assert "did.ENGINE_SPEED" not in resp.json()["detail"]


@pytest.mark.parametrize("max_points", [9, 20001, 0, -5])
def test_series_max_points_out_of_range_is_422(client: TestClient, session_id: str, max_points):
    assert _get(client, session_id, max_points=max_points).status_code == 422


# --- CORS ----------------------------------------------------------------------------------


def test_cors_header_for_configured_origin(settings: Settings):
    cors = Settings(
        settings.data_dir, settings.api_token, 100, cors_origins=("http://localhost:5173",)
    )
    client = TestClient(create_app(cors))
    origin = {"Origin": "http://localhost:5173"}
    resp = client.get("/health", headers=origin)
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in resp.headers
    pre = client.options(
        "/sessions",
        headers={
            **origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert pre.status_code == 200
    other = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_no_cors_header_when_unset(client: TestClient):
    resp = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in resp.headers
