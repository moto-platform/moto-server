"""Exact statistics endpoint: full-resolution values, units and privacy (SV-2)."""

from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from vehicle_server.api import create_app
from vehicle_server.config import Settings

AUTH = {"Authorization": "Bearer test-token"}
SESSION_ID = "20260115-120000-abcd"
BASE_MS = 5_000
STEP_MS = 10
COUNT = 20_001


def _write_derived_session(
    settings: Settings, session_id: str, times: list[int], values: list[float]
) -> None:
    parquet = settings.sessions_dir / session_id / "parquet"
    parquet.mkdir(parents=True)
    signals = ["test_signal"] * len(values) + ["gps_latitude"] * len(values)
    table = pa.table(
        {
            "device_time_ms": pa.array(times + times, type=pa.int64()),
            "signal": pa.array(signals, type=pa.string()),
            "value": pa.array(values + [1.0] * len(values), type=pa.float64()),
            "unit": pa.array(["u"] * (2 * len(values)), type=pa.string()),
            "valid": pa.array([True] * (2 * len(values)), type=pa.bool_()),
        }
    )
    pq.write_table(table, parquet / "derived.parquet")


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def long_session(settings: Settings) -> tuple[str, list[int], np.ndarray]:
    times = [BASE_MS + index * STEP_MS for index in range(COUNT)]
    values = np.sin(np.arange(COUNT) / 17.0) * 100.0 + np.arange(COUNT) % 13
    _write_derived_session(settings, SESSION_ID, times, [float(v) for v in values])
    return SESSION_ID, times, values


def _get(client: TestClient, session_id: str, **params):
    return client.get(f"/sessions/{session_id}/stats", headers=AUTH, params=params)


def test_stats_requires_token(client: TestClient, long_session):
    session_id, _, _ = long_session
    assert client.get(f"/sessions/{session_id}/stats").status_code == 401
    assert client.get(f"/sessions/{session_id}/stats?token=test-token").status_code == 401


def test_stats_are_exact_over_more_than_max_points(client: TestClient, long_session):
    session_id, times, values = long_session
    resp = _get(client, session_id, signals="derived.test_signal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert body["t0_device_ms"] == times[0]
    entry = body["series"][0]
    assert entry["name"] == "derived.test_signal"
    assert entry["unit"] == "u"
    assert entry["count"] == COUNT
    assert entry["count"] > 20_000
    assert entry["mean"] == pytest.approx(float(np.mean(values)), rel=1e-12, abs=1e-12)
    assert entry["std"] == pytest.approx(float(np.std(values)), rel=1e-12, abs=1e-12)
    assert entry["min"] == pytest.approx(float(np.min(values)), rel=1e-12, abs=1e-12)
    assert entry["max"] == pytest.approx(float(np.max(values)), rel=1e-12, abs=1e-12)
    for key, percent in (("p05", 5), ("p50", 50), ("p95", 95)):
        assert entry[key] == pytest.approx(
            float(np.percentile(values, percent)), rel=1e-12, abs=1e-12
        )
    assert entry["t_first_ms"] == 0
    assert entry["t_last_ms"] == times[-1] - times[0]


def test_stats_window_is_relative_to_t0(client: TestClient, long_session):
    session_id, times, values = long_session
    start_ms, end_ms = 1_000, 2_000
    resp = _get(
        client,
        session_id,
        signals="derived.test_signal",
        start_ms=start_ms,
        end_ms=end_ms,
    )
    assert resp.status_code == 200
    entry = resp.json()["series"][0]
    absolute = np.asarray(times)
    expected = values[(absolute >= times[0] + start_ms) & (absolute <= times[0] + end_ms)]
    assert entry["count"] == expected.size
    assert entry["mean"] == pytest.approx(float(np.mean(expected)), rel=1e-12, abs=1e-12)
    assert entry["t_first_ms"] == start_ms
    assert entry["t_last_ms"] == start_ms + 100 * STEP_MS


def test_stats_empty_window_has_zero_count_and_nulls(client: TestClient, long_session):
    session_id, _, _ = long_session
    resp = _get(
        client,
        session_id,
        signals="derived.test_signal",
        start_ms=10**9,
        end_ms=10**9 + 10,
    )
    assert resp.status_code == 200
    entry = resp.json()["series"][0]
    assert entry["count"] == 0
    for key in ("mean", "std", "min", "max", "p05", "p50", "p95", "t_first_ms", "t_last_ms"):
        assert entry[key] is None
    assert entry["unit"] == "u"


@pytest.mark.parametrize("name", ["derived.NOPE", "derived.gps_latitude"])
def test_stats_unknown_or_position_name_is_404(client: TestClient, long_session, name: str):
    session_id, _, _ = long_session
    resp = _get(client, session_id, signals=f"derived.test_signal,{name}")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert name in detail
    assert "derived.test_signal" not in detail


def test_stats_default_listing_filters_position(client: TestClient, long_session):
    session_id, _, _ = long_session
    resp = _get(client, session_id)
    assert resp.status_code == 200
    names = [entry["name"] for entry in resp.json()["series"]]
    assert "derived.test_signal" in names
    assert "derived.gps_latitude" not in names


@pytest.mark.parametrize("bad_id", ["00000000-000000-ffff", "not-a-session", "..%2f..%2fetc"])
def test_stats_unknown_or_malformed_session_is_404(client: TestClient, bad_id: str):
    assert _get(client, bad_id, signals="derived.test_signal").status_code == 404
