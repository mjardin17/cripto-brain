"""Tests for event-pattern intelligence: historical analogs feed better predictions."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from backend.intelligence.agents import AGENT_PROFILES, AgentExecutionContext, execute_agent_task
from backend.intelligence.council import convene_council, gather_domain_facts
from backend.intelligence.pattern_intelligence import (
    analyze_event_pattern,
    predict_reaction_to_event,
    record_event_reaction,
)
from backend.main import create_app
from backend.storage.duckdb_store import DuckDBStore
from backend.tests.auth_helpers import authenticate_test_client


def _disaster_reaction(idx: int, direction: str, magnitude: float) -> dict:
    return {
        "domain": "CRYPTO",
        "event_type": "NATURAL_DISASTER",
        "event_label": f"Hurricane Cat-5 landfall {idx}",
        "occurred_at": f"2026-0{1 + (idx % 9)}-15T00:00:00+00:00",
        "reaction_direction": direction,
        "reaction_magnitude_pct": magnitude,
        "narrative": f"Major hurricane made landfall; crypto markets reacted as recorded in analog set {idx}.",
        "asset_label": "BTC",
        "market_label": "Polymarket BTC markets",
    }


def test_record_and_list_event_reactions():
    store = DuckDBStore(db_path=":memory:")
    r1 = record_event_reaction(store, **_disaster_reaction(1, "DOWN", -8.2))
    record_event_reaction(store, **_disaster_reaction(2, "DOWN", -5.1))
    record_event_reaction(store, **_disaster_reaction(3, "DOWN", -3.4))

    items = store.list_event_reactions(domain="CRYPTO", event_type="NATURAL_DISASTER", limit=10)
    assert len(items) == 3
    assert r1["reaction_id"] in {i["reaction_id"] for i in items}

    # Validation is fail-closed
    for bad in (
        {"domain": "FOREX", **{k: v for k, v in _disaster_reaction(9, "DOWN", -1.0).items() if k != "domain"}},
        {"reaction_direction": "SIDEWAYS", **{k: v for k, v in _disaster_reaction(9, "DOWN", -1.0).items() if k != "reaction_direction"}},
        {"reaction_magnitude_pct": -600.0, **{k: v for k, v in _disaster_reaction(9, "DOWN", -1.0).items() if k != "reaction_magnitude_pct"}},
    ):
        try:
            record_event_reaction(store, **bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_pattern_prediction_uses_analog_base_rate():
    store = DuckDBStore(db_path=":memory:")
    record_event_reaction(store, **_disaster_reaction(1, "DOWN", -8.2))
    record_event_reaction(store, **_disaster_reaction(2, "DOWN", -5.1))
    record_event_reaction(store, **_disaster_reaction(3, "DOWN", -3.4))
    record_event_reaction(store, **_disaster_reaction(4, "UP", 2.0))

    analysis = analyze_event_pattern(store, domain="CRYPTO", event_type="NATURAL_DISASTER")
    assert analysis["predictable"] is True
    assert analysis["analog_count"] == 4
    assert analysis["dominant_direction"] == "DOWN"
    assert analysis["base_rate"] == 0.75
    assert analysis["median_magnitude_pct"] == -4.25

    prediction = predict_reaction_to_event(
        store, domain="CRYPTO", event_type="NATURAL_DISASTER", description="Upcoming Cat-5 hurricane"
    )
    assert prediction["prediction"]["direction"] == "DOWN"
    assert prediction["prediction"]["expected_magnitude_pct"] == -4.25
    assert prediction["advisory_only"] is True


def test_pattern_fails_closed_without_analogs():
    store = DuckDBStore(db_path=":memory:")
    analysis = analyze_event_pattern(store, domain="STOCKS", event_type="GEOPOLITICAL_SHOCK")
    assert analysis["predictable"] is False
    assert analysis["analog_count"] == 0
    prediction = predict_reaction_to_event(store, domain="STOCKS", event_type="GEOPOLITICAL_SHOCK")
    assert "prediction" not in prediction


def test_council_pattern_analyst_stance():
    store = DuckDBStore(db_path=":memory:")
    # Without analogs the Pattern Analyst abstains
    facts = gather_domain_facts(store, "CRYPTO")
    assert facts["best_pattern_analog"] is None
    session = convene_council(store, "CRYPTO")
    stances = {m["member_id"]: m for m in session["member_stances"]}
    assert stances["pattern_analyst"]["stance"] == "ABSTAIN"

    # With a strong analog base rate the Pattern Analyst backs GO
    for i in range(4):
        record_event_reaction(store, **_disaster_reaction(i + 1, "DOWN", -6.0 + i))
    facts = gather_domain_facts(store, "CRYPTO")
    assert facts["best_pattern_analog"]["base_rate"] == 1.0
    session = convene_council(store, "CRYPTO")
    stances = {m["member_id"]: m for m in session["member_stances"]}
    assert stances["pattern_analyst"]["stance"] == "GO"
    assert "NATURAL_DISASTER" in stances["pattern_analyst"]["rationale"]


def test_event_pattern_agent_task():
    store = DuckDBStore(db_path=":memory:")
    for i in range(3):
        record_event_reaction(store, **_disaster_reaction(i + 1, "DOWN", -5.0))
    ctx = AgentExecutionContext(store=store, risk_gate=None, memory=None, learning=None, scanner=None, cross_scanner=None)

    out = asyncio.run(
        execute_agent_task(
            {
                "task_id": "T1",
                "assigned_agent": "PROBABILITY_RESEARCH",
                "task_type": "EVALUATE_EVENT_PATTERN",
                "payload": {"domain": "CRYPTO", "event_type": "NATURAL_DISASTER"},
            },
            ctx,
        )
    )
    assert out["pattern_analysis"]["analog_count"] == 3
    assert out["advisory_only"] is True
    assert "EVALUATE_EVENT_PATTERN" in AGENT_PROFILES["PROBABILITY_RESEARCH"].supports_task_types


def test_pattern_endpoints_record_and_predict():
    app = create_app(db_path=":memory:")
    with TestClient(app) as client:
        # Recording and predicting require auth
        assert client.post("/api/patterns/events", json={}).status_code in (401, 403)
        assert client.post("/api/patterns/predict", json={}).status_code in (401, 403)
        assert client.get("/api/patterns/events").status_code in (401, 403)

        authenticate_test_client(client)
        for i in range(3):
            res = client.post(
                "/api/patterns/events",
                json={
                    **_disaster_reaction(i + 1, "DOWN", -5.0),
                    "sources": ["https://example.com/analogs"],
                    "keywords": ["hurricane", "disaster"],
                },
            )
            assert res.status_code == 200

        bad = client.post(
            "/api/patterns/events",
            json={"domain": "FOREX", "event_type": "X", "event_label": "x", "occurred_at": "2026-01-01",
                  "reaction_direction": "UP", "narrative": "too short"},
        )
        assert bad.status_code in (400, 422)

        listed = client.get("/api/patterns/events?domain=CRYPTO").json()
        assert listed["count"] == 3

        analysis = client.get("/api/patterns/analyze?domain=CRYPTO&event_type=NATURAL_DISASTER").json()
        assert analysis["analog_count"] == 3
        assert analysis["dominant_direction"] == "DOWN"

        pred = client.post(
            "/api/patterns/predict",
            json={"domain": "CRYPTO", "event_type": "NATURAL_DISASTER", "description": "Upcoming landfall"},
        )
        assert pred.status_code == 200
        body = pred.json()
        assert body["prediction"]["direction"] == "DOWN"
        assert body["recorded_prediction_id"].startswith("PRED-")

        # The pattern call is tracked as an OPEN prediction for Brier scoring
        preds = client.get("/api/intelligence/predictions?agent_id=pattern_intelligence_agent").json()
        assert preds["count"] >= 1
        assert preds["items"][0]["market_category"] == "event_pattern"
        assert preds["items"][0]["status"] == "OPEN"
