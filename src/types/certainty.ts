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
    daily_simulated_loss_usd?: number;
    total_realized_pnl_usd?: number;
    total_locked_arb_edge_usd?: number;
    paper_executions_count?: number;
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
  paper_summary?: PaperSummary;
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
      mode?: string;
      status: string;
      provenance: DataProvenance;
      reachable?: boolean | null;
      exchange_active?: boolean | null;
      trading_active?: boolean | null;
      error?: string | null;
      probe_latency_ms?: number;
      live_execution: boolean;
      notes?: string;
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

export interface KalshiMarketSummary {
  ticker: string;
  event_ticker: string;
  series_ticker: string;
  title: string;
  subtitle: string;
  yes_sub_title: string;
  no_sub_title: string;
  open_time?: string;
  close_time: string;
  expiration_time: string;
  updated_time?: string;
  can_close_early?: boolean;
  market_type?: 'binary';
  status?: 'active' | 'open' | 'closed' | 'settled';
  yes_bid_dollars: number | null;
  yes_ask_dollars: number | null;
  no_bid_dollars: number | null;
  no_ask_dollars: number | null;
  volume_24h: number;
  open_interest: number;
  rules_primary: string;
  rules_secondary?: string;
}

export interface KalshiMarketsListResponse {
  provenance: DataProvenance;
  mode: string;
  count: number;
  markets: KalshiMarketSummary[];
}

export type KalshiRawOrderLevel = [string | number, string | number];

export interface KalshiOrderBookRawPayload {
  orderbook_fp?: {
    yes_dollars?: KalshiRawOrderLevel[];
    no_dollars?: KalshiRawOrderLevel[];
  };
  orderbook?: {
    yes?: KalshiRawOrderLevel[];
    no?: KalshiRawOrderLevel[];
    yes_dollars?: KalshiRawOrderLevel[];
    no_dollars?: KalshiRawOrderLevel[];
  };
}

export interface KalshiOutcomeOrderBook {
  best_bid: number | null;
  best_ask: number | null;
  spread?: number | null;
  bids: OrderBookLevel[];
  asks: OrderBookLevel[];
  total_ask_shares: number;
  total_bid_shares?: number;
}

export interface KalshiBookSnapshotResponse {
  provenance: DataProvenance;
  mode: string;
  ticker: string;
  timestamp_ms: number;
  fetched_at_ms: number;
  yes: KalshiOutcomeOrderBook;
  no: KalshiOutcomeOrderBook;
}

export type SettlementMatchStatus = 'VERIFIED' | 'UNVERIFIED_FAIL_CLOSED' | 'MISMATCH_BLOCKED';

export interface KalshiSettlementSpec {
  ticker: string;
  yes_sub_title: string;
  no_sub_title: string;
  rules_primary: string;
  rules_secondary?: string;
  close_time: string;
  expiration_time: string;
  can_close_early: boolean;
  resolution_source_verified: boolean;
  predicate_verified: boolean;
  void_rules_compatible: boolean;
  polarity_inverted: boolean;
  identical_settlement_verified: boolean;
  settlement_status: SettlementMatchStatus;
  settlement_value?: 'yes' | 'no' | 'void' | null;
  settled_at?: string | null;
}

export interface SettlementCheckItem {
  check_id: string;
  passed: boolean;
  detail: string;
}

export interface CrossVenueSettlementAssessment {
  identical_settlement_verified: boolean;
  status: SettlementMatchStatus;
  cutoff_delta_hours: number | null;
  mismatch_reasons: string[];
  checks: SettlementCheckItem[];
}

export interface CrossVenuePairRecord {
  pair_id: string;
  poly_market_id: string;
  poly_question: string;
  kalshi_ticker: string;
  kalshi_title: string;
  resolution_source_verified: boolean;
  predicate_verified: boolean;
  polarity_inverted: boolean;
  void_rules_compatible: boolean;
  allow_early_close_variance: boolean;
  identical_settlement_verified: boolean;
  settlement_status: SettlementMatchStatus;
  mismatch_reasons: string[];
  cutoff_delta_hours: number | null;
  notes: string;
  created_at: string;
}

export interface CrossVenuePairRegisterResponse {
  pair: CrossVenuePairRecord;
  title_similarity: number;
  assessment: CrossVenueSettlementAssessment;
}

