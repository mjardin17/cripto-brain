export type DataProvenance = 'LIVE' | 'SIMULATED' | 'UNAVAILABLE' | 'LIVE_SNAPSHOT';

export interface VenueExposure {
  venue: string;
  mode: string;
  provenance: DataProvenance;
  current_exposure_usd: number;
  max_cap_usd: number;
  daily_loss_limit_usd: number;
}

export interface RiskPolicyConfig {
  min_intra_venue_edge_pct: number;
  min_cross_venue_edge_pct: number;
  max_liquidity_participation_pct: number;
  max_venue_exposure_usd: Record<string, number>;
  daily_loss_limits_usd: Record<string, number>;
  max_data_age_ms: number;
  max_leg_imbalance_ratio: number;
  live_trading_enabled: boolean;
  enabled_tiers: string[];
  enabled_venues: string[];
}

export interface KillSwitchState {
  active: boolean;
  reason?: string;
  actor?: string;
  updated_at?: string;
}

export interface ModuleRegistryItem {
  module_id: string;
  name: string;
  tier: string;
  phase: string;
  status: string;
  execution_capability: string;
  provenance: DataProvenance;
  description: string;
  metrics: Record<string, number>;
}

export interface ArbitrageOpportunity {
  opp_id: string;
  scan_id: string;
  market_id: string;
  condition_id: string;
  question: string;
  slug: string;
  venue: string;
  target_shares: number;
  yes_vwap: number | null;
  no_vwap: number | null;
  combined_raw_cost: number | null;
  taker_fees_usd: number;
  slippage_buffer_usd: number;
  total_executable_cost: number | null;
  guaranteed_payout: number;
  gross_edge_pct: number | null;
  net_edge_pct: number | null;
  liquidity_participation_pct: number;
  leg_imbalance_ratio: number;
  executable: boolean;
  risk_gate_approved: boolean;
  risk_gate_reason: string;
  data_age_ms: number;
  evaluated_at: string;
}

export interface RiskAuditEntry {
  audit_id: string;
  request_id: string;
  venue: string;
  strategy_tier: string;
  execution_mode: string;
  market_id: string;
  requested_notional_usd: number;
  net_edge_pct: number;
  liquidity_participation_pct: number;
  data_age_ms: number;
  decision: 'APPROVED' | 'DENIED';
  failed_rules: string[];
  kill_switch_active: boolean;
  summary: string;
  decided_at: string;
}

export interface AgentMemoryEntry {
  memory_id: string;
  category: string;
  title: string;
  summary: string;
  evidence: Record<string, unknown>;
  status: string;
  created_at: string;
}

export interface HistoricalEdgePoint {
  scan_id: string;
  scan_time: string;
  markets_evaluated: number;
  approved_count: number;
  best_net_edge_pct: number | null;
  avg_net_edge_pct: number | null;
}

export interface DashboardPayload {
  phase: string;
  live_trading_enabled: boolean;
  portfolio: {
    provenance: DataProvenance;
    paper_capital_allocated_usd: number;
    deployed_exposure_usd: number;
    daily_simulated_pnl_usd: number;
    paper_execution_status: string;
    venue_exposures: VenueExposure[];
  };
  risk_gate: {
    fail_closed: boolean;
    policy: RiskPolicyConfig;
    kill_switch: KillSwitchState;
    total_audits: number;
  };
  modules: ModuleRegistryItem[];
  latest_opportunities: ArbitrageOpportunity[];
  historical_opportunities_count: number;
  historical_snapshots_count: number;
  historical_edge_series: HistoricalEdgePoint[];
  recent_audits: RiskAuditEntry[];
  recent_memories: AgentMemoryEntry[];
  recent_all_opportunities: ArbitrageOpportunity[];
  generated_at: string;
}

export interface HealthPayload {
  service: string;
  phase: string;
  status: string;
  live_trading_enabled: boolean;
  duckdb: {
    connected: boolean;
    path: string;
    counts: Record<string, number>;
  };
  risk_gate: {
    fail_closed: boolean;
    kill_switch: KillSwitchState;
  };
  venues: {
    polymarket: {
      mode: string;
      gamma_reachable: boolean | null;
      clob_reachable: boolean | null;
      clob_server_time?: string | null;
      gamma_error?: string | null;
      clob_error?: string | null;
      probe_latency_ms?: number;
    };
    kalshi: {
      venue_id: string;
      name: string;
      phase: string;
      status: string;
      provenance: DataProvenance;
      live_execution: boolean;
      notes: string;
    };
  };
  checked_at: string;
}

export interface OrderBookLevel {
  price: number;
  size: number;
}

export interface MarketBookSnapshotResponse {
  provenance: DataProvenance;
  snapshot: {
    snapshot_id: string;
    scan_id: string;
    market_id: string;
    condition_id: string;
    question: string;
    slug: string;
    yes_token_id: string;
    no_token_id: string;
    yes_best_ask: number | null;
    no_best_ask: number | null;
    yes_best_bid: number | null;
    no_best_bid: number | null;
    yes_ask_depth_shares: number;
    no_ask_depth_shares: number;
    fee_rate: number;
    fees_enabled: boolean;
    neg_risk: boolean;
    clob_timestamp_ms: number;
    ingested_at: string;
    book_payload: {
      yes: {
        bids: OrderBookLevel[];
        asks: OrderBookLevel[];
        min_order_size: number;
        tick_size: number;
        timestamp_ms: number;
      };
      no: {
        bids: OrderBookLevel[];
        asks: OrderBookLevel[];
        min_order_size: number;
        tick_size: number;
        timestamp_ms: number;
      };
      evaluation: {
        target_shares: number;
        yes_vwap: number | null;
        no_vwap: number | null;
        combined_raw_cost: number | null;
        yes_taker_fee_usd: number;
        no_taker_fee_usd: number;
        taker_fees_usd: number;
        slippage_buffer_usd: number;
        total_executable_cost_per_pair: number | null;
        gross_edge_pct: number | null;
        net_edge_pct: number | null;
        fee_rate: number;
        fees_enabled: boolean;
        fee_type: string;
        clob_age_ms: number;
        fetch_age_ms: number;
      };
    };
  };
}

export interface ScanResponse {
  scan_id: string;
  status: 'COMPLETED' | 'ERROR';
  error?: string;
  started_at?: string;
  duration_ms: number;
  markets_discovered?: number;
  markets_scanned: number;
  approved_count?: number;
  opportunities: ArbitrageOpportunity[];
}

export interface RiskEvalRuleCheck {
  rule_id: string;
  passed: boolean;
  detail: string;
}

export interface RiskEvalResponse {
  audit_id: string;
  request_id: string;
  approved: boolean;
  decision: 'APPROVED' | 'DENIED';
  failed_rules: string[];
  checks: RiskEvalRuleCheck[];
  kill_switch_active: boolean;
  summary: string;
  decided_at: string;
}
