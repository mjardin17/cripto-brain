"""Tests for the curated real-event analog set and its import."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.intelligence.council import convene_council, gather_domain_facts
from backend.intelligence.curated_history import (
    CURATED_EVENT_REACTIONS,
    import_curated_history,
    validate_curated_history,
)
from backend.intelligence.fluctuation_analyzer import (
    analyze_fluctuation_drivers,
    forecast_next_fluctuations,
)
from backend.intelligence.pattern_intelligence import (
    predict_reaction_to_event,
    record_event_reaction,
)
from backend.main import create_app
from backend.storage.duckdb_store import DuckDBStore
from backend.tests.auth_helpers import authenticate_test_client


def test_curated_dataset_is_valid():
    assert validate_curated_history() == []
    domains = {str(e["domain"]).upper() for e in CURATED_EVENT_REACTIONS}
    assert domains == {"CRYPTO", "STOCKS"}
    crypto = [e for e in CURATED_EVENT_REACTIONS if e["domain"] == "CRYPTO"]
    stocks = [e for e in CURATED_EVENT_REACTIONS if e["domain"] == "STOCKS"]
    assert len(crypto) >= 9 and len(stocks) >= 4
    # The headline examples from the request are present and documented.
    labels = {e["event_label"] for e in CURATED_EVENT_REACTIONS}
    assert "COVID Black Thursday crash" in labels
    assert "FTX bankruptcy collapse" in labels


def test_import_is_idempotent_and_purges_test_data():
    store = DuckDBStore(db_path=":memory:")
    # Seed fake test analogs the import should clean up
    for i in range(2):
        record_event_reaction(
            store,
            domain="CRYPTO",
            event_type="NATURAL_DISASTER",
            event_label=f"Hurricane Cat-5 landfall {i + 1}",
            occurred_at="2026-01-15",
            reaction_direction="DOWN",
            reaction_magnitude_pct=-6.0,
            narrative="Fake seeded analog for cleanup testing.",
        )

    first = import_curated_history(store)
    assert first["imported"] == len(CURATED_EVENT_REACTIONS)
    assert first["skipped_duplicates"] == 0

    second = import_curated_history(store)
    assert second["imported"] == 0
    assert second["skipped_duplicates"] == len(CURATED_EVENT_REACTIONS)

    # Test analogs still present until the endpoint's purge step runs
    remaining = store.list_event_reactions(limit=1000)
    assert any("Hurricane Cat-5" in (r.get("event_label") or "") for r in remaining)
    purged = store.delete_event_reactions_by_label_prefix("Hurricane Cat-5 landfall")
    assert purged == 2
    remaining = store.list_event_reactions(limit=1000)
    assert not any("Hurricane Cat-5" in (r.get("event_label") or "") for r in remaining)


def test_curated_history_feeds_real_drivers_and_forecasts():
    store = DuckDBStore(db_path=":memory:")
    import_curated_history(store)

    crypto = analyze_fluctuation_drivers(store, domain="CRYPTO")
    types = {d["event_type"] for d in crypto["drivers"]}
    assert {"SYSTEMIC_SHOCK", "SUPPLY_HALVING", "POLICY_CATALYST"} <= types
    halving = next(d for d in crypto["drivers"] if d["event_type"] == "SUPPLY_HALVING")
    assert halving["dominant_direction"] == "UP"
    assert halving["analog_count"] == 4
    assert halving["base_rate"] == 1.0

    stocks = analyze_fluctuation_drivers(store, domain="STOCKS")
    assert stocks["recorded_reactions"] >= 4
    assert stocks["top_driver"] == "SYSTEMIC_SHOCK"

    # The halving rhythm: four events, ~4-year median interval, next ~2028
    fc = forecast_next_fluctuations(store, domain="CRYPTO", horizon_days=30)
    halving_fc = next(
        f for f in fc["forecasts"] if f["event_type"] == "SUPPLY_HALVING"
    )
    assert halving_fc["insufficient_history"] is False
    assert 1300 <= halving_fc["median_interval_days"] <= 1500
    assert halving_fc["next_expected_at"].startswith("2028")
    assert halving_fc["expected_direction"] == "UP"

    # Pattern prediction for a systemic shock analogs DOWN with high base rate
    prediction = predict_reaction_to_event(store, domain="CRYPTO", event_type="SYSTEMIC_SHOCK")
    assert prediction["prediction"]["direction"] == "DOWN"
    assert prediction["analog_count"] == 4
    assert prediction["base_rate"] == 1.0


def test_council_uses_curated_halving_pattern():
    store = DuckDBStore(db_path=":memory:")
    import_curated_history(store)

    facts = gather_domain_facts(store, "CRYPTO")
    assert facts["best_pattern_analog"]["event_type"] == "SUPPLY_HALVING"
    assert facts["next_fluctuation"]["event_type"] == "SUPPLY_HALVING"

    session = convene_council(store, "CRYPTO")
    stances = {m["member_id"]: m for m in session["member_stances"]}
    assert stances["pattern_analyst"]["stance"] == "GO"
    assert "SUPPLY_HALVING" in stances["pattern_analyst"]["rationale"]
    assert "2028" in stances["pattern_analyst"]["rationale"]


def test_import_curated_endpoint():
    app = create_app(db_path=":memory:")
    with TestClient(app) as client:
        assert client.post("/api/patterns/import-curated").status_code in (401, 403)
        authenticate_test_client(client)

        first = client.post("/api/patterns/import-curated").json()
        assert first["imported"] == len(CURATED_EVENT_REACTIONS)
        assert first["purged_test_analogs"] == 0

        listed = client.get("/api/patterns/events?domain=CRYPTO").json()
        assert listed["count"] >= 9

        analysis = client.get("/api/fluctuations/analyze?domain=CRYPTO").json()
        assert analysis["recorded_reactions"] >= 9
        driver_types = {d["event_type"] for d in analysis["drivers"]}
        assert {"SYSTEMIC_SHOCK", "SUPPLY_HALVING"} <= driver_types
