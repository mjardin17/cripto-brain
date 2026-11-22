"""AI & deterministic analyst adapters for the Solana Research Desk.

Safety Invariants:
- Analysts NEVER override failed deterministic filter checks.
- If AI inference is requested and providers fail or are unavailable, raise RuntimeError
  so SolanaPaperDesk fails closed with UNVERIFIED_ANALYST_RESULT (never fabricates LLM output).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from backend.intelligence.model_router import CostAwareModelRouter
from .models import AnalystAssessment, Candidate, FilterEvidence


class DeterministicSolanaAnalyst:
    """Purely deterministic quantitative scorer ($0.00 LLM cost, explicitly labeled deterministic)."""

    provider_id = "local_deterministic"
    model_name = "deterministic-solana-v1"

    def assess(self, candidate: Candidate, evidence: FilterEvidence) -> AnalystAssessment:
        if evidence.volume_1h_usd is None or evidence.lp_locked_pct is None or evidence.top10_holders_pct is None:
            raise ValueError("Incomplete verified evidence for quantitative assessment")
        vol_score = min(1.0, math.log10(max(1.0, evidence.volume_1h_usd)) / 6.0)
        lp_score = max(0.0, min(1.0, (evidence.lp_locked_pct - 90.0) / 10.0))
        conc_score = max(0.0, min(1.0, (25.0 - evidence.top10_holders_pct) / 25.0))
        score = round(max(0.0, min(1.0, 0.45 * vol_score + 0.30 * lp_score + 0.25 * conc_score)), 4)
        rationale = (
            f"Deterministic quantitative score {score:.2f} for {candidate.symbol}: "
            f"vol_1h=${evidence.volume_1h_usd:,.0f}, lp_locked={evidence.lp_locked_pct:.1f}%, "
            f"top10={evidence.top10_holders_pct:.1f}%."
        )
        return AnalystAssessment(
            score=score,
            rationale=rationale,
            evidence_ids=list(evidence.evidence_sources[:6]),
        )


class ModelRouterSolanaAnalyst:
    """Routes Solana analyst synthesis through CostAwareModelRouter with fail-closed parsing."""

    def __init__(self, router: CostAwareModelRouter, preferred_provider: str | None = None) -> None:
        self.router = router
        self.preferred_provider = preferred_provider
        self._prepared: AnalystAssessment | None = None
        self.used_provider: str | None = None
        self.used_model: str | None = None

    async def prepare_assessment(
        self, candidate: Candidate, evidence: FilterEvidence
    ) -> AnalystAssessment:
        system_prompt = (
            "You are a conservative quantitative Solana token risk analyst. "
            "Respond ONLY with valid JSON: "
            '{"score": <float 0..1>, "rationale": "<concise 1-2 sentence rationale>"}. '
            "Never claim live trading profit guarantees."
        )
        user_prompt = json.dumps(
            {
                "mint": candidate.mint,
                "symbol": candidate.symbol,
                "pair_address": candidate.pair_address,
                "evidence": evidence.model_dump(),
            },
            sort_keys=True,
        )
        res = await self.router.synthesize_with_fallback(
            task_id=f"SOL-{candidate.mint[:8]}",
            agent_id="solana_analyst_agent",
            task_category="RESEARCH_SYNTHESIS",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            preferred_provider=self.preferred_provider,
        )
        if not res.get("ok") or not res.get("text"):
            raise RuntimeError(res.get("error") or "AI provider synthesis unavailable")

        raw_text = str(res["text"]).strip()
        match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
        if not match:
            raise ValueError("AI analyst output did not contain valid JSON")
        parsed: dict[str, Any] = json.loads(match.group(0))
        score = float(parsed["score"])
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("AI analyst score out of [0, 1] bounds")
        rationale = str(parsed.get("rationale") or "").strip()[:1800]
        if not rationale:
            raise ValueError("AI analyst returned empty rationale")

        self.used_provider = res.get("provider")
        self.used_model = res.get("model_name")
        self._prepared = AnalystAssessment(
            score=round(score, 4),
            rationale=f"[{self.used_provider}:{self.used_model}] {rationale}",
            evidence_ids=list(evidence.evidence_sources[:6]),
        )
        return self._prepared

    def assess(self, candidate: Candidate, evidence: FilterEvidence) -> AnalystAssessment:
        if self._prepared is None:
            raise RuntimeError("AI assessment not prepared or provider unavailable")
        return self._prepared
