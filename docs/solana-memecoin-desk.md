# Solana Memecoin Desk — Phase 1 (Paper-Only Core)

## Scope
A separate research and paper-trading module inside The Certainty Stack. Existing Polymarket/Kalshi scanners, Risk Gate, auth, memory, and paper simulator are not replaced.

## Six-agent handoff
1. **SCOUT** — future provider adapter discovers Solana pairs and emits typed candidates.
2. **FILTER** — deterministic gate. Missing/unverifiable evidence rejects the candidate.
3. **ANALYST** — provider adapter (planned: xAI Grok) returns a validated 0–1 score and rationale. Provider failure rejects.
4. **EXECUTOR** — Phase 1 only authorizes a paper quote up to 8% of current paper cash. No wallet signer or transaction submitter exists.
5. **RISK** — paper positions use a -6% hard stop; after +12% gain, a 6% drawdown from peak triggers a paper exit.
6. **LOGGER** — structured in-memory events in this first vertical slice; persistent DuckDB audit and UI/API integration are follow-up work.

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

## Next milestones
1. Add provider-backed pair discovery and timestamped on-chain evidence adapters.
2. Add xAI analyst adapter using structured output and strict timeouts/cost caps.
3. Persist candidate, evidence, decision, paper order, fill, position, and PnL events in DuckDB.
4. Add authenticated FastAPI endpoints and a dedicated frontend workspace tab.
5. Replay historical and adversarial cases, including stale data, honeypots, failed sells, thin pools, price gaps, provider outages, and kill-switch activation.
6. Keep live execution disabled unless separately reviewed and explicitly authorized in a future phase.
