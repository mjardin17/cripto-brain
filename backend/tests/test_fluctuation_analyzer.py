"""Tests for the fluctuation analyzer: attribution + next-fluctuation timing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.intelligence.council import convene_council, gather_domain_facts
from backend.intelligence.fluctuation_analyzer import (
    analyze_fluctuation_drivers,
    forecast_next_fluctuations,
)
from backend.intelligence.pattern_intelligence import record_event_reaction
from backend.main import create_app
from backend.storage.duckdb_store import DuckDBStore
from backend.tests.auth_helpers import authenticate_test_client


def _reaction(event_type: str, occurred_at: str, direction: str, magnitude: float) -> dict:
    return {
        "domain": "CRYPTO",
        "event_type": event_type,
        "event_label": f"{event_type} event",
        "occurred_at": occurred_at,
        "reaction_direction": direction,
        "reaction_magnitude_pct": magnitude,
        "narrative": "Recorded analog reaction for fluctuation analysis testing.",
    }


def test_drivers_attributed_and_ranked():
    store = DuckDBStore(db_path=":memory:")
    for i in range(4):
        record_event_reaction(store, **_reaction("NATURAL_DISASTER", f"2026-0{1 + i}-15T00:00:00+00:00", "DOWN", -6.0))
    record_event_reaction(store, **_reaction("EXCHANGE_OUTAGE", "2026-06-01T00:00:00+00:00", "DOWN", -12.0))

    analysis = analyze_fluctuation_drivers(store, domain="CRYPTO")
    assert analysis["recorded_reactions"] == 5
    assert analysis["top_driver"] == "NATURAL_DISASTER"
    shares = {d["event_type"]: d["impact_share"] for d in analysis["drivers"]}
    assert shares["NATURAL_DISASTER"] > shares["EXCHANGE_OUTAGE"]
    driver = next(d for d in analysis["drivers"] if d["event_type"] == "EXCHANGE_OUTAGE")
    assert driver["dominant_direction"] == "DOWN"
    assert driver["median_magnitude_pct"] == 12.0

    try:
        analyze_fluctuation_drivers(store, domain="FOREX")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_forecast_times_next_fluctuation():
    store = DuckDBStore(db_path=":memory:")
    # Four disasters, exactly 30 days apart, last one 10 days ago.
    last = datetime.now(timezone.utc) - timedelta(days=10)
    for i in range(4):
        ts = last - timedelta(days=30 * (3 - i))
        record_event_reaction(
            store,
            **_reaction("NATURAL_DISASTER", ts.isoformat(), "DOWN", -6.0),
        )

    fc = forecast_next_fluctuations(store, domain="CRYPTO", horizon_days=30)
    forecast = next(f for f in fc["forecasts"] if f["event_type"] == "NATURAL_DISASTER")
    assert forecast["insufficient_history"] is False
    assert forecast["median_interval_days"] == 30.0
    assert forecast["interval_consistency"] == 1.0
    assert forecast["expected_direction"] == "DOWN"
    # Next expected = last occurrence + 30 days = 20 days from now
    assert abs(forecast["days_until_expected"] - 20.0) < 1.5
    assert forecast["probability_within_horizon"] > 0.5
    assert forecast["confidence"] > 0.4


def test_forecast_fails_closed_without_history():
    store = DuckDBStore(db_path=":memory:")
    record_event_reaction(store, **_reaction("GEOPOLITICAL_SHOCK", "2026-01-01T00:00:00+00:00", "DOWN", -5.0))
    record_event_reaction(store, **_reaction("GEOPOLITICAL_SHOCK", "2026-02-01T00:00:00+00:00", "DOWN", -5.0))

    fc = forecast_next_fluctuations(store, domain="STOCKS", horizon_days=30)
    assert fc["forecasts"] == []  # reactions were in CRYPTO, not STOCKS

    fc_crypto = forecast_next_fluctuations(store, domain="CRYPTO", horizon_days=30)
    forecast = fc_crypto["forecasts"][0]
    assert forecast["insufficient_history"] is True
    assert "at least 3" in forecast["reason"]


def test_council_pattern_analyst_reports_timing():
    store = DuckDBStore(db_path=":memory:")
    last = datetime.now(timezone.utc) - timedelta(days=10)
    for i in range(4):
        ts = last - timedelta(days=30 * (3 - i))
        record_event_reaction(
            store,
            **_reaction("NATURAL_DISASTER", ts.isoformat(), "DOWN", -6.0),
        )

    facts = gather_domain_facts(store, "CRYPTO")
    assert facts["next_fluctuation"] is not None
    assert facts["next_fluctuation"]["event_type"] == "NATURAL_DISASTER"

    session = convene_council(store, "CRYPTO")
    stances = {m["member_id"]: m for m in session["member_stances"]}
    assert stances["pattern_analyst"]["stance"] == "GO"
    assert "Timing model" in stances["pattern_analyst"]["rationale"]


def test_fluctuation_endpoints():
    app = create_app(db_path=":memory:")
    with TestClient(app) as client:
        assert client.get("/api/fluctuations/analyze?domain=CRYPTO").status_code in (401, 403)
        assert client.get("/api/fluctuations/forecast?domain=CRYPTO").status_code in (401, 403)

        authenticate_test_client(client)
        assert client.get("/api/fluctuations/analyze?domain=FOREX").status_code == 400

        analysis = client.get("/api/fluctuations/analyze?domain=CRYPTO").json()
        assert analysis["recorded_reactions"] == 0
        assert "record" in analysis["reason"].lower()

        forecast = client.get("/api/fluctuations/forecast?domain=CRYPTO&horizon_days=45").json()
        assert forecast["horizon_days"] == 45.0
        assert forecast["forecasts"] == []
