"""Stage 3C Five Specialized Agents & Deterministic Role Handlers.

Roles:
- MARKET_INTELLIGENCE
- ARBITRAGE_RESEARCH
- PROBABILITY_RESEARCH
- RISK_INTELLIGENCE
- LEARNING

Safety Guarantees:
- Deterministic-first ($0 LLM cost, no external paid API dependency).
- Strict permission scopes controlled by the Supervisor.
- Zero live trading, wallet signing, deposits/withdrawals, or Risk Gate override ability.
- Tier 2 and Tier 3 remain INACTIVE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from backend.arbitrage.cross_venue import CrossVenueScanner
from backend.arbitrage.scanner import PolymarketArbitrageScanner
from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import StructuredMemoryManager
from backend.risk_gate.engine import DeterministicRiskGate
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

VALID_AGENT_ROLES = (
    "MARKET_INTELLIGENCE",
    "ARBITRAGE_RESEARCH",
    "PROBABILITY_RESEARCH",
    "RISK_INTELLIGENCE",
    "LEARNING",
)


@dataclass(frozen=True)
class AgentProfile:
    role: str
    agent_id: str
    description: str
    permitted_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    supports_task_types: tuple[str, ...]


FORBIDDEN_GLOBAL_ACTIONS = (
    "EXECUTE_LIVE_ORDER",
    "SIGN_WALLET_TRANSACTION",
    "WITHDRAW_OR_DEPOSIT_FUNDS",
    "OVERRIDE_RISK_GATE",
    "MODIFY_RISK_THRESHOLDS",
    "BYPASS_AUTHENTICATION",
    "BYPASS_RATE_LIMIT",
    "BYPASS_KILL_SWITCH",
    "PROMOTE_OWN_MEMORY",
    "ACTIVATE_TIER2_OR_TIER3",
)

AGENT_PROFILES: dict[str, AgentProfile] = {
    "MARKET_INTELLIGENCE": AgentProfile(
        role="MARKET_INTELLIGENCE",
        agent_id="market_intelligence_agent",
        description="Read-only Polymarket/Kalshi discovery, order-book ingestion, freshness checks, and feed anomaly recording.",
        permitted_actions=(
            "READ_POLYMARKET_PUBLIC",
            "READ_KALSHI_PUBLIC",
            "READ_DUCKDB",
            "WRITE_MEMORY_CANDIDATE",
            "BOUNDED_BACKOFF_RETRY",
            "REFRESH_MARKET_SNAPSHOT",
            "USE_VERIFIED_RESEARCH_USER_AGENT",
        ),
        forbidden_actions=FORBIDDEN_GLOBAL_ACTIONS,
        supports_task_types=("SCAN_POLYMARKET", "SCAN_CROSS_VENUE", "CHECK_FEED_HEALTH", "COUNCIL_SESSION"),
    ),
    "ARBITRAGE_RESEARCH": AgentProfile(
        role="ARBITRAGE_RESEARCH",
        agent_id="arbitrage_research_agent",
        description="Evaluates persisted intra-venue and cross-venue opportunities, settlement flags, and thin-leg depth limits.",
        permitted_actions=(
            "READ_DUCKDB",
            "ANALYZE_ARBITRAGE",
            "WRITE_MEMORY_CANDIDATE",
            "SUBMIT_LEARNING_CYCLE",
            "REDUCE_RESEARCH_SHARE_SIZE",
        ),
        forbidden_actions=FORBIDDEN_GLOBAL_ACTIONS,
        supports_task_types=("ARBITRAGE_ANALYSIS", "EVALUATE_OPPORTUNITY_DEPTH"),
    ),
    "PROBABILITY_RESEARCH": AgentProfile(
        role="PROBABILITY_RESEARCH",
        agent_id="probability_research_agent",
        description="Deterministic advisory probability calibration (Brier / complement midpoint checks). Strictly advisory; cannot authorize trades.",
        permitted_actions=(
            "READ_DUCKDB",
            "COMPUTE_BRIER_CALIBRATION",
            "WRITE_MEMORY_CANDIDATE",
            "SUBMIT_LEARNING_CYCLE",
        ),
        forbidden_actions=FORBIDDEN_GLOBAL_ACTIONS,
        supports_task_types=("CALIBRATE_PROBABILITY", "EVALUATE_FORECAST_CALIBRATION", "EVALUATE_EVENT_PATTERN"),
    ),
    "RISK_INTELLIGENCE": AgentProfile(
        role="RISK_INTELLIGENCE",
        agent_id="risk_intelligence_agent",
        description="Read-only risk audit analyst & independent reviewer for other agents' candidate lessons. Never weakens or overrides Risk Gate.",
        permitted_actions=(
            "READ_DUCKDB",
            "READ_RISK_AUDIT",
            "INDEPENDENT_MEMORY_REVIEW",
            "WRITE_MEMORY_CANDIDATE",
            "RECOMMEND_CONSERVATIVE_THROTTLE",
        ),
        forbidden_actions=FORBIDDEN_GLOBAL_ACTIONS,
        supports_task_types=("AUDIT_RISK_STATE", "REVIEW_CANDIDATE_LESSONS"),
    ),
    "LEARNING": AgentProfile(
        role="LEARNING",
        agent_id="learning_agent",
        description="Runs closed-loop learning consolidation, recurring mistake detection, regression verification, and retention sweeps.",
        permitted_actions=(
            "READ_DUCKDB",
            "INDEPENDENT_MEMORY_REVIEW",
            "RUN_REGRESSION_TESTS",
            "RUN_RETENTION_SWEEP",
            "WRITE_MEMORY_CANDIDATE",
            "SUBMIT_LEARNING_CYCLE",
        ),
        forbidden_actions=FORBIDDEN_GLOBAL_ACTIONS,
        supports_task_types=("CONSOLIDATE_LEARNING", "RUN_REGRESSION_SUITE", "RUN_RETENTION_SWEEP"),
    ),
}


@dataclass
class AgentExecutionContext:
    store: DuckDBStore
    risk_gate: DeterministicRiskGate
    memory: StructuredMemoryManager
    learning: LearningEngine
    scanner: PolymarketArbitrageScanner | None = None
    cross_scanner: CrossVenueScanner | None = None
    custom_handlers: dict[str, Callable[[dict[str, Any], dict[str, Any]], Any]] | None = None


async def execute_agent_task(
    task: dict[str, Any],
    ctx: AgentExecutionContext,
) -> dict[str, Any]:
    """Deterministically execute a leased task under strict role boundaries."""
    role = str(task["assigned_agent"]).upper()
    if role not in AGENT_PROFILES:
        raise ValueError(f"Unknown agent role '{role}'")
    profile = AGENT_PROFILES[role]
    task_type = str(task["task_type"]).upper()
    payload = dict(task.get("payload") or {})

    # Optional injected handler for deterministic testing & extensible tasks
    if ctx.custom_handlers and task_type in ctx.custom_handlers:
        fn = ctx.custom_handlers[task_type]
        out = fn(task, payload)
        if hasattr(out, "__await__"):
            out = await out
        return dict(out) if isinstance(out, dict) else {"value": out}

    # Explicit safety guard: reject any task payload requesting forbidden actions
    requested_action = str(payload.get("action") or "").upper()
    if requested_action in FORBIDDEN_GLOBAL_ACTIONS or payload.get("live_order") or payload.get("ai_override"):
        raise PermissionError(
            f"Safety block: role '{role}' forbidden from action '{requested_action or 'FORBIDDEN_ACTION'}'"
        )

    if role == "MARKET_INTELLIGENCE":
        if task_type == "SCAN_POLYMARKET":
            if ctx.scanner is None:
                raise RuntimeError("Polymarket scanner unavailable in execution context")
            limit = int(payload.get("limit", 6))
            target_shares = float(payload.get("target_shares", 25.0))
            scan_res = await ctx.scanner.scan(limit=limit, target_shares=target_shares)
            if scan_res.get("status") == "ERROR":
                raise ConnectionError(scan_res.get("error") or "Polymarket scan failed")
            ctx.memory.record_candidate(
                memory_tier="WORKING",
                category="MARKET_SCAN_NOTE",
                title=f"Polymarket scan {scan_res['scan_id']} summary",
                summary=f"Scanned {scan_res['markets_scanned']} markets; Risk Gate approved {scan_res['approved_count']}.",
                source_agent=profile.agent_id,
                evidence=[{"source_type": "POLY_SCAN", "source_ref_id": scan_res["scan_id"]}],
                confidence=0.80,
                ttl_hours=12.0,
            )
            return {
                "agent_id": profile.agent_id,
                "scan_id": scan_res["scan_id"],
                "markets_scanned": scan_res["markets_scanned"],
                "approved_count": scan_res["approved_count"],
                "live_trading_enabled": False,
            }

        if task_type == "CHECK_FEED_HEALTH":
            snaps = ctx.store.list_opportunities(limit=5, latest_scan_only=True)
            stale_count = sum(1 for o in snaps if int(o.get("data_age_ms", 0)) > 15_000 or int(o.get("data_age_ms", 0)) < 0)
            return {
                "agent_id": profile.agent_id,
                "recent_opportunities": len(snaps),
                "stale_count": stale_count,
                "checked_at": utc_now_iso(),
            }

        if task_type == "SCAN_CROSS_VENUE":
            pairs = ctx.store.list_cross_venue_pairs(limit=10)
            verified_pairs = [p for p in pairs if p.get("identical_settlement_verified")]
            return {
                "agent_id": profile.agent_id,
                "registered_pairs": len(pairs),
                "verified_pairs": len(verified_pairs),
                "note": "Settlement verification remains strictly fail-closed.",
            }

        if task_type == "COUNCIL_SESSION":
            from backend.intelligence.council import convene_council

            domain = str(payload.get("domain") or "").strip().upper()
            if not domain:
                raise ValueError("COUNCIL_SESSION payload requires a 'domain' (STOCKS or CRYPTO)")
            topic = payload.get("topic")
            session = convene_council(ctx.store, domain, topic=topic)
            ctx.memory.record_candidate(
                memory_tier="WORKING",
                category="COUNCIL_SESSION",
                title=f"{session['domain']} council verdict {session['verdict']}",
                summary=f"{session['verdict_detail']['rationale']}",
                source_agent=profile.agent_id,
                evidence=[{"source_type": "COUNCIL_SESSION", "source_ref_id": session["session_id"]}],
                confidence=0.75,
                ttl_hours=12.0,
            )
            return {
                "agent_id": profile.agent_id,
                "council": session["domain"],
                "session_id": session["session_id"],
                "verdict": session["verdict"],
                "confidence": session["confidence"],
                "member_stances": session["member_stances"],
                "advisory_only": True,
                "live_trading_enabled": False,
            }

    if role == "ARBITRAGE_RESEARCH":
        opps = ctx.store.list_opportunities(limit=50, latest_scan_only=False)
        approved = [o for o in opps if o.get("risk_gate_approved")]
        best_net = max((o.get("net_edge_pct") for o in approved if o.get("net_edge_pct") is not None), default=None)
        ctx.memory.record_candidate(
            memory_tier="EPISODIC",
            category="ARBITRAGE_DIGEST",
            title="Intra-venue arbitrage digest",
            summary=f"Evaluated {len(opps)} opportunities ({len(approved)} Risk Gate approved, best_net_edge={best_net}).",
            source_agent=profile.agent_id,
            evidence=[{"source_type": "DUCKDB_OPPORTUNITIES", "source_ref_id": opps[0]["opp_id"] if opps else "NONE"}],
            confidence=0.78,
        )
        return {
            "agent_id": profile.agent_id,
            "opportunities_reviewed": len(opps),
            "approved_count": len(approved),
            "best_net_edge_pct": best_net,
            "live_trading_enabled": False,
        }

    if role == "PROBABILITY_RESEARCH":
        if task_type == "EVALUATE_EVENT_PATTERN":
            from backend.intelligence.pattern_intelligence import (
                analyze_event_pattern,
                record_event_reaction,
            )

            action = str(payload.get("action") or "ANALYZE").upper()
            if action == "RECORD":
                reaction = record_event_reaction(
                    ctx.store,
                    domain=str(payload.get("domain") or ""),
                    event_type=str(payload.get("event_type") or ""),
                    event_label=str(payload.get("event_label") or ""),
                    occurred_at=str(payload.get("occurred_at") or ""),
                    reaction_direction=str(payload.get("reaction_direction") or ""),
                    narrative=str(payload.get("narrative") or ""),
                    asset_label=payload.get("asset_label"),
                    market_label=payload.get("market_label"),
                    reaction_magnitude_pct=payload.get("reaction_magnitude_pct"),
                    sources=payload.get("sources"),
                    keywords=payload.get("keywords"),
                )
                return {
                    "agent_id": profile.agent_id,
                    "recorded": True,
                    "reaction_id": reaction["reaction_id"],
                    "advisory_only": True,
                    "live_trading_enabled": False,
                }
            domain = str(payload.get("domain") or "").strip().upper()
            event_type = str(payload.get("event_type") or "").strip()
            if not domain or not event_type:
                raise ValueError("EVALUATE_EVENT_PATTERN payload requires 'domain' and 'event_type'")
            analysis = analyze_event_pattern(ctx.store, domain=domain, event_type=event_type)
            return {
                "agent_id": profile.agent_id,
                "pattern_analysis": analysis,
                "advisory_only": True,
                "live_trading_enabled": False,
            }

        # Deterministic advisory probability & Brier score evaluation ($0 LLM cost)
        forecasts = payload.get("forecasts") or [
            {"id": "CAL-DEFAULT-1", "subject_key": "binary_complement_sum", "predicted_prob": 0.50, "actual_outcome": 1}
        ]
        brier_scores: list[float] = []
        for fc in forecasts:
            p = max(0.0, min(1.0, float(fc.get("predicted_prob", 0.5))))
            y = 1 if int(fc.get("actual_outcome", 0)) == 1 else 0
            brier_scores.append(round((p - y) ** 2, 6))
        avg_brier = round(sum(brier_scores) / max(1, len(brier_scores)), 6)
        ctx.memory.record_candidate(
            memory_tier="AGENT_PERF",
            category="CALIBRATION_DIGEST",
            title=f"Probability calibration batch ({len(brier_scores)} samples)",
            summary=f"Advisory deterministic Brier average={avg_brier:.4f}. Advisory only; zero trade authority.",
            source_agent=profile.agent_id,
            evidence=[{"source_type": "CALIBRATION_BATCH", "source_ref_id": str(task["task_id"]), "avg_brier": avg_brier}],
            confidence=0.80,
        )
        return {
            "agent_id": profile.agent_id,
            "samples": len(brier_scores),
            "avg_brier": avg_brier,
            "advisory_only": True,
            "tier3_status": "INACTIVE",
        }

    if role == "RISK_INTELLIGENCE":
        audits = ctx.store.list_risk_audit(limit=50)
        denied = [a for a in audits if a.get("decision") == "DENIED"]
        paper_summary = ctx.store.get_paper_summary()
        kill_switch = ctx.store.is_kill_switch_active()
        # Promote eligible candidate entries authored by OTHER agents (never self-promotion)
        promoted_ids: list[str] = []
        if task_type == "REVIEW_CANDIDATE_LESSONS" or payload.get("review_candidates"):
            for cand in ctx.memory.list_entries(status="CANDIDATE", limit=10):
                if (
                    cand["source_agent"] != profile.agent_id
                    and len(cand["evidence"]) > 0
                    and float(cand["confidence"]) >= 0.65
                ):
                    try:
                        res = ctx.memory.verify_and_promote(
                            cand["memory_id"],
                            reviewer_id=profile.agent_id,
                            review_reason="Independent verification by Risk Intelligence",
                        )
                        promoted_ids.append(res["memory_id"])
                    except (ValueError, PermissionError):
                        continue
        return {
            "agent_id": profile.agent_id,
            "audits_inspected": len(audits),
            "denied_count": len(denied),
            "kill_switch_active": kill_switch,
            "daily_simulated_loss_usd": paper_summary["daily_simulated_loss_usd"],
            "promoted_memory_ids": promoted_ids,
            "risk_policy_immutable": True,
        }

    if role == "LEARNING":
        mistakes = ctx.learning.get_recurring_mistakes(min_occurrences=2)
        sweep = ctx.memory.run_retention_sweep(actor=profile.agent_id)
        reg_cases = ctx.learning.list_regression_cases(limit=25)
        return {
            "agent_id": profile.agent_id,
            "recurring_mistakes_count": len(mistakes),
            "archived_memories_count": sweep["archived_count"],
            "regression_cases_count": len(reg_cases),
        }

    raise ValueError(f"Task type '{task_type}' not supported for role '{role}'")
