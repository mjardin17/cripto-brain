# Solana Memecoin Desk — Phase 1 (Paper-Only Core)

## Scope
A separate research and paper-trading module inside The Certainty Stack. Existing Polymarket/Kalshi scanners, Risk Gate, auth, memory, and paper simulator are not replaced.

## Six-agent handoff
1. **SCOUT** — `DexScreenerSolanaAdapter` & `GeckoTerminalSolanaAdapter` discover Solana pairs and resolve pool + mint addresses.
2. **FILTER** — deterministic fail-closed gate combining `SolanaRpcReadOnlyAdapter` (mint/freeze authority, supply & top-10 concentration), `RugCheckReadOnlyAdapter` (LP lock/burn & dev risk), and `JupiterQuoteReadOnlyAdapter` (read-only sell quote probe). Missing or errored checks reject.
3. **ANALYST** — `ModelRouterSolanaAnalyst` routes through `CostAwareModelRouter` (Ollama, Groq, OpenRouter, HF, Gemini fallback) or explicit `DeterministicSolanaAnalyst` and enforces a bounded `[0, 1]` score and rationale. Provider failure rejects fail-closed (`UNVERIFIED_ANALYST_RESULT`).
4. **EXECUTOR** — strictly paper-only up to 8% of current simulated cash balance, logging to `risk_gate_audit`. Zero wallet signer or TX submitter exists.
5. **RISK** — paper positions enforce a -6% hard stop; after +12% gain, a 6% drawdown from peak triggers a paper exit, recording outcomes into `LearningEngine`.
6. **LOGGER** — persists structured six-agent events (`solana_paper_events`), candidates (`solana_candidates`), and simulated positions (`solana_paper_positions`) in DuckDB.

## Fail-closed filter thresholds
- Mint authority verified revoked.
- Freeze authority verified revoked.
- LP locked/burned at least 95%.
- Top ten holders at or below 20%.
- 1-hour volume at least $50,000.
- Sell simulation explicitly passes.
- Developer wallet risk explicitly clear.
- Unknown, stale, malformed, missing, NaN, or infinite evidence rejects.

## Important corrections to the supplied blueprint
- A profile feed is not proof of a newly created liquid pair; discovery must resolve and validate actual pool addresses and token mints.
- Mint/freeze boolean flags cannot be trusted unless the adapter records source, timestamp, and raw evidence.
- A 6% stop is a trigger policy, not a guaranteed maximum loss. Thin liquidity, failed transactions, MEV, outages, and price gaps can produce worse exits.
- A fixed 1.5% slippage assumption is not a fill model. Paper fills must model pool depth, price impact, fees, priority fees, and failed sells.
- LLM scoring is advisory only. It cannot approve a candidate that failed deterministic checks or override the independent Risk Gate.

## Explicit non-goals in Phase 1
No private keys, wallet connection, Jupiter transaction submission, live trading, deposits, withdrawals, or claims of profitability. The reported "$280 to $3,412 overnight" result is unverified and is not a performance expectation.

## Integrated Milestones (Unified Platform)
- Completed: Public read-only adapters (`DexScreener`, `GeckoTerminal`, `Solana RPC`, `RugCheck`, `Jupiter` quote-only) in `backend/solana_desk/providers.py`.
- Completed: Multi-provider `ModelRouterSolanaAnalyst` with fail-closed fallback (`UNVERIFIED_ANALYST_RESULT` on provider outage).
- Completed: DuckDB persistence (`PersistentSolanaDesk`) for candidates, simulated positions, cash balance, and six-agent event trail.
- Completed: Authenticated RBAC-protected FastAPI routes (`/api/solana/*`), Supervisor workforce tasks, and dedicated UI workspace view (`Solana Research Desk`).
- Permanent Lock: Live execution remains disabled unless separately reviewed and explicitly authorized in a future phase.