export interface CrossVenueOpportunityRecord {
  xopp_id: string;
  scan_id: string;
  pair_id: string;
  poly_market_id: string;
  kalshi_ticker: string;
  question: string;
  direction: 'POLY_YES_KALSHI_NO' | 'KALSHI_YES_POLY_NO';
  leg1_venue: string;
  leg1_outcome: 'YES' | 'NO';
  leg2_venue: string;
  leg2_outcome: 'YES' | 'NO';
  target_shares: number;
  leg1_vwap: number | null;
  leg2_vwap: number | null;
  combined_raw_cost: number | null;
  total_fees_usd: number;
  slippage_buffer_usd: number;
  total_executable_cost: number | null;
  gross_edge_pct: number | null;
  net_edge_pct: number | null;
  liquidity_participation_pct: number;
  leg_imbalance_ratio: number;
  executable: boolean;
  identical_settlement_verified: boolean;
  settlement_status: string;
  risk_gate_approved: boolean;
  risk_gate_reason: string;
  risk_audit_id: string;
  data_age_ms: number;
  route_a_net_edge_pct: number | null;
  route_b_net_edge_pct: number | null;
  provenance: DataProvenance;
  evaluated_at: string;
}

export interface CrossVenueScanResponse {
  provenance: DataProvenance;
  count: number;
  results: CrossVenueOpportunityRecord[];
  errors: Array<{ pair_id: string; error: string }>;
  message?: string;
}

export type PaperExecutionScenario =
  | 'LIVE_BOOKS'
  | 'PARTIAL_FILL_LEG1'
  | 'SECOND_LEG_PRICE_DRIFT_UNWIND'
  | 'LEG2_REJECTED_UNWIND';

export interface PaperOrderRecord {
  order_id: string;
  execution_id: string;
  pair_id: string;
  audit_id: string;
  leg_number: number;
  venue: string;
  market_ref: string;
  side: string;
  outcome: string;
  order_type: string;
  requested_shares: number;
  filled_shares: number;
  limit_price: number;
  avg_fill_price: number | null;
  fees_usd: number;
  latency_ms: number;
  status: string;
  reason: string;
  provenance: DataProvenance;
  created_at: string;
}

export interface PaperFillRecord {
  fill_id: string;
  execution_id: string;
  order_id: string;
  venue: string;
  market_ref: string;
  side: string;
  outcome: string;
  shares: number;
  fill_price: number;
  notional_usd: number;
  fee_usd: number;
  is_unwind: boolean;
  provenance: DataProvenance;
  filled_at: string;
}

export interface PaperPositionRecord {
  position_id: string;
  execution_id: string;
  pair_id: string;
  venue: string;
  market_ref: string;
  outcome: string;
  shares: number;
  entry_price: number;
  notional_usd: number;
  locked_payout_usd: number;
  status: string;
  provenance: DataProvenance;
  updated_at: string;
}

export interface PaperPnlLedgerRecord {
  ledger_id: string;
  execution_id: string;
  pair_id: string;
  event_type: string;
  outcome_status: string;
  matched_shares: number;
  unwound_shares: number;
  gross_cost_usd: number;
  fees_usd: number;
  realized_pnl_usd: number;
  locked_arb_edge_usd: number;
  notes: string;
  provenance: DataProvenance;
  recorded_at: string;
}

export interface PaperSummary {
  provenance: DataProvenance;
  venue_exposures: Record<string, number>;
  deployed_exposure_usd: number;
  daily_simulated_pnl_usd: number;
  daily_simulated_loss_usd: number;
  executions: number;
  total_realized_pnl_usd: number;
  total_locked_arb_edge_usd: number;
  total_fees_usd: number;
  total_matched_shares: number;
  total_unwound_shares: number;
}

export interface PaperBlotterResponse {
  provenance: DataProvenance;
  live_trading_enabled: boolean;
  summary: PaperSummary;
  orders: PaperOrderRecord[];
  fills: PaperFillRecord[];
  positions: PaperPositionRecord[];
  pnl_ledger: PaperPnlLedgerRecord[];
}

export interface PaperExecuteResponse {
  execution_id: string;
  pair_id: string;
  direction: 'POLY_YES_KALSHI_NO' | 'KALSHI_YES_POLY_NO';
  status: string;
  approved: boolean;
  provenance: DataProvenance;
  live_trading_enabled: boolean;
  scenario: PaperExecutionScenario;
  gate_result: RiskEvalResponse;
  orders: PaperOrderRecord[];
  fills: PaperFillRecord[];
  positions: PaperPositionRecord[];
  pnl_entry: PaperPnlLedgerRecord | null;
  summary: PaperSummary;
}
