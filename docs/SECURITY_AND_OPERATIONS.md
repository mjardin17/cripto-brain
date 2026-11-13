# Security & Operational Controls

## 1. Fail-Closed Risk Gate Invariants

The Risk Gate (`backend/risk_gate/engine.py`) is an independent deterministic verifier. If any required field is missing, NaN/Inf, stale, or invalid, the engine **fails closed (`approved = False`)**.

### Active Phase 1 & Phase 2 Controls
1. **Intra-Venue Minimum Net Edge:** `>= 1.50%` (`0.015`) after executable VWAP, taker fees, and slippage buffer.
2. **Cross-Venue Minimum Net Edge:** `>= 2.00%` (`0.020`) enforced on every Polymarket <-> Kalshi scan and paper execution (`PAPER_EXECUTION`).
3. **Liquidity Participation Cap:** `<= 10.0%` (`0.10`) of executable depth on the thinner leg (`min(yes_depth, no_depth)`).
4. **Maximum Exposure per Venue:** `<= $2,500.00` simulated/paper cap per venue (`polymarket` and `kalshi`), tracked independently and enforced on both legs.
5. **Separate Daily Loss Limits:** Tier 1 daily simulated loss limit `$250.00`; Tier 2/Tier 3 segregated and disabled (`$0.00`).
6. **Stale-Data Rejection:** Computes canonical `data_age_ms = max(clob_age_ms, fetch_age_ms)` across legs (`-1` if missing/invalid/future-skewed beyond `5s`) and rejects any snapshot outside `[0, 15,000 ms]`.
7. **Partial-Fill & Leg-Risk Guard:** Rejects trades where leg liquidity imbalance ratio exceeds `5:1`, identical settlement is unverified, or partial-fill protection is disabled.
8. **Kill Switch:** Persistent global halt flag stored in DuckDB `system_state`. When active, **all** Risk Gate evaluations return `DENIED (KILL_SWITCH)`.
9. **Immutable Audit Trail:** Every evaluation (approved or denied) is atomically recorded to `risk_gate_audit` with the exact rule checks and reason codes.

---

## 2. Credential & Execution Isolation

- **No Private Keys in Frontend or Backend:** Phase 1 & Phase 2 operate strictly on unauthenticated public read-only APIs (`gamma-api.polymarket.com`, `clob.polymarket.com`, and `api.elections.kalshi.com/trade-api/v2`).
- **No Live Trading Endpoints:** No `/api/orders/live`, withdrawal, deposit, RSA-PSS signing, or EIP-712 signing functions exist; `live_trading_enabled = false` permanently.
- **Separate Tier Capital Isolation:** Tier 1 (Prediction Arbitrage), Tier 2 (Crypto Delta-Neutral), and Tier 3 (AI Market Research) maintain segregated virtual capital buckets and credential namespaces.
- **AI Authority Boundary:** Future Tier 3 AI agents may only *reduce* position size or *reject* an opportunity; they can never force or override Risk Gate authorization.

---

## 3. Operational Runbook

- **Start Stack:** `npm run dev` starts `server.ts` on `:3000`, spawns `uvicorn backend.main:app --host 127.0.0.1 --port 8010`, and verifies `/api/health`.
- **Run Backend Tests:** `.venv/bin/pytest -v backend/tests`
- **Run Frontend / Logic Tests:** `npx --yes tsx --test src/utils/formatters.test.ts`
- **Emergency Halt:** Toggle the **Kill Switch** in the Command Center or Risk Gate tab (`POST /api/risk-gate/kill-switch`).
