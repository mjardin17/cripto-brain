# The Certainty Stack — System Architecture

## 1. Architectural Principles

1. **Fail-Closed Deterministic Risk Gate:** Every trade intent or strategy proposal must pass an independent Python service (`backend/risk_gate/engine.py`) before any simulated or future execution. No frontend state, AI module, or strategy engine can override or bypass a Risk Gate denial.
2. **Strict Data Provenance:** Every metric, balance, order book, and P&L number carries an explicit provenance classification:
   - `LIVE`: Real-time or freshly fetched data from verified external APIs (Polymarket Gamma, Polymarket CLOB V2, Kalshi Trade API v2).
   - `SIMULATED`: Paper-trading allocations, cross-venue simulated orders/fills/unwinds/P&L, or dry-run Risk Gate evaluations.
   - `UNAVAILABLE`: Modules or data feeds belonging to inactive future phases (Tier 2 Crypto Delta-Neutral, Tier 3 AI Research).
3. **Zero Live Execution:** `live_trading_enabled = false` permanently. No private keys, wallet mnemonics, signing endpoints, deposits, or live order-submission clients exist in the codebase.

---

## 2. Service Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Browser Client (React + TypeScript + Tailwind CSS v4)                   │
│  - Command Center · Scanner & Depth · Cross-Venue & Paper · Risk · Docs │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ HTTP /api/* (same-origin port 3000)
┌───────────────────────────────▼─────────────────────────────────────────┐
│ Edge Proxy & Asset Server (server.ts — Node/Express on :3000)           │
│  - Serves Vite dev middleware (dev) or built SPA dist/ (prod)           │
│  - Spawns & supervises FastAPI backend on 127.0.0.1:8010                │
│  - Proxies /api/* -> http://127.0.0.1:8010/api/*                        │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ HTTP REST
┌───────────────────────────────▼─────────────────────────────────────────┐
│ Quantitative Backend (Python 3.11 + FastAPI on 127.0.0.1:8010)          │
│                                                                         │
│  ├── backend/main.py             FastAPI routes & DuckDB lifecycle      │
│  ├── backend/risk_gate/          Independent Fail-Closed Risk Gate      │
│  │     ├── policy.py             Immutable default bounds & policy      │
│  │     └── engine.py             Deterministic authorization evaluator  │
│  │                                                                      │
│  ├── backend/venues/             Venue Adapters                         │
│  │     ├── polymarket.py         Gamma + CLOB V2 read-only client       │
│  │     └── kalshi.py             Kalshi Trade API v2 read-only client   │
│  │                                                                      │
│  ├── backend/arbitrage/          Tier 1 Binary Arbitrage Engine         │
│  │     ├── calculator.py         Book VWAP, fee, freshness, edge math   │
│  │     ├── scanner.py            Polymarket discovery + batch evaluator │
│  │     ├── matcher.py            Fail-closed settlement matcher         │
│  │     ├── pair_discovery.py     Poly<->Kalshi candidate pair registry  │
│  │     └── cross_venue.py        Cross-venue route VWAP & scanner       │
│  │                                                                      │
│  ├── backend/execution/          Simulated Execution Engine             │
│  │     └── paper_simulator.py    Paper orders, drift, partial & unwind  │
│  │                                                                      │
│  ├── backend/future/             Module Registry & Inactive Tier 2/3    │
│  │     └── modules.py            Phase 2 + inactive Tier 2/3 contracts  │
│  │                                                                      │
│  └── backend/storage/            DuckDB Analytical, Audit & Memory      │
│        └── duckdb_store.py       Thread-safe schema, audit, paper ledger│
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. DuckDB Data Model (`data/certainty_stack.duckdb`)

1. **`market_snapshots`**:
   - Captures every scanned Polymarket market condition (`snapshot_id`, `scan_id`, `market_id`, `condition_id`, `question`, `slug`, `yes_token_id`, `no_token_id`, `yes_best_ask`, `no_best_ask`, `yes_best_bid`, `no_best_bid`, `yes_ask_depth_shares`, `no_ask_depth_shares`, `fee_rate`, `fees_enabled`, `neg_risk`, `clob_timestamp_ms`, `book_payload_json`, `ingested_at`).
2. **`arbitrage_opportunities`**:
   - Logs every evaluated Polymarket intra-venue pair (`opp_id`, `scan_id`, `market_id`, `condition_id`, `question`, `slug`, `venue`, `target_shares`, `yes_vwap`, `no_vwap`, `combined_raw_cost`, `taker_fees_usd`, `slippage_buffer_usd`, `total_executable_cost`, `guaranteed_payout`, `gross_edge_pct`, `net_edge_pct`, `liquidity_participation_pct`, `leg_imbalance_ratio`, `executable`, `risk_gate_approved`, `risk_gate_reason`, `data_age_ms`, `evaluated_at`).
3. **`cross_venue_pairs`** & **`cross_venue_opportunities`**:
   - Stores registered Polymarket <-> Kalshi contract pairs with fail-closed settlement verification checks (`identical_settlement_verified`, `settlement_status`, `cutoff_delta_hours`, `mismatch_reasons_json`) and live dual-route cross-venue scan evaluations.
4. **`paper_orders`**, **`paper_fills`**, **`paper_positions`**, & **`paper_pnl_ledger`**:
   - Persists all simulated paper orders, leg fills, resting-bid unwinds (`is_unwind=TRUE`), open hedged/unhedged exposures (`OPEN_HEDGED`, `UNWOUND`, `OPEN_UNHEDGED`), and realized unwind P&L / locked carry (`provenance='SIMULATED'`).
5. **`risk_gate_audit`**:
   - Immutable authorization log (`audit_id`, `request_id`, `venue`, `strategy_tier`, `execution_mode`, `market_id`, `requested_notional_usd`, `net_edge_pct`, `liquidity_participation_pct`, `data_age_ms`, `decision`, `failed_rules_json`, `kill_switch_active`, `summary`, `decided_at`).
6. **`agent_memory`**:
   - Provider-independent persistent knowledge & blocker escalation (`memory_id`, `category`, `title`, `summary`, `evidence_json`, `status`, `created_at`).
7. **`system_state`**:
   - Key-value operational state (e.g., `kill_switch_active`).

---

## 4. Phase Roadmap & Current Gate

- **Phase 0 (Complete):** Workspace, dependency, Polymarket Gamma/CLOB V2 API, and fee math audit.
- **Phase 1 (Complete):** Fail-closed Risk Gate, Polymarket read-only ingestion, Tier 1 binary arbitrage scanner, DuckDB persistence, Command Center UI, and automated Pytest + TypeScript tests.
- **Phase 2 (Active — Stages 2A–2D Complete):** Read-only Kalshi Trade API v2 integration, complement-symmetry YES/NO normalization, fail-closed cross-venue settlement matching, and deterministic Paper Execution Simulator (limit IOC orders, partial fills, latency/drift, second-leg failures, bid-book unwinds, and DuckDB paper blotter).
- **Future Tiers (Gated & Inactive):** Tier 2 Crypto Delta-Neutral & Tier 3 AI Market Research (presently inactive; AI outputs may only reduce size or reject, never authorize trades).
