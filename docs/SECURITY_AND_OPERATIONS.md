# Security & Operational Controls

## 1. Fail-Closed Risk Gate Invariants

The Risk Gate (`backend/risk_gate/engine.py`) is an independent deterministic verifier. If any required field is missing, NaN/Inf, stale, or invalid, the engine **fails closed (`approved = False`)**.

### Active Phase 1 & Phase 2 Controls
1. **Intra-Venue Minimum Net Edge:** `>= 1.50%` (`0.015`) after executable VWAP, taker fees, and slippage buffer.
2. **Cross-Venue Minimum Net Edge:** `>= 2.00%` (`0.020`) enforced on every Polymarket <-> Kalshi scan and paper execution (`PAPER_EXECUTION`).
3. **Liquidity Participation Cap:** `<= 10.0%` (`0.10`) of executable depth on the thinner leg (`min(yes_depth, no_depth)`).
4. **Maximum Exposure per Venue:** `<= $2,500.00` simulated/paper cap per venue (`polymarket` and `kalshi`), tracked independently and enforced on both legs.
5. **Separate Daily Loss Limits:** Tier 1 daily simulated loss limit `$250.00`; Tier 2/Tier 3 segregated and disabled (`$0.00`).
6. **Stale-Data Rejection (Nov 16 Correction):** Computes canonical `data_age_ms = fetch_age_ms` across legs (`-1` if missing/invalid/future-skewed beyond `5s`) and rejects any snapshot outside `[0, 15,000 ms]`. Exchange order-book `clob_age_ms` is recorded strictly as diagnostic metadata so quiet markets with unchanged resting books do not falsely trigger stale blocks when the HTTP fetch itself succeeded within bounds.
7. **Partial-Fill & Leg-Risk Guard:** Rejects trades where leg liquidity imbalance ratio exceeds `5:1`, identical settlement is unverified, or partial-fill protection is disabled.
8. **Kill Switch:** Persistent global halt flag stored in DuckDB `system_state`. When active, **all** Risk Gate evaluations, cross-venue paper simulations, Solana paper entries/evaluations, and Supervisor task dispatches halt (`DENIED (KILL_SWITCH)`).
9. **Immutable Audit Trail:** Every evaluation (approved or denied) is atomically recorded to `risk_gate_audit` with the exact rule checks and reason codes.

---

## 2. Credential, RBAC & Execution Isolation

- **Stage 4A Authentication & Deny-by-Default RBAC:** Scrypt password hashing, server-side DuckDB sessions, HttpOnly `SameSite=Lax` cookies, CSRF double-submit verification (`X-CSRF-Token`), brute-force lockouts, and four immutable role tiers (`READ_ONLY`, `ANALYST`, `OPERATOR`, `OWNER`).
- **No Private Keys in Frontend or Backend:** Polymarket, Kalshi, and Solana discovery operate strictly on unauthenticated public read-only APIs. Zero wallet private keys, mnemonics, or TX signing code exist.
- **No Live Trading Endpoints:** `live_trading_enabled = false` permanently across arbitrage, councils, and Solana paper desks.
- **Multi-Provider AI Cap & Fail-Closed Boundaries:** `CostAwareModelRouter` defaults to `$0.00` deterministic execution first, supports free/open-weight providers (`ollama`, `groq`, `openrouter :free`, `huggingface`), caps paid economical Gemini fallback at `$0.01/task` & `$0.50/day`, never exposes env keys to the frontend, and never fabricates offline availability.
- **Solana Paper Desk Thresholds:** Enforces revoked mint & freeze authorities, `>= 95%` LP lock/burn, `<= 20%` top-10 holder concentration, `>= $50,000` 1h volume, verified Jupiter read-only sell route quote, clear developer wallet risk, 8% max position notional on `$280` initial simulated cash, one entry per mint, `-6%` hard stop, and `+12% -> -6%` trailing stop. Missing or timed-out checks remain `None` and fail closed.

---

## 3. Operational Runbook

- **Start Stack:** `npm run dev` starts `server.ts` on `:3000`, spawns `uvicorn backend.main:app --host 127.0.0.1 --port 8010`, and verifies `/api/health`.
- **Run Backend Tests:** `CERTAINTY_DUCKDB_PATH=:memory: .venv/bin/pytest -v backend/tests`
- **Run Frontend / Logic Tests:** `npx --yes tsx --test src/utils/formatters.test.ts`
- **Emergency Halt:** Toggle the **Kill Switch** in the Command Center or Risk Gate tab (`POST /api/risk-gate/kill-switch`).
