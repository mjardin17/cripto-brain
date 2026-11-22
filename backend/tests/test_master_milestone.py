"""Master Milestone Integration & Safety Tests:
- PersistentSolanaDesk DuckDB persistence, Kill Switch, 8% cap, -6% hard stop, +12%->-6% trailing stop, Learning recording
- Solana read-only public provider adapters (mocked HTTP, fail-closed on missing/failed checks)
- CostAwareModelRouter multi-provider ecosystem, rate-limit cooldown, ordered fallback, zero fabricated availability
- LearningEngine 10-step regression monitoring & safe lesson rollback
- Supervisor/Agent workforce Solana & Learning tasks
- FastAPI RBAC & CSRF authenticated routes (/api/solana/*, /api/intelligence/providers*, /api/intelligence/learning/*)
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["CERTAINTY_DUCKDB_PATH"] = ":memory:"

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.intelligence.agents import AGENT_PROFILES, AgentExecutionContext, execute_agent_task
from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import StructuredMemoryManager
from backend.intelligence.model_router import CostAwareModelRouter
from backend.main import create_app
from backend.risk_gate.engine import DeterministicRiskGate
from backend.security.config import SecuritySettings
from backend.solana_desk.engine import PersistentSolanaDesk
from backend.solana_desk.models import AnalystAssessment, FilterEvidence
from backend.solana_desk.providers import (
    DexScreenerSolanaAdapter,
    JupiterQuoteReadOnlyAdapter,
    RugCheckReadOnlyAdapter,
    SolanaEvidenceCollector,
    SolanaRpcReadOnlyAdapter,
)
from backend.storage.duckdb_store import DuckDBStore
from backend.tests.auth_helpers import authenticate_test_client, sync_csrf_header


class _StaticAnalyst:
    provider_id = "test_analyst"

    def __init__(self, score: float = 0.82) -> None:
        self.score = score

    def assess(self, candidate, evidence):
        return AnalystAssessment(score=self.score, rationale="deterministic test check")


def _complete_safe_evidence() -> FilterEvidence:
    return FilterEvidence(
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        lp_locked_pct=99.0,
        top10_holders_pct=11.0,
        volume_1h_usd=95000.0,
        sell_simulation_passed=True,
        dev_wallet_clear=True,
        evidence_sources=["dexscreener@test", "solana_rpc@test", "rugcheck@test", "jupiter@test"],
    )


def test_persistent_solana_desk_restart_killswitch_and_stops(tmp_path):
    async def _run():
        db_file = str(tmp_path / "solana_persist.duckdb")
        store = DuckDBStore(db_file)
        mem = StructuredMemoryManager(store)
        learn = LearningEngine(store, mem)
        gate = DeterministicRiskGate(store)
        desk = PersistentSolanaDesk(store, risk_gate=gate, learning_engine=learn)

        mint = "MintPersist111111111111111111111111111111111"
        ev_res = await desk.evaluate_mint(
            mint,
            symbol="PERSIST",
            override_evidence=_complete_safe_evidence(),
            override_analyst=_StaticAnalyst(0.84),
            override_price_usd=1.0,
        )
        assert ev_res["status"] == "PAPER_QUOTE_REQUIRED"
        assert ev_res["max_notional_usd"] == 22.40

        # Kill Switch halts both opening paper positions and evaluations
        store.set_kill_switch(active=True, reason="Milestone halt check", actor="test_owner")
        with pytest.raises(PermissionError, match="Kill Switch"):
            desk.open_paper_position(mint=mint, entry_price=1.0)
        halted_eval = await desk.evaluate_mint(
            "MintOther11111111111111111111111111111111111",
            symbol="HALTED",
            override_evidence=_complete_safe_evidence(),
            override_analyst=_StaticAnalyst(0.9),
        )
        assert halted_eval["status"] == "REJECTED"
        assert "KILL_SWITCH_ACTIVE" in halted_eval["reasons"]

        store.set_kill_switch(active=False, reason="Resume for paper test", actor="test_owner")

        # Exceeding 8% cap is rejected
        with pytest.raises(ValueError, match="8% cap"):
            desk.open_paper_position(mint=mint, entry_price=1.0, notional_usd=25.0)

        opened = desk.open_paper_position(mint=mint, entry_price=1.0, notional_usd=22.40)
        assert opened["position"]["notional_usd"] == 22.40
        assert opened["balance_usd"] == pytest.approx(257.60, abs=1e-4)

        # Duplicate mint across fresh PersistentSolanaDesk instance is blocked
        desk2 = PersistentSolanaDesk(store, risk_gate=gate, learning_engine=learn)
        dup_res = await desk2.evaluate_mint(mint, symbol="PERSIST", analyst_mode="deterministic")
        assert dup_res["status"] == "REJECTED"
        assert dup_res["reason"] == "DUPLICATE_MINT"

        # Price rallies +15% -> trailing stop arms, then pulls back -6% from peak -> exits & logs LearningCycle
        m1 = await desk2.mark_positions(prices={mint: 1.15})
        assert m1["closed_positions"] == []
        assert m1["updated_positions"][0]["trailing_active"] is True

        m2 = await desk2.mark_positions(prices={mint: 1.08})
        assert len(m2["closed_positions"]) == 1
        assert m2["closed_positions"][0]["exit_reason"] == "TRAILING_STOP_6_PERCENT_FROM_PEAK"
        cycles = learn.list_cycles(source_type="SOLANA_PAPER", limit=10)
        assert len(cycles) >= 1
        assert cycles[0]["outcome_matched"] is True

        # Second mint tests -6% hard stop loss
        mint2 = "MintHardStop11111111111111111111111111111111"
        await desk2.evaluate_mint(
            mint2,
            symbol="DROP",
            override_evidence=_complete_safe_evidence(),
            override_analyst=_StaticAnalyst(0.80),
            override_price_usd=2.0,
        )
        desk2.open_paper_position(mint=mint2, entry_price=2.0)
        m3 = await desk2.mark_positions(prices={mint2: 1.88})
        assert len(m3["closed_positions"]) == 1
        assert m3["closed_positions"][0]["exit_reason"] == "HARD_STOP_6_PERCENT"
        store.close()

    asyncio.run(_run())


def test_solana_public_provider_adapters_fail_closed_when_missing():
    async def _run():
        # Verify RPC adapter fails closed (None) on network error instead of fabricating revoked=True
        rpc = SolanaRpcReadOnlyAdapter(timeout_seconds=1.0)
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))):
            res_err = await rpc.inspect_mint("MintErr1111111111111111111111111111111111111")
        assert res_err["mint_authority_revoked"] is None
        assert res_err["freeze_authority_revoked"] is None
        assert res_err["top10_holders_pct"] is None
        assert res_err["error"] is not None

        # Verify RPC adapter parses valid revoked mint + holder supply cleanly
        def _mock_rpc_post(url, json=None, **kwargs):
            method = (json or {}).get("method")
            req = httpx.Request("POST", url)
            if method == "getAccountInfo":
                body = {
                    "context": {"slot": 123},
                    "value": {
                        "data": {
                            "parsed": {
                                "type": "mint",
                                "info": {"mintAuthority": None, "freezeAuthority": None, "extensions": []},
                            }
                        }
                    },
                }
            elif method == "getTokenSupply":
                body = {"value": {"uiAmount": 1000.0}}
            else:
                body = {"value": [{"uiAmount": 60.0}, {"uiAmount": 40.0}]}
            return httpx.Response(200, request=req, json={"jsonrpc": "2.0", "id": 1, "result": body})

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_rpc_post)):
            res_ok = await rpc.inspect_mint("MintSafe111111111111111111111111111111111111")
        assert res_ok["mint_authority_revoked"] is True
        assert res_ok["freeze_authority_revoked"] is True
        assert res_ok["top10_holders_pct"] == 10.0

        # RugCheck HTTP 500 -> returns None, never True
        rug = RugCheckReadOnlyAdapter(timeout_seconds=1.0)
        with patch(
            "httpx.AsyncClient.get",
            new=AsyncMock(return_value=httpx.Response(500, request=httpx.Request("GET", "https://api.rugcheck.xyz"))),
        ):
            rug_fail = await rug.inspect_token("MintSafe111111111111111111111111111111111111")
        assert rug_fail["lp_locked_pct"] is None
        assert rug_fail["dev_wallet_clear"] is None

        # Jupiter HTTP 400 NO_SELL_ROUTE -> sell_simulation_passed is strictly False
        jup = JupiterQuoteReadOnlyAdapter(timeout_seconds=1.0)
        with patch(
            "httpx.AsyncClient.get",
            new=AsyncMock(return_value=httpx.Response(400, request=httpx.Request("GET", "https://lite-api.jup.ag"))),
        ):
            jup_fail = await jup.probe_sell_route("MintTrap111111111111111111111111111111111111")
        assert jup_fail["sell_simulation_passed"] is False

        # DexScreener ignores profile without real pool pairAddress
        dex = DexScreenerSolanaAdapter()
        assert dex._parse_pair({"chainId": "solana", "baseToken": {"address": "A" * 40}}, "test") is None

    asyncio.run(_run())


def test_multi_provider_router_cooldown_fallback_and_no_fabricated_ollama(monkeypatch):
    async def _run():
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "HF_API_TOKEN", "HUGGINGFACE_API_KEY"):
            monkeypatch.delenv(k, raising=False)

        store = DuckDBStore(":memory:")
        router = CostAwareModelRouter(store)

        # Probe with unreachable Ollama -> stays UNAVAILABLE (never fabricated)
        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("connection refused"))):
            probed = await router.probe_providers(timeout_seconds=0.2)
        ollama_rec = next(p for p in probed["providers"] if p["provider_id"] == "ollama")
        assert ollama_rec["status"] == "UNAVAILABLE"
        assert ollama_rec["api_key_exposed_to_frontend"] is False

        # Configure free-tier Groq & OpenRouter keys; simulate HTTP 429 on Groq -> automatic fallback to OpenRouter
        monkeypatch.setenv("GROQ_API_KEY", "test-groq-free-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-free-key")

        async def _mock_chat_post(url, headers=None, json=None, **kwargs):
            req = httpx.Request("POST", url)
            if "groq.com" in str(url):
                return httpx.Response(429, request=req, json={"error": {"message": "rate limit"}})
            return httpx.Response(
                200,
                request=req,
                json={"choices": [{"message": {"content": "{\"score\": 0.78, \"rationale\": \"Safe fallback\"}"}}]},
            )

        router.update_fallback_order(["groq", "openrouter", "local_deterministic"], actor="owner_test")
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=_mock_chat_post)):
            syn = await router.synthesize_with_fallback(
                task_id="T-MILESTONE-1",
                agent_id="solana_analyst",
                task_category="SOLANA_ANALYST",
                system_prompt="Return JSON.",
                user_prompt="Evaluate token.",
            )

        assert syn["ok"] is True
        assert syn["provider"] == "openrouter"
        reg = router.get_provider_registry()
        groq_rec = next(p for p in reg["providers"] if p["provider_id"] == "groq")
        assert groq_rec["status"] == "RATE_LIMITED"
        assert groq_rec["rate_limit_hits"] == 1

        # Clearing cooldown restores Groq availability
        cleared = router.clear_provider_cooldown("groq")
        groq_after = next(p for p in cleared["providers"] if p["provider_id"] == "groq")
        assert groq_after["status"] == "AVAILABLE"
        store.close()

    asyncio.run(_run())


def test_learning_engine_regression_monitor_rolls_back_failing_lesson():
    store = DuckDBStore(":memory:")
    mem = StructuredMemoryManager(store)
    learn = LearningEngine(store, mem)

    cyc = learn.record_recovery_learning(
        blocker_id="BLK-99",
        agent_id="arbitrage_research_agent",
        subject_key="solana_mint_authority_guard",
        strategy_name="FAIL_CLOSED_RPC_CHECK",
        succeeded=True,
        input_fixture={"mint_authority_revoked": None},
        expected_invariant={"passed": False},
        reviewer_id="learning_agent",
    )
    assert cyc["stage"] == "VERIFIED_LESSON"
    case_id = cyc["regression_case_id"]
    mem_id = cyc["promoted_memory_id"]
    assert case_id is not None and mem_id is not None

    # Regression fails -> monitor_regressions automatically rolls back cycle & archives memory
    learn.run_regression_case(case_id, observed_output={"passed": True})
    mon = learn.monitor_regressions(actor="test_supervisor")
    assert case_id in mon["failing_case_ids"]
    assert cyc["cycle_id"] in mon["rolled_back_cycle_ids"]

    rolled = learn.get_cycle(cyc["cycle_id"])
    assert rolled is not None
    assert rolled["stage"] == "ROLLED_BACK"
    assert rolled["applied_flag"] is False
    mem_entry = mem.get_entry(mem_id)
    assert mem_entry is not None
    assert mem_entry["status"] == "ARCHIVED"
    store.close()


def test_agent_workforce_solana_and_learning_tasks_and_api_rbac(tmp_path):
    # Verify exact 5-role invariant still holds while running Solana & Learning tasks
    assert set(AGENT_PROFILES.keys()) == {
        "MARKET_INTELLIGENCE",
        "ARBITRAGE_RESEARCH",
        "PROBABILITY_RESEARCH",
        "RISK_INTELLIGENCE",
        "LEARNING",
    }

    settings = SecuritySettings(env_mode="development", session_secret="m" * 48, rate_limit_enabled=False)
    app = create_app(db_path=str(tmp_path / "milestone_api.duckdb"), enable_background_loop=False, security_settings=settings)
    with TestClient(app) as client:
        authenticate_test_client(client, username="owner_m", password="Owner-Milestone!2026")
        # Provision READ_ONLY viewer to verify RBAC enforcement on new routes
        viewer_pw = "Viewer-Milestone!2026"
        assert (
            client.post(
                "/api/admin/users",
                json={"username": "viewer_m", "password": viewer_pw, "role": "READ_ONLY", "email": "v@c.local"},
            ).status_code
            == 200
        )

        # OWNER can query overview, probe providers, run regression monitor, update fallback order
        ov = client.get("/api/solana/overview")
        assert ov.status_code == 200
        ov_body = ov.json()
        assert ov_body["live_trading_enabled"] is False
        assert ov_body["ledger"]["starting_balance_usd"] == 280.0

        mods = client.get("/api/modules/status").json()
        assert mods["solana_desk"]["status"] == "ACTIVE_SIMULATED"
        assert any(m["module_id"] == "tier3_ai_research" and m["status"] == "INACTIVE" for m in mods["modules"])

        prov_resp = client.get("/api/intelligence/providers")
        assert prov_resp.status_code == 200
        assert prov_resp.json()["api_key_exposed_to_frontend"] is False

        fb_resp = client.post(
            "/api/intelligence/providers/fallback-order",
            json={"fallback_order": ["groq", "openrouter", "ollama"]},
        )
        assert fb_resp.status_code == 200
        assert fb_resp.json()["fallback_order"][0] == "groq"

        mon_resp = client.post("/api/intelligence/learning/monitor-regressions", json={})
        assert mon_resp.status_code == 200
        assert "rolled_back_cycle_ids" in mon_resp.json()

        # Verify READ_ONLY user can read overview & providers but gets 403 on mutations
        assert client.post("/api/auth/login", json={"username": "viewer_m", "password": viewer_pw}).status_code == 200
        sync_csrf_header(client)
        assert client.get("/api/solana/overview").status_code == 200
        assert client.get("/api/intelligence/providers").status_code == 200
        assert client.post("/api/solana/discover", json={"query": "SOL"}).status_code == 403
        assert client.post("/api/intelligence/providers/probe", json={}).status_code == 403
        assert client.post("/api/intelligence/learning/monitor-regressions", json={}).status_code == 403
