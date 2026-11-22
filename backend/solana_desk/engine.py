"""Six-stage paper-only orchestration with explicit, auditable handoffs."""
from __future__ import annotations
import math
from typing import Protocol
from .filters import evaluate_candidate
from .models import AnalystAssessment, Candidate, DeskConfig, DeskEvent, FilterEvidence, PaperPosition
from .risk import calculate_position_notional, update_position_risk

class Analyst(Protocol):
    def assess(self, candidate: Candidate, evidence: FilterEvidence) -> AnalystAssessment: ...

class SolanaPaperDesk:
    """No wallet, private key, Jupiter swap, or live execution path exists here."""
    def __init__(self, config: DeskConfig | None = None) -> None:
        self.config = config or DeskConfig()
        self.balance_usd = self.config.starting_balance_usd
        self.positions: dict[str, PaperPosition] = {}
        self.events: list[DeskEvent] = []
        self._seen_mints: set[str] = set()
        self._entered_mints: set[str] = set()
        self._paper_quote_approved: set[str] = set()
        self.live_trading_enabled = False

    def _log(self, agent: str, event: str, mint: str | None = None, **details) -> None:
        self.events.append(DeskEvent(agent=agent, event=event, mint=mint, details=details))

    def process_candidate(self, candidate: Candidate, evidence: FilterEvidence, analyst: Analyst) -> dict:
        self._log("SCOUT", "CANDIDATE_DISCOVERED", candidate.mint, symbol=candidate.symbol)
        if candidate.mint in self._seen_mints:
            self._log("FILTER", "REJECT_DUPLICATE_MINT", candidate.mint)
            return {"status": "REJECTED", "reason": "DUPLICATE_MINT"}
        decision = evaluate_candidate(candidate, evidence, self.config)
        if not decision.passed:
            self._log("FILTER", "REJECTED", candidate.mint, reasons=decision.rejection_reasons)
            return {"status": "REJECTED", "agent": "FILTER", "reasons": decision.rejection_reasons}
        self._log("FILTER", "PASSED", candidate.mint)
        try:
            assessment = analyst.assess(candidate, evidence)
            if not math.isfinite(assessment.score) or not 0 <= assessment.score <= 1:
                raise ValueError("Analyst score outside [0, 1]")
        except Exception as exc:
            self._log("ANALYST", "FAILED_CLOSED", candidate.mint, error_type=type(exc).__name__)
            return {"status": "REJECTED", "agent": "ANALYST", "reason": "UNVERIFIED_ANALYST_RESULT"}
        self._log("ANALYST", "ASSESSED", candidate.mint, score=assessment.score,
                  rationale=assessment.rationale, evidence_ids=assessment.evidence_ids)
        if assessment.score < self.config.min_analyst_score:
            self._log("EXECUTOR", "NO_TRADE_SCORE_BELOW_THRESHOLD", candidate.mint, score=assessment.score)
            return {"status": "NO_TRADE", "score": assessment.score}
        if candidate.mint in self._seen_mints or candidate.mint in self.positions:
            self._log("EXECUTOR", "NO_TRADE_ALREADY_ENTERED", candidate.mint)
            return {"status": "NO_TRADE", "reason": "ONE_ENTRY_PER_MINT"}
        notional = calculate_position_notional(self.balance_usd, self.config)
        if notional <= 0 or notional > self.balance_usd:
            self._log("EXECUTOR", "NO_TRADE_INSUFFICIENT_BALANCE", candidate.mint)
            return {"status": "NO_TRADE", "reason": "INSUFFICIENT_BALANCE"}
        self._seen_mints.add(candidate.mint)
        self._paper_quote_approved.add(candidate.mint)
        self._log("EXECUTOR", "APPROVED_FOR_PAPER_QUOTE", candidate.mint,
                  max_notional_usd=notional, live_trading_enabled=False)
        self._log("LOGGER", "CANDIDATE_PIPELINE_COMPLETE", candidate.mint)
        return {"status": "PAPER_QUOTE_REQUIRED", "max_notional_usd": notional,
                "score": assessment.score, "live_trading_enabled": False}

    def open_paper_position(self, candidate: Candidate, entry_price: float, notional_usd: float) -> PaperPosition:
        if self.live_trading_enabled:
            raise RuntimeError("Live trading is forbidden in this module")
        if candidate.mint not in self._paper_quote_approved:
            raise ValueError("Candidate has not passed the complete paper authorization pipeline")
        if candidate.mint in self._entered_mints or candidate.mint in self.positions:
            raise ValueError("Only one entry per mint is permitted")
        max_notional = calculate_position_notional(self.balance_usd, self.config)
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("Entry price must be finite and positive")
        if not math.isfinite(notional_usd) or not 0 < notional_usd <= max_notional:
            raise ValueError("Paper notional exceeds the strict 8% balance cap")
        if notional_usd > self.balance_usd:
            raise ValueError("Insufficient paper balance")
        position = PaperPosition(mint=candidate.mint, symbol=candidate.symbol, entry_price=entry_price,
            mark_price=entry_price, peak_price=entry_price, notional_usd=notional_usd)
        self.balance_usd = round(self.balance_usd - notional_usd, 2)
        self.positions[candidate.mint] = position
        self._seen_mints.add(candidate.mint)
        self._entered_mints.add(candidate.mint)
        self._log("EXECUTOR", "PAPER_BUY_FILLED", candidate.mint, entry_price=entry_price, notional_usd=notional_usd)
        return position

    def mark_positions(self, prices: dict[str, float]) -> list[dict]:
        outcomes: list[dict] = []
        for mint, position in list(self.positions.items()):
            mark_price = prices.get(mint)
            if mark_price is None or not math.isfinite(mark_price) or mark_price <= 0:
                self._log("RISK", "POSITION_MARK_UNAVAILABLE", mint)
                continue
            updated, exit_reason = update_position_risk(position, mark_price, self.config)
            if exit_reason is None:
                self.positions[mint] = updated
                continue
            pnl_fraction = (updated.mark_price - updated.entry_price) / updated.entry_price
            exit_value = updated.notional_usd * (1.0 + pnl_fraction)
            self.balance_usd = round(self.balance_usd + max(0.0, exit_value), 2)
            del self.positions[mint]
            self._log("RISK", "PAPER_POSITION_CLOSED", mint, reason=exit_reason,
                      pnl_fraction=pnl_fraction, exit_value_usd=exit_value)
            outcomes.append({"mint": mint, "reason": exit_reason, "pnl_fraction": pnl_fraction})
        return outcomes


