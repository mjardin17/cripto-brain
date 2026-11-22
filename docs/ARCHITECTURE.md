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
│  │     ├── pair_review.py        Side-by-side rules review & reject     │
│  │     ├── council.py           STOCKS/CRYPTO deliberation teams    │
│  │     ├── pattern_intelligence Historical event analogs->predict  │
│  │     ├── fluctuation_analyzer  Cause attribution + next-move timing  │
│  │     ├── curated_history.py    Vetted real-event analog import      │
│  │     └── cross_venue.py        Cross-venue route VWAP & scanner       │
│  │                                                                      │
│  ├── backend/execution/          Simulated Execution Engine             │
│  │     └── paper_simulator.py    Paper orders, drift, partial & unwind  │
│  │                                                                      │
│  ├── backend/future/             Module Registry & Inactive Tier 2      │
│  │     └── modules.py            Active subsystems & Tier 2 stub        │
│  │                                                                      │
│  ├── backend/intelligence/       Unified AI Workforce & Learning Core   │
│  │     ├── supervisor.py         Lease-fenced 5-role orchestrator       │
│  │     ├── agents.py             Polymarket, Kalshi, Solana & Risk roles│
│  │     ├── learning_engine.py    10-step observe->verify->rollback loop │
│  │     ├── memory_manager.py     Versioned tiered memory                │
│  │     ├── model_router.py       Multi-provider free/budget-capped AI   │
│  │     ├── councils.py           Separate Crypto & Stocks AI Councils   │
│  │     └── validation_labs.py    Soak, concurrency & adversarial labs   │
│  │                                                                      │
│  ├── backend/solana_desk/        Solana Memecoin Research & Paper Desk  │
│  │     ├── providers.py          DexScreener/Gecko/RPC/RugCheck/Jupiter │
│  │     ├── filters.py            Fail-closed deterministic token checks │
│  │     ├── analyst.py            ModelRouter-backed & deterministic eval│
│  │     ├── risk.py               8% cap, -6% hard & +12%->-6% trailing  │
│  │     └── engine.py             PersistentSolanaDesk + DuckDB ledger   │
│  │                                                                      │
│  └── backend/storage/            DuckDB Analytical, Audit & Memory      │
│        └── duckdb_store.py       Thread-safe schema, audit, paper ledger│
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. DuckDB Data Model (`data/certainty_stack.duckdb`)

1. **`market_snapshots`** & **`arbitrage_opportunities`**: Polymarket Gamma + CLOB V2 books and net-edge evaluations (`data_age_ms = fetch_age_ms` per Nov 16 correction).
2. **`cross_venue_pairs`** & **`cross_venue_opportunities`**: Polymarket <-> Kalshi settlement assessments (`identical_settlement_verified`) and dual-route spread scans.
3. **`paper_orders`**, **`paper_fills`**, **`paper_positions`**, & **`paper_pnl_ledger`**: Cross-venue simulated paper blotter (`provenance='SIMULATED'`).
4. **`solana_candidates`**, **`solana_paper_positions`**, & **`solana_paper_events`**: Solana pool discovery, timestamped raw safety evidence, fail-closed evaluations, simulated positions, and six-agent logs.
5. **`risk_gate_audit`**: Immutable authorization log (`APPROVED`/`DENIED` with rule checks and Kill Switch state).
6. **`memory_entries`**, **`memory_versions`**, **`learning_cycles`**, **`generated_regression_cases`**, & **`model_routing_log`**: Structured versioned memory, 10-step learning lifecycle (including `ROLLED_BACK`), regression guards, and multi-provider AI spend/rate-limit telemetry.
7. **`users`**, **`sessions`**, **`security_audit_logs`**, & **`system_state`**: Stage 4A authentication, RBAC sessions, immutable security audit log, and `kill_switch_active`.

---

## 4. Phase Roadmap & Current State

- **Phase 0–2 (Complete):** Fail-closed Risk Gate, Polymarket & Kalshi read-only scanners, settlement matcher, and deterministic Cross-Venue Paper Simulator.
- **Stage 3A–4A (Active):** Supervisor 5-role workforce, Stocks & Crypto AI Councils with market drivers, Brier calibration, 10-step Learning Core (with continuous regression monitoring and rollback), Validation Labs, and full RBAC authentication.
- **Unified Milestone (Active):** Integrated `backend/solana_desk/` (`SCOUT -> FILTER -> ANALYST -> EXECUTOR -> RISK -> LOGGER`), real read-only Solana adapters, DuckDB simulated ledger, and multi-provider free/open-weight AI ecosystem (`local_deterministic`, `ollama`, `groq`, `openrouter`, `huggingface`, `gemini`).
- **Future Module (Gated & Inactive):** Tier 2 Crypto Delta-Neutral & Carry remains `INACTIVE` / `UNAVAILABLE` with zero live execution.
