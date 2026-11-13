# The Certainty Stack — System Architecture

## 1. Architectural Principles

1. **Fail-Closed Deterministic Risk Gate:** Every trade intent or strategy proposal must pass an independent Python service (`backend/risk_gate/engine.py`) before any simulated or future execution. No frontend state, AI module, or strategy engine can override or bypass a Risk Gate denial.
2. **Strict Data Provenance:** Every metric, balance, order book, and P&L number carries an explicit provenance classification:
   - `LIVE`: Real-time or freshly fetched data from verified external APIs (Polymarket Gamma, Polymarket CLOB V2).
   - `SIMULATED`: Paper-trading allocations, simulated execution outputs, or dry-run Risk Gate evaluations.
   - `UNAVAILABLE`: Modules or data feeds belonging to future phases (Kalshi Phase 3, Paper Execution Phase 2, Tier 2 Crypto Delta-Neutral, Tier 3 AI Research).
3. **Zero Live Execution in Phase 1:** No private keys, wallet mnemonics, signing endpoints, or order-submission clients exist in the codebase.

---

## 2. Service Topology

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Browser Client (React + TypeScript + Tailwind CSS v4)                   │
│  - Command Center · Scanner & Depth · Risk Gate & Audit · Architecture  │
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
│ Quantitative Backend (Python 3.10 + FastAPI on 127.0.0.1:8010)          │
│                                                                         │
│  ├── backend/risk_gate/        Independent Fail-Closed Risk Gate        │
│  │     ├── policy.py           Immutable default bounds & Kill Switch   │
│  │     ├── engine.py           Deterministic authorization evaluator    │
│  │     └── audit.py            Append-only audit persistence            │
│  │                                                                      │
│  ├── backend/venues/           Venue Adapters                           │
│  │     ├── polymarket.py       Gamma + CLOB V2 read-only client         │
│  │     └── kalshi_stub.py      Phase 3 interface contract (inactive)    │
│  │                                                                      │
│  ├── backend/arbitrage/        Tier 1 Binary Arbitrage Engine           │
│  │     ├── calculator.py       Sorted book VWAP, fee, slippage, edge    │
│  │     └── scanner.py          Gamma discovery + CLOB batch evaluator   │
│  │                                                                      │
│  ├── backend/memory/           Persistent Agent Memory & Escalation     │
│  │     └── store.py            Lessons, error logs, blocker records     │
│  │                                                                      │
│  ├── backend/future/           Inactive Phase 5 Blueprints              │
│  │     ├── tier2_delta_neutral.py   (Disabled stub)                     │
│  │     └── tier3_ai_research.py     (Disabled stub)                     │
│  │                                                                      │
│  └── backend/storage/          DuckDB Analytical & Audit Persistence    │
│        └── duckdb_store.py     Thread-safe DuckDB schema & queries      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. DuckDB Data Model (`data/certainty_stack.duckdb`)

1. **`market_snapshots`**:
   - Captures every scanned Polymarket market condition (`scan_id`, `market_id`, `condition_id`, `question`, `slug`, `yes_token_id`, `no_token_id`, `yes_best_ask`, `no_best_ask`, `yes_best_bid`, `no_best_bid`, `yes_ask_depth_usd`, `no_ask_depth_usd`, `fee_rate`, `fees_enabled`, `neg_risk`, `clob_timestamp_ms`, `ingested_at`).
2. **`arbitrage_opportunities`**:
   - Logs every evaluated pair (`opp_id`, `scan_id`, `market_id`, `condition_id`, `question`, `target_shares`, `yes_vwap`, `no_vwap`, `combined_raw_cost`, `taker_fees_usd`, `slippage_buffer_usd`, `total_executable_cost`, `guaranteed_payout`, `gross_edge_pct`, `net_edge_pct`, `max_participating_shares`, `risk_gate_approved`, `risk_gate_reason`, `data_age_ms`, `evaluated_at`).
3. **`risk_gate_audit`**:
   - Immutable authorization log (`audit_id`, `request_id`, `venue`, `strategy_tier`, `market_id`, `requested_notional_usd`, `net_edge_pct`, `liquidity_participation_pct`, `data_age_ms`, `decision`, `failed_rules`, `kill_switch_active`, `decided_at`).
4. **`agent_memory`**:
   - Provider-independent persistent knowledge & blocker escalation (`memory_id`, `category`, `title`, `summary`, `evidence_json`, `status`, `created_at`).
5. **`system_state`**:
   - Key-value operational state (e.g., `kill_switch_active`, `daily_simulated_loss_usd`).

---

## 4. Phase Roadmap & Current Gate

- **Phase 0 (Complete):** Workspace, dependency, Polymarket Gamma/CLOB V2 API, and fee math audit.
- **Phase 1 (Current Deliverable):** Fail-closed Risk Gate, Polymarket read-only ingestion, Tier 1 binary arbitrage scanner, DuckDB persistence, Command Center UI, and automated Pytest + TypeScript tests.
- **Phase 2 (Gated on Owner Approval):** Paper Execution Simulator (limit orders, partial fills, latency, unwinds, second-leg failure).
- **Phase 3 (Gated):** Read-only Kalshi V2 integration & cross-venue settlement matching.
- **Phase 4 (Gated):** Extended Tier 1 paper validation.
- **Phase 5 (Gated):** Tier 2 Crypto Delta-Neutral & Tier 3 AI Research modules (presently inactive).