class PersistentSolanaDesk:
    """DuckDB-persisted Solana Research Desk integrated with Risk Gate, Kill Switch, ModelRouter, and LearningEngine.

    Strictly SIMULATED paper ledger ($280 virtual starting balance, 8% max single-mint cap).
    live_trading_enabled is permanently False; zero private keys or wallet signers exist.
    """

    def __init__(
        self,
        store: Any,
        *,
        collector: Any | None = None,
        model_router: Any | None = None,
        learning_engine: Any | None = None,
        risk_gate: Any | None = None,
        config: DeskConfig | None = None,
    ) -> None:
        from backend.solana_desk.providers import SolanaEvidenceCollector

        self.store = store
        self.config = config or DeskConfig()
        self.collector = collector or SolanaEvidenceCollector()
        self.model_router = model_router
        self.learning = learning_engine
        self.risk_gate = risk_gate
        self.live_trading_enabled = False

    def _log(self, agent: str, event: str, mint: str | None = None, **details: Any) -> dict[str, Any]:
        return self.store.add_solana_event(agent=agent, event=event, mint=mint, details=details)

    async def discover_pools(self, *, query: str = "", limit: int = 12) -> dict[str, Any]:
        from backend.storage.duckdb_store import utc_now_iso

        pools = await self.collector.dex.discover_pools(query=query, limit=limit)
        self._log(
            "SCOUT",
            "POOLS_DISCOVERED",
            details={"query": query, "count": len(pools)},
        )
        items: list[dict[str, Any]] = []
        for pool in pools:
            existing = self.store.get_solana_candidate(pool.candidate.mint)
            status = existing["status"] if existing else "DISCOVERED"
            rec = self.store.upsert_solana_candidate(
                {
                    "mint": pool.candidate.mint,
                    "symbol": pool.candidate.symbol,
                    "pair_address": pool.candidate.pair_address,
                    "dex_id": pool.dex_id,
                    "source": pool.candidate.source,
                    "price_usd": pool.price_usd,
                    "liquidity_usd": pool.liquidity_usd,
                    "volume_1h_usd": pool.volume_1h_usd,
                    "status": status,
                    "analyst_score": existing.get("analyst_score") if existing else None,
                    "analyst_rationale": existing.get("analyst_rationale") if existing else None,
                    "analyst_provider": existing.get("analyst_provider") if existing else None,
                    "rejection_reasons": existing.get("rejection_reasons", []) if existing else [],
                    "evidence": existing.get("evidence", {}) if existing else {},
                    "raw_evidence": existing.get("raw_evidence", {"pool": pool.raw_pair})
                    if existing
                    else {"pool": pool.raw_pair},
                    "provenance": pool.provenance,
                }
            )
            items.append(rec)
        return {
            "provenance": "LIVE",
            "live_trading_enabled": False,
            "count": len(items),
            "checked_at": utc_now_iso(),
            "items": items,
        }

    async def evaluate_mint(
        self,
        mint: str,
        *,
        symbol: str | None = None,
        analyst_mode: str = "ai",
        preferred_provider: str | None = None,
        override_evidence: FilterEvidence | None = None,
        override_analyst: Analyst | None = None,
        override_price_usd: float | None = None,
    ) -> dict[str, Any]:
        """Run SCOUT -> FILTER -> ANALYST -> EXECUTOR(QUOTE) -> LOGGER with full fail-closed checks."""
        from backend.solana_desk.analyst import DeterministicSolanaAnalyst, ModelRouterSolanaAnalyst

        clean_mint = mint.strip()
        if self.store.is_kill_switch_active():
            self._log("RISK", "KILL_SWITCH_HALTED_EVALUATION", clean_mint)
            return {
                "status": "REJECTED",
                "agent": "RISK",
                "reasons": ["KILL_SWITCH_ACTIVE"],
                "live_trading_enabled": False,
            }

        existing = self.store.get_solana_candidate(clean_mint)
        entered_positions = [p for p in self.store.list_solana_positions(limit=500) if p["mint"] == clean_mint]
        if existing and existing.get("status") == "PAPER_ENTERED" or entered_positions:
            self._log("FILTER", "REJECT_DUPLICATE_MINT", clean_mint)
            return {
                "status": "REJECTED",
                "agent": "FILTER",
                "reason": "DUPLICATE_MINT",
                "reasons": ["DUPLICATE_MINT"],
                "candidate": existing,
                "live_trading_enabled": False,
            }

        pool = None
        if override_evidence is not None:
            evidence = override_evidence
            raw_bundle: dict[str, Any] = {
                "source": "explicit_evidence",
                "evidence_sources": list(evidence.evidence_sources),
                "provenance": "SIMULATED",
            }
            candidate = Candidate(
                mint=clean_mint,
                symbol=(symbol or (existing["symbol"] if existing else "TOKEN"))[:32],
                pair_address=existing.get("pair_address") if existing else None,
                source="explicit_evidence",
            )
            price_usd = override_price_usd
            liq_usd = None
            dex_id = existing.get("dex_id") if existing else None
        else:
            pool, evidence, raw_bundle = await self.collector.collect(clean_mint)
            candidate = (
                pool.candidate
                if pool is not None
                else Candidate(
                    mint=clean_mint,
                    symbol=(symbol or "UNKNOWN")[:32],
                    source="solana_evidence_collector",
                )
            )
            price_usd = override_price_usd if override_price_usd is not None else (pool.price_usd if pool else None)
            liq_usd = pool.liquidity_usd if pool else None
            dex_id = pool.dex_id if pool else None

        self._log("SCOUT", "CANDIDATE_EVALUATION_STARTED", candidate.mint, symbol=candidate.symbol)

        decision = evaluate_candidate(candidate, evidence, self.config)
        if not decision.passed:
            self._log("FILTER", "REJECTED", candidate.mint, reasons=decision.rejection_reasons)
            rec = self.store.upsert_solana_candidate(
                {
                    "mint": candidate.mint,
                    "symbol": candidate.symbol,
                    "pair_address": candidate.pair_address,
                    "dex_id": dex_id,
                    "source": candidate.source,
                    "price_usd": price_usd,
                    "liquidity_usd": liq_usd,
                    "volume_1h_usd": evidence.volume_1h_usd,
                    "status": "REJECTED",
                    "rejection_reasons": decision.rejection_reasons,
                    "evidence": evidence.model_dump(),
                    "raw_evidence": raw_bundle,
                    "provenance": raw_bundle.get("provenance", "LIVE"),
                }
            )
            return {
                "status": "REJECTED",
                "agent": "FILTER",
                "reasons": decision.rejection_reasons,
                "candidate": rec,
                "live_trading_enabled": False,
            }

        self._log("FILTER", "PASSED", candidate.mint)

        analyst_provider = "local_deterministic"
        try:
            if override_analyst is not None:
                assessment = override_analyst.assess(candidate, evidence)
                analyst_provider = getattr(override_analyst, "provider_id", "custom_analyst")
            elif analyst_mode.strip().lower() == "deterministic":
                det = DeterministicSolanaAnalyst()
                assessment = det.assess(candidate, evidence)
                analyst_provider = det.provider_id
            else:
                if self.model_router is None:
                    raise RuntimeError("ModelRouter not configured")
                ai_analyst = ModelRouterSolanaAnalyst(
                    self.model_router, preferred_provider=preferred_provider
                )
                assessment = await ai_analyst.prepare_assessment(candidate, evidence)
                analyst_provider = ai_analyst.used_provider or "ai_router"

            if not math.isfinite(assessment.score) or not 0 <= assessment.score <= 1:
                raise ValueError("Analyst score outside [0, 1]")
        except Exception as exc:
            self._log("ANALYST", "FAILED_CLOSED", candidate.mint, error_type=type(exc).__name__, detail=str(exc)[:180])
            rec = self.store.upsert_solana_candidate(
                {
                    "mint": candidate.mint,
                    "symbol": candidate.symbol,
                    "pair_address": candidate.pair_address,
                    "dex_id": dex_id,
                    "source": candidate.source,
                    "price_usd": price_usd,
                    "liquidity_usd": liq_usd,
                    "volume_1h_usd": evidence.volume_1h_usd,
                    "status": "REJECTED",
                    "rejection_reasons": ["UNVERIFIED_ANALYST_RESULT"],
                    "analyst_rationale": f"Fail-closed: {type(exc).__name__}: {str(exc)[:180]}",
                    "analyst_provider": analyst_provider,
                    "evidence": evidence.model_dump(),
                    "raw_evidence": raw_bundle,
                    "provenance": raw_bundle.get("provenance", "LIVE"),
                }
            )
            return {
                "status": "REJECTED",
                "agent": "ANALYST",
                "reason": "UNVERIFIED_ANALYST_RESULT",
                "reasons": ["UNVERIFIED_ANALYST_RESULT"],
                "candidate": rec,
                "live_trading_enabled": False,
            }

        self._log(
            "ANALYST",
            "ASSESSED",
            candidate.mint,
            score=assessment.score,
            provider=analyst_provider,
            rationale=assessment.rationale,
        )

        balance = self.store.get_solana_balance_usd(self.config.starting_balance_usd)
        max_notional = calculate_position_notional(balance, self.config)

        if assessment.score < self.config.min_analyst_score:
            final_status = "NO_TRADE"
            self._log("EXECUTOR", "NO_TRADE_SCORE_BELOW_THRESHOLD", candidate.mint, score=assessment.score)
        elif max_notional <= 0 or max_notional > balance:
            final_status = "NO_TRADE"
            self._log("EXECUTOR", "NO_TRADE_INSUFFICIENT_BALANCE", candidate.mint, balance_usd=balance)
        else:
            final_status = "PAPER_QUOTE_REQUIRED"
            self._log(
                "EXECUTOR",
                "APPROVED_FOR_PAPER_QUOTE",
                candidate.mint,
                max_notional_usd=max_notional,
                live_trading_enabled=False,
            )

        self._log("LOGGER", "CANDIDATE_PIPELINE_COMPLETE", candidate.mint, status=final_status)
        rec = self.store.upsert_solana_candidate(
            {
                "mint": candidate.mint,
                "symbol": candidate.symbol,
                "pair_address": candidate.pair_address,
                "dex_id": dex_id,
                "source": candidate.source,
                "price_usd": price_usd,
                "liquidity_usd": liq_usd,
                "volume_1h_usd": evidence.volume_1h_usd,
                "status": final_status,
                "analyst_score": assessment.score,
                "analyst_rationale": assessment.rationale,
                "analyst_provider": analyst_provider,
                "rejection_reasons": [],
                "evidence": evidence.model_dump(),
                "raw_evidence": raw_bundle,
                "provenance": raw_bundle.get("provenance", "LIVE"),
            }
        )
        return {
            "status": final_status,
            "score": assessment.score,
            "analyst_provider": analyst_provider,
            "max_notional_usd": max_notional,
            "candidate": rec,
            "live_trading_enabled": False,
        }

    def open_paper_position(
        self,
        *,
        mint: str,
        entry_price: float | None = None,
        notional_usd: float | None = None,
    ) -> dict[str, Any]:
        import uuid
        from backend.storage.duckdb_store import utc_now_iso

        if self.live_trading_enabled:
            raise RuntimeError("Live trading is forbidden in Solana desk")
        if self.store.is_kill_switch_active():
            self._log("RISK", "PAPER_BUY_BLOCKED_KILL_SWITCH", mint)
            raise PermissionError("Kill Switch is active: Solana paper execution is halted")

        cand = self.store.get_solana_candidate(mint.strip())
        if not cand or cand.get("status") != "PAPER_QUOTE_REQUIRED":
            raise ValueError("Candidate has not passed the complete fail-closed paper quote pipeline")

        existing_positions = [
            p for p in self.store.list_solana_positions(limit=500) if p["mint"] == cand["mint"]
        ]
        if existing_positions:
            raise ValueError("Only one paper entry per mint is permitted")

        resolved_price = entry_price if entry_price is not None else cand.get("price_usd")
        if resolved_price is None or not math.isfinite(float(resolved_price)) or float(resolved_price) <= 0:
            raise ValueError("Entry price must be finite and positive")
        resolved_price = float(resolved_price)

        balance = self.store.get_solana_balance_usd(self.config.starting_balance_usd)
        max_notional = calculate_position_notional(balance, self.config)
        req_notional = max_notional if notional_usd is None else round(float(notional_usd), 2)
        if not math.isfinite(req_notional) or not 0 < req_notional <= max_notional:
            raise ValueError(f"Paper notional ${req_notional:.2f} exceeds strict 8% cap (${max_notional:.2f})")
        if req_notional > balance:
            raise ValueError("Insufficient simulated paper balance")

        # Record immutable entry in Risk Gate audit ledger if risk_gate exists
        if self.risk_gate is not None:
            self.store.record_risk_audit(
                {
                    "audit_id": f"AUD-SOL-{uuid.uuid4().hex[:8].upper()}",
                    "request_id": f"SOL-PAPER-{cand['mint'][:8]}",
                    "venue": "solana_paper_desk",
                    "strategy_tier": "tier1_solana_paper",
                    "execution_mode": "PAPER_EXECUTION",
                    "market_id": cand["mint"],
                    "requested_notional_usd": req_notional,
                    "net_edge_pct": 0.0,
                    "liquidity_participation_pct": 1.0,
                    "data_age_ms": 0,
                    "decision": "APPROVED",
                    "failed_rules": [],
                    "kill_switch_active": False,
                    "summary": f"SIMULATED Solana paper entry approved (${req_notional:.2f} <= 8% cap of ${balance:.2f})",
                    "decided_at": utc_now_iso(),
                }
            )

        now_iso = utc_now_iso()
        pos_id = f"SOLPOS-{uuid.uuid4().hex[:10].upper()}"
        pos = self.store.upsert_solana_position(
            {
                "position_id": pos_id,
                "mint": cand["mint"],
                "symbol": cand["symbol"],
                "entry_price": resolved_price,
                "mark_price": resolved_price,
                "peak_price": resolved_price,
                "notional_usd": req_notional,
                "trailing_active": False,
                "status": "OPEN",
                "exit_reason": None,
                "realized_pnl_usd": 0.0,
                "realized_pnl_pct": 0.0,
                "provenance": "SIMULATED",
                "opened_at": now_iso,
                "updated_at": now_iso,
                "closed_at": None,
            }
        )
        new_balance = self.store.set_solana_balance_usd(balance - req_notional)
        self.store.upsert_solana_candidate({**cand, "status": "PAPER_ENTERED"})
        self._log(
            "EXECUTOR",
            "PAPER_BUY_FILLED",
            cand["mint"],
            position_id=pos_id,
            entry_price=resolved_price,
            notional_usd=req_notional,
            remaining_balance_usd=new_balance,
        )
        return {"position": pos, "balance_usd": new_balance, "provenance": "SIMULATED", "live_trading_enabled": False}

    async def mark_positions(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        from backend.storage.duckdb_store import utc_now_iso

        open_rows = self.store.list_solana_positions(status="OPEN", limit=200)
        outcomes: list[dict[str, Any]] = []
        updated_positions: list[dict[str, Any]] = []
        balance = self.store.get_solana_balance_usd(self.config.starting_balance_usd)

        for row in open_rows:
            mint = row["mint"]
            mark_price = (prices or {}).get(mint)
            if mark_price is None:
                try:
                    pool = await self.collector.dex.resolve_mint(mint)
                    if pool and pool.price_usd:
                        mark_price = pool.price_usd
                except Exception:
                    mark_price = None

            if mark_price is None or not math.isfinite(float(mark_price)) or float(mark_price) <= 0:
                self._log("RISK", "POSITION_MARK_UNAVAILABLE", mint)
                continue

            pos_model = PaperPosition(
                mint=row["mint"],
                symbol=row["symbol"],
                entry_price=float(row["entry_price"]),
                mark_price=float(row["mark_price"]),
                peak_price=float(row["peak_price"]),
                notional_usd=float(row["notional_usd"]),
                trailing_active=bool(row["trailing_active"]),
            )
            updated, exit_reason = update_position_risk(pos_model, float(mark_price), self.config)
            now_iso = utc_now_iso()
            pnl_fraction = (updated.mark_price - updated.entry_price) / updated.entry_price
            pnl_usd = round(updated.notional_usd * pnl_fraction, 4)

            if exit_reason is None:
                saved = self.store.upsert_solana_position(
                    {
                        **row,
                        "mark_price": updated.mark_price,
                        "peak_price": updated.peak_price,
                        "trailing_active": updated.trailing_active,
                        "updated_at": now_iso,
                    }
                )
                updated_positions.append(saved)
                continue

            exit_value = max(0.0, updated.notional_usd + pnl_usd)
            balance = self.store.set_solana_balance_usd(balance + exit_value)
            closed = self.store.upsert_solana_position(
                {
                    **row,
                    "mark_price": updated.mark_price,
                    "peak_price": updated.peak_price,
                    "trailing_active": updated.trailing_active,
                    "status": "CLOSED",
                    "exit_reason": exit_reason,
                    "realized_pnl_usd": pnl_usd,
                    "realized_pnl_pct": round(pnl_fraction * 100.0, 2),
                    "updated_at": now_iso,
                    "closed_at": now_iso,
                }
            )
            self._log(
                "RISK",
                "PAPER_POSITION_CLOSED",
                mint,
                position_id=row["position_id"],
                reason=exit_reason,
                pnl_usd=pnl_usd,
                pnl_pct=round(pnl_fraction * 100.0, 2),
            )
            outcomes.append(closed)

            # Record stop-loss / trade exit into LearningEngine
            if self.learning is not None:
                try:
                    cyc = self.learning.record_observation(
                        source_type="SOLANA_PAPER",
                        source_ref_id=row["position_id"],
                        agent_id="solana_risk_agent",
                        subject_key=f"solana_exit_{row['symbol'].lower()}",
                        hypothesis=f"Paper exit rule {exit_reason} executed cleanly for {row['symbol']}",
                        expected_outcome={"positive_pnl": True},
                        evidence=[
                            {
                                "source_type": "SOLANA_PAPER_POSITION",
                                "source_ref_id": row["position_id"],
                                "detail": f"reason={exit_reason}, pnl_pct={pnl_fraction * 100.0:.2f}%",
                            }
                        ],
                    )
                    self.learning.record_evaluation(
                        cyc["cycle_id"],
                        evaluation_notes=f"Paper exit {exit_reason} at mark=${updated.mark_price:.6f}",
                    )
                    self.learning.record_decision(
                        cyc["cycle_id"],
                        decision_summary=f"Closed simulated position {row['position_id']} ({exit_reason})",
                    )
                    self.learning.record_outcome(
                        cyc["cycle_id"],
                        actual_outcome={"realized_pnl_usd": pnl_usd, "exit_reason": exit_reason},
                        outcome_matched=pnl_usd > 0,
                    )
                except Exception:
                    pass

        return {
            "provenance": "SIMULATED",
            "live_trading_enabled": False,
            "balance_usd": balance,
            "updated_positions": updated_positions,
            "closed_positions": outcomes,
        }

    def get_overview(self, limit: int = 50) -> dict[str, Any]:
        balance = self.store.get_solana_balance_usd(self.config.starting_balance_usd)
        candidates = self.store.list_solana_candidates(limit=limit)
        positions = self.store.list_solana_positions(limit=limit * 2)
        events = self.store.list_solana_events(limit=limit)
        open_pos = [p for p in positions if p["status"] == "OPEN"]
        closed_pos = [p for p in positions if p["status"] == "CLOSED"]

        unrealized_usd = 0.0
        open_market_value = 0.0
        for p in open_pos:
            frac = (float(p["mark_price"]) - float(p["entry_price"])) / float(p["entry_price"])
            pnl = float(p["notional_usd"]) * frac
            unrealized_usd += pnl
            open_market_value += max(0.0, float(p["notional_usd"]) + pnl)

        realized_usd = sum(float(p.get("realized_pnl_usd") or 0.0) for p in closed_pos)
        wins = sum(1 for p in closed_pos if float(p.get("realized_pnl_usd") or 0.0) > 0)
        hard_stops = sum(1 for p in closed_pos if p.get("exit_reason") == "HARD_STOP_6_PERCENT")
        trailing_stops = sum(
            1 for p in closed_pos if p.get("exit_reason") == "TRAILING_STOP_6_PERCENT_FROM_PEAK"
        )
        avg_pnl_pct = (
            round(sum(float(p.get("realized_pnl_pct") or 0.0) for p in closed_pos) / len(closed_pos), 2)
            if closed_pos
            else 0.0
        )

        return {
            "provenance": "SIMULATED",
            "live_trading_enabled": False,
            "kill_switch_active": self.store.is_kill_switch_active(),
            "config": self.config.model_dump(),
            "ledger": {
                "provenance": "SIMULATED",
                "starting_balance_usd": self.config.starting_balance_usd,
                "cash_balance_usd": balance,
                "open_positions_value_usd": round(open_market_value, 2),
                "total_equity_usd": round(balance + open_market_value, 2),
                "max_next_notional_usd": calculate_position_notional(balance, self.config),
                "unrealized_pnl_usd": round(unrealized_usd, 4),
                "realized_pnl_usd": round(realized_usd, 4),
                "open_count": len(open_pos),
                "closed_count": len(closed_pos),
                "win_rate_pct": round((wins / len(closed_pos)) * 100.0, 1) if closed_pos else 0.0,
                "avg_realized_pnl_pct": avg_pnl_pct,
                "hard_stop_exits": hard_stops,
                "trailing_stop_exits": trailing_stops,
            },
            "candidates": candidates,
            "positions": positions,
            "events": events,
        }

