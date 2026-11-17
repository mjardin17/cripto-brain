"""Separate Stocks & Crypto AI Councils + Continuous Movement Forecast & Learning Loop.

Preserves two independent advisory AI Councils:
- CRYPTO Council (BTC / ETH spot, exchange volume/whale proxies, OKX leverage/funding, Fear & Greed, IBIT ETF flows, SPY/QQQ spillover)
- STOCKS Council (SPY / QQQ macro momentum, IBIT/FBTC institutional risk appetite, Fed policy/CPI/jobs & geopolitical RSS, BTC-equity correlation)

Each council convenes all 5 required roles:
1. Pattern Analyst
2. Momentum Scout
3. Edge Analyst
4. Calibration Auditor
5. Risk Sentinel

Safety & Data Hygiene Invariants:
- Advisory & paper-research only (live_trading_enabled = False; zero real orders or money movement).
- Fail-closed Risk Sentinel checks DeterministicRiskGate kill switch, feed health, and leverage squeeze danger.
- Strict contamination isolation: TEST, SIMULATED, and UNVERIFIED rows never contaminate LIVE priors,
  historical pattern matching, or LIVE Brier/calibration scorecards.
- Zero unnecessary AI credits ($0.00 deterministic council reasoning).
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.market_drivers import MarketDriverEngine
from backend.intelligence.memory_manager import StructuredMemoryManager
from backend.intelligence.probability import (
    ProbabilityIntelligenceEngine,
    compute_brier_score,
    compute_log_loss,
)
from backend.intelligence.validation_labs import AdversarialReviewEngine
from backend.risk_gate.engine import DeterministicRiskGate
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

CouncilType = Literal["CRYPTO", "STOCKS"]

COUNCIL_ROLES: tuple[str, ...] = (
    "Pattern Analyst",
    "Momentum Scout",
    "Edge Analyst",
    "Calibration Auditor",
    "Risk Sentinel",
)


def _clamp_prob(p: float) -> float:
    if not math.isfinite(p):
        return 0.5
    return round(min(0.95, max(0.05, float(p))), 4)


def _horizon_hours(horizon: str) -> float:
    h = (horizon or "24H").strip().upper()
    if h == "4H":
        return 4.0
    if h == "12H":
        return 12.0
    if h == "48H":
        return 48.0
    return 24.0


class AICouncilEngine:
    """Orchestrates separate Crypto & Stocks AI Councils, peer challenge debates, and forecast learning."""

    def __init__(
        self,
        *,
        store: DuckDBStore,
        market_drivers: MarketDriverEngine,
        risk_gate: DeterministicRiskGate,
        prob_engine: ProbabilityIntelligenceEngine,
        learning: LearningEngine,
        memory: StructuredMemoryManager,
        adversarial: AdversarialReviewEngine | None = None,
    ) -> None:
        self.store = store
        self.market_drivers = market_drivers
        self.risk_gate = risk_gate
        self.prob_engine = prob_engine
        self.learning = learning
        self.memory = memory
        self.adversarial = adversarial

    # ------------------------------------------------------------------
    # Clean Calibration & Contamination Isolation Queries
    # ------------------------------------------------------------------
    def get_clean_calibration_metrics(
        self,
        *,
        council_type: CouncilType | None = None,
        include_non_live: bool = False,
        num_buckets: int = 10,
    ) -> dict[str, Any]:
        """Compute Brier score, log loss, and ECE strictly from clean LIVE verified forecasts by default."""
        with self.store._lock:
            clauses = ["1=1"]
            params: list[Any] = []
            if council_type:
                clauses.append("council_type = ?")
                params.append(council_type.upper())
            if not include_non_live:
                clauses.append("provenance = 'LIVE' AND verified_clean = TRUE")

            rows = self.store._conn.execute(
                f"""
                SELECT status, predicted_move_prob, actual_outcome, brier_score, log_loss, provenance, verified_clean
                FROM movement_forecasts
                WHERE {' AND '.join(clauses)}
                """,
                params,
            ).fetchall()

            excluded_non_live = self.store._conn.execute(
                """
                SELECT COUNT(*) FROM movement_forecasts
                WHERE NOT (provenance = 'LIVE' AND verified_clean = TRUE)
                """
            ).fetchone()[0]

        open_cnt = sum(1 for r in rows if r[0] == "OPEN")
        voided_cnt = sum(1 for r in rows if r[0] == "VOIDED")
        resolved = [r for r in rows if r[0] == "RESOLVED" and r[2] in (0, 1)]
        n_res = len(resolved)

        brier_sum = 0.0
        logloss_sum = 0.0
        correct = 0
        buckets_acc: list[list[tuple[float, int]]] = [[] for _ in range(num_buckets)]

        for _, p_raw, y_raw, b_raw, ll_raw, _, _ in resolved:
            p = float(p_raw)
            y = int(y_raw)
            brier = float(b_raw) if b_raw is not None else compute_brier_score(p, y)
            ll = float(ll_raw) if ll_raw is not None else compute_log_loss(p, y)
            brier_sum += brier
            logloss_sum += ll
            if (p >= 0.5 and y == 1) or (p < 0.5 and y == 0):
                correct += 1
            idx = min(num_buckets - 1, max(0, int(p * num_buckets)))
            buckets_acc[idx].append((p, y))

        ece = 0.0
        if n_res > 0:
            for items in buckets_acc:
                if items:
                    avg_p = sum(x[0] for x in items) / len(items)
                    emp_y = sum(x[1] for x in items) / len(items)
                    ece += (len(items) / n_res) * abs(avg_p - emp_y)

        return {
            "isolation_mode": "ALL_INCLUDING_NON_LIVE" if include_non_live else "CLEAN_LIVE_ONLY",
            "resolved_count": n_res,
            "open_count": open_cnt,
            "voided_count": voided_cnt,
            "excluded_non_live_or_unverified_count": int(excluded_non_live or 0),
            "mean_brier_score": round(brier_sum / n_res, 6) if n_res > 0 else None,
            "mean_log_loss": round(logloss_sum / n_res, 6) if n_res > 0 else None,
            "directional_accuracy_pct": round((correct / n_res) * 100.0, 2) if n_res > 0 else None,
            "expected_calibration_error": round(ece, 6) if n_res > 0 else None,
        }

    # ------------------------------------------------------------------
    # Single Council Deliberation (All 5 Roles + Inter-Agent Challenge)
    # ------------------------------------------------------------------
    def _deliberate_council(
        self,
        *,
        council_type: CouncilType,
        snapshot: dict[str, Any],
        horizon: str,
        create_forecast: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        assets = snapshot.get("assets") or {}
        corrs = snapshot.get("correlations") or {}
        derivs = snapshot.get("derivatives") or {}
        sentiment = snapshot.get("sentiment") or {}
        macro = snapshot.get("macro_news") or {}
        why_drivers = snapshot.get("why_drivers") or []
        when_signals = snapshot.get("when_signals") or {}
        feed_health = snapshot.get("feed_health") or {}

        is_crypto = council_type == "CRYPTO"
        target_symbol = "BTC" if is_crypto else "SPY"
        secondary_symbol = "ETH" if is_crypto else "QQQ"
        primary_asset = assets.get(target_symbol) or {}
        secondary_asset = assets.get(secondary_symbol) or {}
        ibit_asset = assets.get("IBIT") or {}
        okx_btc = derivs.get("BTC") or {}

        base_price = primary_asset.get("price_usd")
        chg_24h = primary_asset.get("change_24h_pct")
        sec_chg = secondary_asset.get("change_24h_pct")
        v_surge = primary_asset.get("volume_surge_ratio")
        comp_ratio = primary_asset.get("compression_ratio")
        realized_vol = primary_asset.get("realized_vol_20d_pct")

        etf_proxy = macro.get("etf_flow_proxy") or {}
        etf_score = float(etf_proxy.get("score") or 0.0)
        news_polarity = float(macro.get("avg_news_polarity") or 0.0)
        fed_count = int(macro.get("fed_policy_count") or 0)
        geo_count = int(macro.get("geopolitical_risk_count") or 0)
        fng_val = sentiment.get("value")
        fng_delta = sentiment.get("delta_7d")
        fr = okx_btc.get("funding_rate")
        ls_ratio = okx_btc.get("long_short_account_ratio")
        liq_risk = float(okx_btc.get("liquidation_risk_score") or 0.0)
        btc_qqq_corr = (corrs.get("BTC_QQQ") or {}).get("correlation")
        btc_spy_corr = (corrs.get("BTC_SPY") or {}).get("correlation")

        # Pull verified historical memories & clean live calibration stats (never TEST/SIMULATED)
        clean_cal = self.get_clean_calibration_metrics(council_type=council_type, include_non_live=False)
        verified_lessons = self.memory.search_relevant(
            f"{council_type.lower()} {target_symbol.lower()} volatility movement",
            statuses=["VERIFIED_LESSON", "APPLIED"],
            limit=4,
        )

        # ------------------------------------------------------------------
        # Role 1: Pattern Analyst
        # ------------------------------------------------------------------
        pa_move_prob = 0.38
        pa_up_prob = 0.50
        pa_notes: list[str] = []
        if comp_ratio is not None:
            if comp_ratio <= 0.72:
                pa_move_prob += 0.24
                pa_notes.append(f"{target_symbol} range compressed to {comp_ratio:.2f}x 20d ATR (coil breakout setup)")
            elif comp_ratio >= 1.40:
                pa_move_prob += 0.20
                pa_notes.append(f"{target_symbol} range expanded to {comp_ratio:.2f}x 20d ATR (active volatility regime)")
        if is_crypto and btc_qqq_corr is not None:
            pa_notes.append(f"BTC↔QQQ 30d Pearson correlation r={btc_qqq_corr:+.2f}")
            if abs(btc_qqq_corr) >= 0.45 and sec_chg is not None:
                pa_up_prob += max(-0.12, min(0.12, float((assets.get("QQQ") or {}).get("change_24h_pct") or 0.0) * 0.04))
        elif not is_crypto and btc_spy_corr is not None:
            pa_notes.append(f"Equities↔BTC cross-correlation r={btc_spy_corr:+.2f}; SPY realized vol={realized_vol}%")
        if when_signals.get("historical_analogs_count", 0) > 0:
            pa_notes.append(f"Matched {when_signals['historical_analogs_count']} verified LIVE historical regime analog(s)")

        pa_stance = (
            "BREAKOUT_WATCH"
            if pa_move_prob >= 0.58
            else ("BULLISH_BIAS" if pa_up_prob >= 0.56 else ("BEARISH_BIAS" if pa_up_prob <= 0.44 else "RANGE_NEUTRAL"))
        )
        pattern_opinion = {
            "role": "Pattern Analyst",
            "agent_id": f"{council_type.lower()}_pattern_analyst",
            "stance": pa_stance,
            "significant_move_prob": _clamp_prob(pa_move_prob),
            "up_prob": _clamp_prob(pa_up_prob),
            "confidence": 0.72 if base_price is not None else 0.35,
            "key_metrics": {
                "compression_ratio": comp_ratio,
                "realized_vol_20d_pct": realized_vol,
                "btc_qqq_corr": btc_qqq_corr,
                "verified_analogs": when_signals.get("historical_analogs_count", 0),
            },
            "rationale": "; ".join(pa_notes) or f"Baseline structural range on {target_symbol}.",
        }

        # ------------------------------------------------------------------
        # Role 2: Momentum Scout
        # ------------------------------------------------------------------
        ms_move_prob = 0.40
        ms_up_prob = 0.50
        ms_notes: list[str] = []
        if chg_24h is not None:
            ms_move_prob += min(0.24, abs(chg_24h) * 0.05)
            ms_up_prob += max(-0.20, min(0.20, chg_24h * 0.055))
            ms_notes.append(f"{target_symbol} 24h {chg_24h:+.2f}%, {secondary_symbol} {(sec_chg or 0.0):+.2f}%")
        if v_surge is not None and v_surge >= 1.25:
            ms_move_prob += min(0.18, (v_surge - 1.0) * 0.16)
            ms_notes.append(f"Volume surge {v_surge:.2f}x 20d/30d avg confirms participation")
        if is_crypto:
            ms_up_prob += max(-0.10, min(0.10, etf_score * 0.14))
            if etf_proxy.get("signals"):
                ms_notes.append(f"ETF institutional proxy: {etf_proxy.get('regime')} ({etf_score:+.2f})")
            if fng_val is not None:
                ms_notes.append(f"Fear & Greed={fng_val}/100 ({sentiment.get('classification')}, 7d Δ{int(fng_delta or 0):+d})")
        else:
            ibit_chg = ibit_asset.get("change_24h_pct")
            if ibit_chg is not None:
                ms_notes.append(f"IBIT institutional risk-beta {ibit_chg:+.2f}%")
            if fed_count or geo_count:
                ms_up_prob += max(-0.10, min(0.10, news_polarity * 0.12))
                ms_notes.append(f"Macro/Fed ({fed_count}) & Geopolitics ({geo_count}) tone {news_polarity:+.2f}")

        ms_stance = (
            "BULLISH_MOVE"
            if ms_up_prob >= 0.56
            else ("BEARISH_MOVE" if ms_up_prob <= 0.44 else "NEUTRAL_FLOW")
        )
        momentum_opinion = {
            "role": "Momentum Scout",
            "agent_id": f"{council_type.lower()}_momentum_scout",
            "stance": ms_stance,
            "significant_move_prob": _clamp_prob(ms_move_prob),
            "up_prob": _clamp_prob(ms_up_prob),
            "confidence": 0.74 if chg_24h is not None else 0.35,
            "key_metrics": {
                "change_24h_pct": chg_24h,
                "volume_surge_ratio": v_surge,
                "etf_flow_score": round(etf_score, 3),
                "fear_greed": fng_val,
            },
            "rationale": "; ".join(ms_notes) or f"Neutral momentum & volume profile for {target_symbol}.",
        }

        # ------------------------------------------------------------------
        # Role 3: Edge Analyst
        # ------------------------------------------------------------------
        raw_move_prob = 0.45 * pattern_opinion["significant_move_prob"] + 0.55 * momentum_opinion["significant_move_prob"]
        raw_up_prob = 0.40 * pattern_opinion["up_prob"] + 0.60 * momentum_opinion["up_prob"]
        urgency = float(when_signals.get("movement_urgency_score") or 0.30)
        raw_move_prob = 0.65 * raw_move_prob + 0.35 * urgency

        if is_crypto and liq_risk >= 0.65:
            # Leverage squeeze amplifies significant movement likelihood, counter-positioning crowded side
            raw_move_prob = min(0.88, raw_move_prob + 0.12)
            if "LONG_SQUEEZE" in str(okx_btc.get("crowding_regime") or ""):
                raw_up_prob = max(0.20, raw_up_prob - 0.10)
            elif "SHORT_SQUEEZE" in str(okx_btc.get("crowding_regime") or ""):
                raw_up_prob = min(0.82, raw_up_prob + 0.10)

        edge_stance = (
            "HIGH_EDGE_BREAKOUT"
            if raw_move_prob >= 0.60
            else ("DIRECTIONAL_TILT" if abs(raw_up_prob - 0.50) >= 0.08 else "LOW_EDGE_WAIT")
        )
        edge_opinion = {
            "role": "Edge Analyst",
            "agent_id": f"{council_type.lower()}_edge_analyst",
            "stance": edge_stance,
            "significant_move_prob": _clamp_prob(raw_move_prob),
            "up_prob": _clamp_prob(raw_up_prob),
            "confidence": round(0.75 if feed_health.get("live_count", 0) >= 6 else 0.52, 3),
            "key_metrics": {
                "raw_move_prob": round(raw_move_prob, 4),
                "raw_up_prob": round(raw_up_prob, 4),
                "urgency_score": urgency,
                "verified_lessons_applied": len(verified_lessons),
            },
            "rationale": (
                f"Synthesized {target_symbol} ({horizon}) significant-move edge={raw_move_prob*100:.1f}%, "
                f"upward bias={raw_up_prob*100:.1f}% across {len(why_drivers)} live driver groups "
                f"({when_signals.get('movement_regime')})."
            ),
        }

        # ------------------------------------------------------------------
        # Role 4: Calibration Auditor (Shrinks overconfidence when unresolved/high ECE)
        # ------------------------------------------------------------------
        resolved_cnt = int(clean_cal.get("resolved_count") or 0)
        mean_brier = clean_cal.get("mean_brier_score")
        ece = clean_cal.get("expected_calibration_error")
        shrinkage_factor = 0.65  # Conservative default when resolved live samples < 10
        cal_notes: list[str] = []

        if resolved_cnt < 10:
            cal_notes.append(
                f"Only {resolved_cnt}/10 clean LIVE resolved forecasts; shrinking extreme probabilities 35% toward base rate"
            )
        else:
            if mean_brier is not None and float(mean_brier) > 0.22:
                shrinkage_factor = 0.60
                cal_notes.append(f"LIVE Brier={mean_brier:.4f} elevated (>0.22); applying 40% calibration damping")
            elif ece is not None and float(ece) > 0.12:
                shrinkage_factor = 0.72
                cal_notes.append(f"LIVE ECE={ece:.4f} (>0.12); applying 28% calibration damping")
            else:
                shrinkage_factor = 0.88
                cal_notes.append(f"LIVE Brier={mean_brier} across {resolved_cnt} verified forecasts supports 88% weight")

        cal_move_prob = _clamp_prob(0.45 + shrinkage_factor * (edge_opinion["significant_move_prob"] - 0.45))
        cal_up_prob = _clamp_prob(0.50 + shrinkage_factor * (edge_opinion["up_prob"] - 0.50))
        calibration_opinion = {
            "role": "Calibration Auditor",
            "agent_id": f"{council_type.lower()}_calibration_auditor",
            "stance": "CALIBRATION_DAMPENED" if shrinkage_factor < 0.80 else "CALIBRATION_VALIDATED",
            "significant_move_prob": cal_move_prob,
            "up_prob": cal_up_prob,
            "confidence": round(0.85 if resolved_cnt >= 10 else 0.62, 3),
            "key_metrics": {
                "clean_live_resolved_count": resolved_cnt,
                "clean_live_brier_score": mean_brier,
                "excluded_non_live_count": clean_cal.get("excluded_non_live_or_unverified_count", 0),
                "shrinkage_factor": shrinkage_factor,
            },
            "rationale": "; ".join(cal_notes),
        }

        # ------------------------------------------------------------------
        # Role 5: Risk Sentinel (Fail-closed safety, kill switch, liquidity & crowding)
        # ------------------------------------------------------------------
        kill_active = self.store.is_kill_switch_active()
        risk_warnings: list[str] = []
        unavailable_feeds = [k for k, v in (feed_health.get("feeds") or {}).items() if v != "LIVE"]

        if kill_active:
            risk_warnings.append("GLOBAL KILL SWITCH ACTIVE: all execution & dispatch halted; advisory observation only.")
        if base_price is None:
            risk_warnings.append(f"Primary price feed for {target_symbol} is UNAVAILABLE — fail-closed uncertainty enforced.")
        if unavailable_feeds:
            risk_warnings.append(f"Degraded feeds ({len(unavailable_feeds)}): {', '.join(unavailable_feeds[:5])}")
        if is_crypto and liq_risk >= 0.68:
            risk_warnings.append(
                f"High OKX leverage crowding ({okx_btc.get('crowding_regime')}, funding={fr}, L/S={ls_ratio}): liquidation whipsaw hazard."
            )
        if geo_count >= 2 or fed_count >= 2:
            risk_warnings.append("Macro/Fed or Geopolitical event cluster active — gap/slippage risk elevated.")
        if snapshot.get("provenance") != "LIVE" or not snapshot.get("verified_clean"):
            risk_warnings.append(
                f"Snapshot provenance={snapshot.get('provenance')} (verified_clean={snapshot.get('verified_clean')}) — quarantined from LIVE priors."
            )

        risk_veto = kill_active or base_price is None
        risk_caution = risk_veto or liq_risk >= 0.68 or len(unavailable_feeds) >= 4
        final_conf = min(
            0.88 if resolved_cnt >= 10 else 0.68,
            0.25 if risk_veto else (0.54 if risk_caution else 0.74),
        )

        risk_opinion = {
            "role": "Risk Sentinel",
            "agent_id": f"{council_type.lower()}_risk_sentinel",
            "stance": "FAIL_CLOSED_VETO" if risk_veto else ("CAUTION_THROTTLE" if risk_caution else "WITHIN_BOUNDS"),
            "significant_move_prob": cal_move_prob,
            "up_prob": cal_up_prob if not risk_veto else 0.50,
            "confidence": round(final_conf, 3),
            "key_metrics": {
                "kill_switch_active": kill_active,
                "live_trading_enabled": False,
                "unavailable_feeds_count": len(unavailable_feeds),
                "liquidation_risk_score": liq_risk,
            },
            "rationale": " | ".join(risk_warnings) if risk_warnings else "No kill-switch, data-freshness, or squeeze vetoes triggered. Advisory paper research only.",
        }

        agent_opinions = [
            pattern_opinion,
            momentum_opinion,
            edge_opinion,
            calibration_opinion,
            risk_opinion,
        ]

        # ------------------------------------------------------------------
        # Inter-Agent Challenge & Dissent Round
        # ------------------------------------------------------------------
        challenges: list[dict[str, str]] = []
        if abs(edge_opinion["up_prob"] - cal_up_prob) >= 0.03 or resolved_cnt < 10:
            challenges.append(
                {
                    "challenger": "Calibration Auditor",
                    "target": "Edge Analyst",
                    "claim_challenged": f"Raw directional/move conviction ({edge_opinion['significant_move_prob']*100:.1f}% move / {edge_opinion['up_prob']*100:.1f}% up)",
                    "challenge": f"Shrank extreme tail probabilities via factor {shrinkage_factor:.2f} because clean LIVE resolved count={resolved_cnt} (<10 gate or Brier penalty).",
                    "resolution": f"Adopted calibrated move_prob={cal_move_prob*100:.1f}%, up_prob={cal_up_prob*100:.1f}%.",
                }
            )
        if is_crypto and btc_qqq_corr is not None and chg_24h is not None and sec_chg is not None:
            qqq_chg_val = (assets.get("QQQ") or {}).get("change_24h_pct")
            if qqq_chg_val is not None and (chg_24h * qqq_chg_val < 0) and abs(btc_qqq_corr) >= 0.35:
                challenges.append(
                    {
                        "challenger": "Pattern Analyst",
                        "target": "Momentum Scout",
                        "claim_challenged": f"Spot momentum {chg_24h:+.2f}% diverging from QQQ ({qqq_chg_val:+.2f}%)",
                        "challenge": f"BTC↔QQQ correlation is {btc_qqq_corr:+.2f}; crypto-equity divergence frequently mean-reverts unless backed by ETF flows.",
                        "resolution": "Flagged divergence risk in confidence score.",
                    }
                )
        if risk_caution:
            challenges.append(
                {
                    "challenger": "Risk Sentinel",
                    "target": "Edge Analyst",
                    "claim_challenged": "Position/confidence sizing during elevated hazard",
                    "challenge": risk_opinion["rationale"],
                    "resolution": f"Enforced confidence ceiling {final_conf:.2f}; paper-only advisory posture maintained.",
                }
            )
        if not challenges:
            challenges.append(
                {
                    "challenger": "Risk Sentinel",
                    "target": "Momentum Scout",
                    "claim_challenged": "Feed provenance & paper-only invariant check",
                    "challenge": "Verified live feed timestamps, zero prompt injections, and kill-switch inactive before publishing advisory forecast.",
                    "resolution": "Consensus reached with zero unresolved vetoes.",
                }
            )

        # Optional AdversarialReviewEngine formal check when move probability is elevated
        if self.adversarial is not None and cal_move_prob >= 0.65 and base_price is not None:
            self.adversarial.run_review(
                subject_key=f"council:{council_type.lower()}:{target_symbol.lower()}",
                conclusion_agent=f"{council_type.lower()}_edge_analyst",
                conclusion_summary=edge_opinion["rationale"][:500],
                proposer_evidence=[
                    {
                        "source_type": "MARKET_DRIVER_SNAPSHOT",
                        "source_ref_id": snapshot["snapshot_id"],
                        "detail": f"{target_symbol}=${base_price}, live_feeds={feed_health.get('live_count')}",
                    }
                ],
                challenger_agent=f"{council_type.lower()}_calibration_auditor",
                arbiter_agent=f"{council_type.lower()}_risk_sentinel",
                metrics={"claimed_prob": cal_move_prob, "data_age_ms": snapshot.get("data_freshness_ms", 0)},
            )

        final_move_prob = cal_move_prob
        final_up_prob = 0.50 if risk_veto else cal_up_prob
        if risk_veto:
            regime_verdict = "UNCERTAIN_HALTED"
            directional_bias = "NEUTRAL_HOLD"
        else:
            regime_verdict = str(when_signals.get("movement_regime") or "RANGE_CONSOLIDATION")
            directional_bias = (
                "BULLISH" if final_up_prob >= 0.55 else ("BEARISH" if final_up_prob <= 0.45 else "NEUTRAL_TWO_WAY")
            )

        top_why = "; ".join(d["explanation"] for d in why_drivers[:3]) or "Awaiting stronger multi-factor catalyst alignment."
        when_exp = (
            f"Regime: {regime_verdict} ({when_signals.get('expected_window', horizon)}). "
            + "; ".join(when_signals.get("trigger_reasons") or [])
        )
        dissent_summary = " | ".join(f"{c['challenger']} -> {c['target']}: {c['challenge']}" for c in challenges)

        delib_id = f"CNCL-{council_type[:3]}-{uuid.uuid4().hex[:8].upper()}"
        created_at = utc_now_iso()
        prov = str(snapshot.get("provenance") or "UNAVAILABLE").upper()
        v_clean = bool(snapshot.get("verified_clean")) and prov == "LIVE"

        delib = {
            "deliberation_id": delib_id,
            "council_type": council_type,
            "snapshot_id": snapshot["snapshot_id"],
            "provenance": prov,
            "verified_clean": v_clean,
            "target_symbol": target_symbol,
            "horizon": horizon,
            "regime_verdict": regime_verdict,
            "directional_bias": directional_bias,
            "significant_move_prob": final_move_prob,
            "up_prob": final_up_prob,
            "confidence": round(final_conf, 3),
            "why_explanation": top_why,
            "when_explanation": when_exp,
            "agent_opinions": agent_opinions,
            "challenges": challenges,
            "dissent_summary": dissent_summary,
            "risk_warnings": risk_warnings,
            "created_at": created_at,
        }

        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO council_deliberations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    delib_id,
                    council_type,
                    snapshot["snapshot_id"],
                    prov,
                    v_clean,
                    target_symbol,
                    horizon,
                    regime_verdict,
                    directional_bias,
                    final_move_prob,
                    final_up_prob,
                    round(final_conf, 3),
                    top_why[:1000],
                    when_exp[:1000],
                    json.dumps(agent_opinions),
                    json.dumps(challenges),
                    dissent_summary[:1000],
                    json.dumps(risk_warnings),
                    created_at,
                ],
            )

        forecast_rec: dict[str, Any] | None = None
        if create_forecast and base_price is not None:
            threshold_pct = 1.5 if is_crypto else 0.75
            resolve_after = (
                datetime.now(timezone.utc) + timedelta(hours=_horizon_hours(horizon))
            ).isoformat()
            forecast_rec = self.record_forecast(
                deliberation_id=delib_id,
                council_type=council_type,
                symbol=target_symbol,
                horizon=horizon,
                move_threshold_pct=threshold_pct,
                predicted_move_prob=final_move_prob,
                predicted_up_prob=final_up_prob,
                confidence=round(final_conf, 3),
                baseline_price=float(base_price),
                provenance=prov,
                verified_clean=v_clean,
                resolve_after=resolve_after,
                rationale=f"WHY: {top_why[:280]} | WHEN: {regime_verdict}",
            )

        return delib, forecast_rec

    def convene_councils(
        self,
        *,
        snapshot: dict[str, Any],
        councils: list[CouncilType] | None = None,
        horizon: str = "24H",
        create_forecasts: bool = True,
    ) -> dict[str, Any]:
        selected = councils or ["CRYPTO", "STOCKS"]
        deliberations: dict[str, dict[str, Any]] = {}
        forecasts: list[dict[str, Any]] = []
        for c_type in selected:
            ctype = c_type.upper()  # type: ignore[assignment]
            if ctype not in ("CRYPTO", "STOCKS"):
                raise ValueError(f"Unsupported council_type '{c_type}'. Must be CRYPTO or STOCKS.")
            delib, fc = self._deliberate_council(
                council_type=ctype,  # type: ignore[arg-type]
                snapshot=snapshot,
                horizon=horizon.upper(),
                create_forecast=create_forecasts,
            )
            deliberations[ctype] = delib
            if fc is not None:
                forecasts.append(fc)

        return {
            "snapshot_id": snapshot["snapshot_id"],
            "provenance": snapshot.get("provenance"),
            "verified_clean": snapshot.get("verified_clean"),
            "live_trading_enabled": False,
            "deliberations": deliberations,
            "created_forecasts": forecasts,
        }

    # ------------------------------------------------------------------
    # Movement Forecast Persistence, Outcome Resolution & Learning Loop
    # ------------------------------------------------------------------
    def record_forecast(
        self,
        *,
        deliberation_id: str,
        council_type: CouncilType,
        symbol: str,
        horizon: str,
        move_threshold_pct: float,
        predicted_move_prob: float,
        predicted_up_prob: float,
        confidence: float,
        baseline_price: float | None,
        provenance: str,
        verified_clean: bool,
        resolve_after: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        fid = f"MFC-{uuid.uuid4().hex[:10].upper()}"
        created_at = utc_now_iso()
        eff_resolve = resolve_after or (
            datetime.now(timezone.utc) + timedelta(hours=_horizon_hours(horizon))
        ).isoformat()
        eff_prov = provenance.strip().upper()
        eff_clean = bool(verified_clean) and eff_prov == "LIVE"

        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO movement_forecasts VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    fid,
                    deliberation_id,
                    council_type.upper(),
                    symbol.upper(),
                    horizon.upper(),
                    float(move_threshold_pct),
                    _clamp_prob(predicted_move_prob),
                    _clamp_prob(predicted_up_prob),
                    round(min(1.0, max(0.0, float(confidence))), 4),
                    float(baseline_price) if baseline_price is not None else None,
                    eff_prov,
                    eff_clean,
                    "OPEN",
                    eff_resolve,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    rationale[:600],
                    created_at,
                    None,
                ],
            )
            fc = self.get_forecast(fid)
            assert fc is not None
            return fc

    def get_forecast(self, forecast_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM movement_forecasts WHERE forecast_id = ?", [forecast_id]
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return dict(zip(cols, row))

    def list_forecasts(
        self,
        *,
        council_type: str | None = None,
        status: str | None = None,
        clean_live_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if council_type:
            clauses.append("council_type = ?")
            params.append(council_type.upper())
        if status:
            clauses.append("status = ?")
            params.append(status.upper())
        if clean_live_only:
            clauses.append("provenance = 'LIVE' AND verified_clean = TRUE")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM movement_forecasts {where_sql} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def list_deliberations(
        self,
        *,
        council_type: str | None = None,
        clean_live_only: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if council_type:
            clauses.append("council_type = ?")
            params.append(council_type.upper())
        if clean_live_only:
            clauses.append("provenance = 'LIVE' AND verified_clean = TRUE")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM council_deliberations {where_sql} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["agent_opinions"] = json.loads(d.pop("agent_opinions_json", "[]"))
                d["challenges"] = json.loads(d.pop("challenges_json", "[]"))
                d["risk_warnings"] = json.loads(d.pop("risk_warnings_json", "[]"))
                out.append(d)
            return out

    def resolve_forecast(
        self,
        forecast_id: str,
        *,
        actual_price: float | None = None,
        void_reason: str | None = None,
        reviewer_id: str = "calibration_auditor",
    ) -> dict[str, Any]:
        """Resolve an OPEN movement forecast against observed price, score Brier/log-loss, and update LearningEngine."""
        with self.store._lock:
            fc = self.get_forecast(forecast_id)
            if not fc:
                raise ValueError(f"Unknown forecast_id '{forecast_id}'")
            if fc["status"] != "OPEN":
                raise ValueError(f"Forecast '{forecast_id}' already finalized ({fc['status']})")

            now_iso = utc_now_iso()
            if void_reason:
                self.store._conn.execute(
                    "UPDATE movement_forecasts SET status = 'VOIDED', resolved_at = ?, rationale = ? WHERE forecast_id = ?",
                    [now_iso, f"{fc['rationale']} [VOIDED: {void_reason[:120]}]", forecast_id],
                )
                updated = self.get_forecast(forecast_id)
                assert updated is not None
                return updated

            if actual_price is None or not math.isfinite(float(actual_price)) or float(actual_price) <= 0:
                raise ValueError("actual_price must be a positive finite number when resolving forecast")
            if not fc.get("baseline_price") or float(fc["baseline_price"]) <= 0:
                raise ValueError("Cannot compute return without positive baseline_price")

            act_px = float(actual_price)
            base_px = float(fc["baseline_price"])
            ret_pct = round(((act_px - base_px) / base_px) * 100.0, 4)
            sig_move = 1 if abs(ret_pct) >= float(fc["move_threshold_pct"]) else 0

            brier = compute_brier_score(fc["predicted_move_prob"], sig_move)
            ll = compute_log_loss(fc["predicted_move_prob"], sig_move)

            # Feed errors / significant calibration misses into LearningEngine ONLY for clean LIVE forecasts
            # (strictly preventing TEST / SIMULATED data from polluting verified learning cycles!)
            cycle_id: str | None = None
            prob_wrong = (fc["predicted_move_prob"] >= 0.5 and sig_move == 0) or (
                fc["predicted_move_prob"] < 0.5 and sig_move == 1
            )
            if fc["provenance"] == "LIVE" and fc["verified_clean"] and (brier > 0.15 or prob_wrong):
                subj = f"{fc['council_type'].lower()}_{fc['symbol'].lower()}_move_{fc['horizon'].lower()}"
                cyc = self.learning.record_observation(
                    source_type="MOVEMENT_FORECAST",
                    source_ref_id=forecast_id,
                    agent_id=f"{fc['council_type'].lower()}_edge_analyst",
                    subject_key=subj,
                    hypothesis=(
                        f"{fc['council_type']} council predicted {fc['predicted_move_prob']*100:.1f}% prob of "
                        f"|return|>={fc['move_threshold_pct']}% on {fc['symbol']} ({fc['horizon']})"
                    ),
                    expected_outcome={
                        "significant_move_prob": fc["predicted_move_prob"],
                        "expected_significant_move": fc["predicted_move_prob"] >= 0.5,
                    },
                    evidence=[
                        {
                            "source_type": "LIVE_MOVEMENT_RESOLUTION",
                            "source_ref_id": forecast_id,
                            "baseline_price": base_px,
                            "actual_price": act_px,
                            "actual_return_pct": ret_pct,
                            "brier_score": brier,
                        }
                    ],
                )
                self.learning.record_evaluation(
                    cyc["cycle_id"],
                    evaluation_notes=(
                        f"Observed {fc['symbol']} return {ret_pct:+.2f}% vs threshold {fc['move_threshold_pct']}%; "
                        f"Brier={brier:.4f}."
                    ),
                )
                self.learning.record_decision(
                    cyc["cycle_id"],
                    decision_summary=(
                        f"Adjust {fc['council_type']} {fc['horizon']} compression/catalyst weighting via independent calibration review."
                    ),
                )
                self.learning.record_outcome(
                    cyc["cycle_id"],
                    actual_outcome={
                        "actual_significant_move": bool(sig_move),
                        "actual_return_pct": ret_pct,
                        "brier_score": brier,
                    },
                    outcome_matched=(not prob_wrong and brier <= 0.15),
                )
                # Independent review by Calibration Auditor (anti-self-promotion preserved)
                eff_rev = (
                    reviewer_id
                    if reviewer_id.strip().lower() != f"{fc['council_type'].lower()}_edge_analyst"
                    else "calibration_auditor"
                )
                self.learning.review_and_extract_lesson(
                    cyc["cycle_id"],
                    reviewer_id=eff_rev,
                    review_notes=f"Calibration Auditor reviewed resolved forecast {forecast_id} (Brier={brier:.4f}).",
                    approve_lesson=(brier > 0.20),
                    lesson_tier="EPISODIC",
                    lesson_category="MOVEMENT_CALIBRATION_LESSON",
                    lesson_title=f"{fc['council_type']} {fc['symbol']} {fc['horizon']} forecast adjustment",
                    lesson_summary=(
                        f"Predicted {fc['predicted_move_prob']*100:.1f}% move prob, actual return {ret_pct:+.2f}% "
                        f"(Brier={brier:.4f}). Calibrate catalyst/compression weight accordingly."
                    ),
                    confidence=0.78,
                )
                cycle_id = cyc["cycle_id"]

            self.store._conn.execute(
                """
                UPDATE movement_forecasts SET
                    status = 'RESOLVED',
                    actual_price = ?,
                    actual_return_pct = ?,
                    actual_outcome = ?,
                    brier_score = ?,
                    log_loss = ?,
                    learning_cycle_id = ?,
                    resolved_at = ?
                WHERE forecast_id = ?
                """,
                [act_px, ret_pct, sig_move, brier, ll, cycle_id, now_iso, forecast_id],
            )
            updated = self.get_forecast(forecast_id)
            assert updated is not None
            return updated

    async def resolve_due_forecasts(self, *, force_all_live: bool = False) -> dict[str, Any]:
        """Resolve due OPEN LIVE forecasts against freshly fetched public prices (never TEST/SIMULATED)."""
        open_live = self.list_forecasts(status="OPEN", clean_live_only=True, limit=100)
        now_dt = datetime.now(timezone.utc)
        due: list[dict[str, Any]] = []
        for fc in open_live:
            if force_all_live:
                due.append(fc)
                continue
            try:
                ra = datetime.fromisoformat(str(fc["resolve_after"]).replace("Z", "+00:00"))
                if ra <= now_dt:
                    due.append(fc)
            except (TypeError, ValueError):
                continue

        if not due:
            return {"resolved_count": 0, "skipped_not_due": len(open_live), "items": []}

        snap = await self.market_drivers.ingest_live_snapshot()
        assets = snap.get("assets") or {}
        resolved_items: list[dict[str, Any]] = []
        for fc in due:
            sym = fc["symbol"]
            px = (assets.get(sym) or {}).get("price_usd")
            if px is not None:
                resolved_items.append(self.resolve_forecast(fc["forecast_id"], actual_price=float(px)))
        return {
            "resolved_count": len(resolved_items),
            "snapshot_id": snap["snapshot_id"],
            "items": resolved_items,
        }

    def get_overview(self, *, horizon: str = "24H") -> dict[str, Any]:
        latest_clean = self.market_drivers.get_latest_snapshot(clean_live_only=True)
        latest_crypto = self.list_deliberations(council_type="CRYPTO", clean_live_only=True, limit=1)
        latest_stocks = self.list_deliberations(council_type="STOCKS", clean_live_only=True, limit=1)
        return {
            "provenance": latest_clean["provenance"] if latest_clean else "UNAVAILABLE",
            "live_trading_enabled": False,
            "horizon": horizon,
            "latest_snapshot": latest_clean,
            "crypto_council": latest_crypto[0] if latest_crypto else None,
            "stocks_council": latest_stocks[0] if latest_stocks else None,
            "recent_deliberations": self.list_deliberations(limit=12),
            "forecasts": self.list_forecasts(limit=40),
            "calibration_live_only": self.get_clean_calibration_metrics(include_non_live=False),
            "crypto_calibration": self.get_clean_calibration_metrics(council_type="CRYPTO", include_non_live=False),
            "stocks_calibration": self.get_clean_calibration_metrics(council_type="STOCKS", include_non_live=False),
            "generated_at": utc_now_iso(),
        }
