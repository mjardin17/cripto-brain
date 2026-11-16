"""Tests for the STOCKS and CRYPTO council deliberation teams."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.intelligence.agents import AGENT_PROFILES, AgentExecutionContext
from backend.intelligence.council import (
    COUNCIL_DOMAINS,
    gather_domain_facts,
    get_council_definition,
    convene_council,
)
from backend.main import create_app
from backend.storage.duckdb_store import DuckDBStore
from backend.tests.auth_helpers import authenticate_test_client


def _make_store() -> DuckDBStore:
    return DuckDBStore(db_path=":memory:")


def _opp(opp_id: str, question: str, *, approved: bool, net_edge: float, age_ms: int = 100):
    return {
        "opp_id": opp_id,
        "scan_id": "SC-1",
        "market_id": f"M-{opp_id}",
        "condition_id": f"C-{opp_id}",
        "question": question,
        "slug": f"slug-{opp_id}",
        "venue": "polymarket",
        "target_shares": 25.0,
        "yes_vwap": 0.5,
        "no_vwap": 0.5,
        "combined_raw_cost": 1.0,
        "taker_fees_usd": 0.1,
        "slippage_buffer_usd": 0.05,
        "total_executable_cost": 1.15,
        "guaranteed_payout": 25.0,
        "gross_edge_pct": 2.5,
        "net_edge_pct": net_edge,
        "liquidity_participation_pct": 0.02,
        "leg_imbalance_ratio": 1.1,
        "executable": True,
        "risk_gate_approved": approved,
        "risk_gate_reason": "ok" if approved else "DENIED",
        "data_age_ms": age_ms,
        "evaluated_at": "2026-11-16T00:00:00+00:00",
    }


def test_council_definitions_and_domain_filtering():
    assert COUNCIL_DOMAINS == ("STOCKS", "CRYPTO")
    for d in COUNCIL_DOMAINS:
        c = get_council_definition(d)
        assert len(c["members"]) == 5
        assert any(m["member_id"] == "risk_sentinel" for m in c["members"])
        assert c["advisory_only"] is True
    try:
        get_council_definition("FOREX")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_convene_holds_with_no_domain_data():
    store = _make_store()
    session = convene_council(store, "CRYPTO")
    assert session["verdict"] == "HOLD"
    assert session["key_metrics"]["opportunities_total"] == 0
    assert session["member_stances"][0]["stance"] in ("HOLD",)
    assert session["advisory_only"] is True


def test_crypto_council_reaches_go_on_approved_edge():
    store = _make_store()
    store.record_opportunity(_opp("O1", "Will Bitcoin close above $100k in December?", approved=True, net_edge=3.1))
    store.record_opportunity(_opp("O2", "Will NVDA beat earnings?", approved=False, net_edge=-1.0))

    facts = gather_domain_facts(store, "CRYPTO")
    assert facts["opportunities_total"] == 1
    assert facts["approved_count"] == 1

    session = convene_council(store, "CRYPTO")
    assert session["verdict"] == "GO"
    stances = {m["member_id"]: m["stance"] for m in session["member_stances"]}
    assert stances["edge_analyst"] == "GO"
    assert stances["risk_sentinel"] == "GO"

    # The stock question must not leak into the crypto council
    stock_facts = gather_domain_facts(store, "STOCKS")
    assert stock_facts["opportunities_total"] == 1
    stock_session = convene_council(store, "STOCKS")
    assert stock_session["verdict"] == "HOLD"


def test_risk_sentinel_veto_downgrades_go_votes():
    store = _make_store()
    store.record_opportunity(
        _opp("O1", "Will Ethereum merge 2.0 ship?", approved=True, net_edge=2.5, age_ms=60_000)
    )
    session = convene_council(store, "CRYPTO")
    assert session["verdict"] == "HOLD"
    stances = {m["member_id"]: m for m in session["member_stances"]}
    assert stances["risk_sentinel"]["stance"] == "NO"
    assert "Revised" in stances["edge_analyst"]["rationale"]
    assert stances["edge_analyst"]["stance"] == "HOLD"


def test_sessions_persist_and_list_by_domain():
    store = _make_store()
    convene_council(store, "STOCKS", topic="Weekly review")
    convene_council(store, "CRYPTO")
    crypto = store.list_council_sessions(domain="CRYPTO", limit=10)
    stocks = store.list_council_sessions(domain="STOCKS", limit=10)
    assert len(crypto) == 1 and len(stocks) == 1
    assert stocks[0]["topic"] == "Weekly review"
    assert crypto[0]["member_stances"][0]["member_id"] == "edge_analyst"
    assert crypto[0]["verdict_detail"]["verdict"] == "HOLD"


def test_council_session_agent_task():
    store = _make_store()
    store.record_opportunity(_opp("O1", "Will the S&P 500 end the year above 6000?", approved=True, net_edge=2.8))

    ctx = AgentExecutionContext(store=store, risk_gate=None, memory=None, learning=None, scanner=None, cross_scanner=None)
    task = {
        "task_id": "T1",
        "assigned_agent": "MARKET_INTELLIGENCE",
        "task_type": "COUNCIL_SESSION",
        "payload": {"domain": "STOCKS"},
    }

    class _Mem:
        def record_candidate(self, **kwargs):
            self.captured = kwargs

    ctx.memory = _Mem()
    import asyncio

    out = asyncio.run(_run_council_task(task, ctx))
    assert out["council"] == "STOCKS"
    assert out["verdict"] == "GO"
    assert out["advisory_only"] is True
    assert "COUNCIL_SESSION" in AGENT_PROFILES["MARKET_INTELLIGENCE"].supports_task_types


async def _run_council_task(task, ctx):
    from backend.intelligence.agents import execute_agent_task

    return await execute_agent_task(task, ctx)


def test_council_endpoints():
    app = create_app(db_path=":memory:")
    with TestClient(app) as client:
        # Convene requires an authenticated operator
        assert client.post("/api/councils/CRYPTO/convene", json={}).status_code in (401, 403)
        assert client.get("/api/councils").status_code in (401, 403)

        authenticate_test_client(client)
        councils = client.get("/api/councils").json()["councils"]
        assert [c["domain"] for c in councils] == ["STOCKS", "CRYPTO"]

        assert client.get("/api/councils/FOREX/sessions").status_code == 404
        assert client.post("/api/councils/FOREX/convene", json={}).status_code == 404

        res = client.post("/api/councils/CRYPTO/convene", json={"topic": "Weekly review"})
        assert res.status_code == 200
        body = res.json()
        assert body["domain"] == "CRYPTO"
        assert body["verdict"] == "HOLD"
        assert len(body["member_stances"]) == 5
        assert body["advisory_only"] is True

        sessions = client.get("/api/councils/CRYPTO/sessions?limit=5").json()
        assert sessions["count"] == 1
        assert sessions["items"][0]["session_id"] == body["session_id"]
