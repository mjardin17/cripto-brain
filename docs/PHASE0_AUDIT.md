# Phase 0 Audit — Dependencies, Exchange APIs, and Research Verification

**Audit Date:** 2026-11-12  
**Scope:** Workspace repositories, system runtimes, Python/Node packages, Polymarket Gamma & CLOB V2 APIs, Kalshi V2 API, and fee/arbitrage claims.  
**Reconciliation Note:** Historical Phase 0 baseline snapshot. Kalshi V2 read-only, Cross-Venue Paper Simulator, Stage 3-4A Authentication/Workforce/Learning Core, the Nov 16 `data_age_ms = fetch_age_ms` freshness correction, and the Solana Research Desk are now active; see `docs/ARCHITECTURE.md` and `docs/SECURITY_AND_OPERATIONS.md` for current invariants.

---

## 1. Workspace & Runtime Audit

| Component | Observed State | Limitation / Mitigation |
|---|---|---|
| Node.js / Express / Vite | Node v22+, Vite 8.3.0, React 19.0.1, Tailwind CSS 4.3.3, TypeScript 7.0.2, `tsx` 4.21.0 | Runtime preview binds exclusively to port `3000`, while container `control-plane-api` occupies `:8000`. Solution: `server.ts` serves Vite/dist on `:3000`, supervises FastAPI on `127.0.0.1:8010`, and reverse-proxies `/api/*`. |
| Python Runtime | Python 3.11.2 pre-installed; `pip3` and `venv` initially absent | Installed `python3-pip` and `python3-venv` via `apt-get`, creating isolated `.venv` for FastAPI, DuckDB, httpx, Pydantic, and Pytest. |
| PostgreSQL (Operational State) | Not provisioned in local Phase 1 container | Phase 1 uses DuckDB for market snapshots, arbitrage opportunities, and audit logs; PostgreSQL operational schema is specified for production deployment (`docs/ARCHITECTURE.md`). |
| Supplied Repositories | Clean greenfield workspace (`package.json`, `vite.config.ts`, `src/App.tsx`, `src/main.tsx`, `src/index.css`) | No pre-existing trading modules or legacy SDKs present. |

---

## 2. Exchange API Verification (Live Probes)

### A. Polymarket Gamma API (`https://gamma-api.polymarket.com`)
- **Authentication:** Public read-only access (no API key required).
- **Verified Endpoint:** `GET /markets?active=true&closed=false&limit=...`
- **Verified Payload Fields:**
  - `id`, `question`, `conditionId`, `slug`, `endDate`, `resolutionSource`, `description`
  - `outcomes` (JSON-encoded string array, e.g. `["Yes", "No"]`)
  - `clobTokenIds` (JSON-encoded string array of 77-digit ERC-1155 token IDs matching `outcomes`)
  - `enableOrderBook` (boolean), `acceptingOrders` (boolean)
  - `negRisk` (boolean), `negRiskMarketID`, `negRiskOther`
  - `feesEnabled` (boolean), `feeType` (e.g., `"politics_fees"`), `makerBaseFee`, `takerBaseFee`
  - `feeSchedule` (e.g., `{"exponent": 1, "rate": 0.04, "takerOnly": true, "rebateRate": 0.25}`)
- **Limitation Observed:** `outcomes` and `clobTokenIds` are returned as JSON-encoded strings rather than native arrays and must be parsed defensively. Non-binary or `negRisk` multi-outcome markets require identical settlement verification before pairing.

### B. Polymarket CLOB V2 API (`https://clob.polymarket.com`)
- **Authentication:** Read-only endpoints (`/time`, `/book`, `/fee-rate`, `/midpoint`) are public. Trading requires L1 EIP-712 wallet signature + L2 API credentials (`py-clob-client-v2`). **No live credentials or signing keys are configured or permitted in Phase 1.**
- **Verified Endpoints:**
  - `GET /time` → returned epoch timestamp `1794538507`
  - `GET /book?token_id=<id>` → returned `market`, `asset_id`, `timestamp` (ms string), `hash`, `bids`, `asks`, `min_order_size`, `tick_size`, `neg_risk`
  - `GET /fee-rate?token_id=<id>` → returned `{"base_fee": 1000}`
- **CRITICAL Findings from Live Inspection:**
  1. **User-Agent / Cloudflare Filtering:** Python's default `urllib` (`Python-urllib/3.10`) receives `HTTP 403 Forbidden` from `clob.polymarket.com/book`. `curl` and `httpx` with an explicit standard HTTP client User-Agent succeed with `HTTP 200`.
  2. **Order Book Level Ordering:** Live `/book` responses return `bids` sorted **ascending** (worst bid first, e.g., `0.001` ... `0.016`) and `asks` sorted **descending** (worst ask first, e.g., `0.999` ... `0.985`). Taking `asks[0]` without sorting yields `$0.999` instead of the true best ask! **Our CLOB parser explicitly sorts `bids` descending and `asks` ascending by price.**
  3. **Geoblocking:** Order submission on Polymarket CLOB is geo-restricted for US/UK/select jurisdictions; Phase 1 performs strictly read-only public market-data ingestion.
  4. **SDK Audit:** Legacy `py-clob-client` (V1) is deprecated in favor of `py-clob-client-v2`. Because Phase 1 is strictly read-only, we use `httpx` directly against the verified REST endpoints, eliminating unnecessary wallet/signing dependencies until an authorized execution phase.

### C. Kalshi V2 API (`https://api.elections.kalshi.com/trade-api/v2`)
- **Verified Endpoint:** `GET /markets?limit=1` responded with `HTTP 200` and live contract payloads.
- **Phase 1 Status:** **Not active.** Scheduled for Phase 2 (Kalshi read-only integration + cross-venue semantic/settlement matching + cross-venue paper execution).

---

## 3. Research & Arbitrage Math Verification

1. **Guaranteed Payout Invariant:** A binary outcome pair (`YES` + `NO`) on the same Polymarket `conditionId` settles to exactly `$1.00` USDC at resolution (`1.00 * Q` for `Q` matched pairs), assuming identical settlement condition and non-void market resolution.
2. **Displayed Midpoint Trap:** Gamma `outcomePrices` reflect last/midpoint quotes, **not** executable liquidity. Arbitrage viability must be computed by walking the **actual CLOB V2 ask books** for both `YES` and `NO` tokens for a target share size `Q` to obtain executable Volume-Weighted Average Prices (VWAP).
3. **Polymarket Taker Fee Formula:** Per verified `feeSchedule` (`takerOnly: true`, `rate`, `exponent`):
   - When `feesEnabled` is `true`, taker orders pay:
     $$\text{Fee}(Q, p) = Q \times \text{rate} \times p \times (1 - p)$$
     (rounded to 5 decimal places in USDC; maker resting orders pay `$0.00` but incur non-fill / adverse selection / leg risk).
   - For immediate-fill taker simulation (walking both ask books), taker fees on both the `YES` and `NO` legs must be added to the combined execution cost, alongside estimated slippage and gas/settlement buffer.
4. **Leg-Risk & Partial-Fill Hazard:** Even on a single venue, submitting separate `YES` and `NO` orders does not guarantee atomic simultaneous fills. Phase 1 flags liquidity imbalance between the `YES` and `NO` top-of-book depth and enforces a Risk Gate liquidity participation cap (`<= 10%` of available depth at target price levels).
