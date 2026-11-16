export type DataProvenance =
  | 'LIVE'
  | 'SIMULATED'
  | 'HISTORICAL'
  | 'SHORT_VALIDATION'
  | 'SOAK_TEST'
  | 'UNAVAILABLE'
  | 'LIVE_SNAPSHOT';

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
  review_state?: PairReviewState;
}

export type PairReviewState = 'PENDING_REVIEW' | 'CONFIRMED' | 'REJECTED';

export interface PairReviewPolymarketSide {
  market_id: string;
  question: string;
  rules_text: string;
  resolution_source: string;
  end_date: string;
  neg_risk: boolean;
  neg_risk_other: boolean;
  url: string | null;
}

export interface PairReviewKalshiSide {
  ticker: string;
  title: string;
  event_title: string;
  yes_sub_title: string;
  rules_primary: string;
  rules_secondary: string;
  early_close_condition: string;
  can_close_early: boolean;
  close_time: string;
  expiration_time: string;
  expected_expiration_time: string;
  settlement_timer_seconds: number | null;
  settlement_sources: { name: string; url: string }[];
  contract_url: string | null;
  url: string | null;
}

export interface PairReviewResponse {
  provenance: 'LIVE';
  pair: CrossVenuePairRecord;
  review_state: PairReviewState;
  polymarket: PairReviewPolymarketSide | null;
  kalshi: PairReviewKalshiSide | null;
  assessment: CrossVenueSettlementAssessment | null;
  errors: string[];
}

export interface PairDiscoveryCandidate {
  poly_market_id: string;
  poly_question: string;
  kalshi_ticker: string;
  kalshi_title: string;
  similarity: number;
  cutoff_delta_hours: number | null;
  settlement_status: string;
  registered: boolean;
  pair_id: string | null;
  skip_reason: string | null;
}

export interface PairDiscoveryResponse {
  provenance: 'LIVE';
  mode: 'REGISTER' | 'DRY_RUN';
  polymarket_markets_scanned: number;
  kalshi_markets_scanned: number;
  candidates_found: number;
  registered_count: number;
  verified_count: number;
  warnings: string[];
  note: string;
  candidates: PairDiscoveryCandidate[];
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

// ----------------------------------------------------------------------
// Stage 3D: Agent Orchestration, Probability Intelligence & Routing
// ----------------------------------------------------------------------

export interface BackgroundLoopState {
  running: boolean;
  interval_seconds: number;
  started_at: string | null;
  stopped_at: string | null;
  ticks: number;
  last_tick_at: string | null;
  last_error: string | null;
  startup_recovered_count: number;
  shutdown_recovered_count: number;
  duplicate_starts_blocked: number;
  halted_by_kill_switch: boolean;
}

export interface AgentProfileInfo {
  role: string;
  agent_id: string;
  description: string;
  permitted_actions: string[];
  forbidden_actions: string[];
  supports_task_types: string[];
}

export interface WorkerRecord {
  worker_id: string;
  agent_role: string;
  status: 'IDLE' | 'BUSY' | 'UNHEALTHY' | 'STOPPED';
  current_task_id: string | null;
  permitted_actions: string[];
  last_heartbeat_at: string;
  tasks_completed: number;
  tasks_failed: number;
  spawned_at: string;
  updated_at: string;
}

export interface AgentTaskRecord {
  task_id: string;
  dedup_key: string;
  task_type: string;
  assigned_agent: string;
  status:
    | 'PENDING'
    | 'QUEUED'
    | 'LEASED'
    | 'RUNNING'
    | 'BLOCKED'
    | 'COMPLETED'
    | 'FAILED'
    | 'ESCALATED'
    | 'CANCELLED';
  priority: number;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  depends_on: string[];
  worker_id: string | null;
  lease_token: string | null;
  lease_expires_at: string | null;
  last_heartbeat_at: string | null;
  timeout_seconds: number;
  started_at: string | null;
  attempt_count: number;
  max_retries: number;
  recovery_attempts: number;
  max_recovery_attempts: number;
  last_blocker_id: string | null;
  escalation_reason: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface BlockerEventRecord {
  blocker_id: string;
  task_id: string;
  agent_id: string;
  worker_id: string | null;
  blocker_class: string;
  error_message: string;
  memory_hits: Array<Record<string, unknown>>;
  candidate_strategies: Array<{
    strategy: string;
    allowed: boolean;
    rejection_reason?: string | null;
  }>;
  selected_strategy: string | null;
  recovery_action: string | null;
  verification_detail: string | null;
  outcome: string;
  learning_cycle_id: string | null;
  regression_case_id: string | null;
  created_at: string;
}

export interface AgentActivityRecord {
  activity_id: string;
  actor: string;
  agent_role: string;
  task_id: string | null;
  worker_id: string | null;
  event_type: string;
  summary: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface SupervisorStatusPayload {
  phase: string;
  live_trading_enabled: boolean;
  kill_switch_active: boolean;
  dispatch_status: string;
  background_loop: BackgroundLoopState;
  policy: {
    max_concurrency: number;
    hard_max_concurrency: number;
    max_recovery_attempts: number;
    max_retries: number;
    lease_seconds: number;
    heartbeat_grace_seconds: number;
  };
  agents: AgentProfileInfo[];
  active_workers: WorkerRecord[];
  active_workers_count: number;
  task_counts_by_status: Record<string, number>;
  recent_blockers: BlockerEventRecord[];
  recent_activity: AgentActivityRecord[];
}

export interface CalibrationBucket {
  bucket_index: number;
  range_label: string;
  low: number;
  high: number;
  count: number;
  avg_predicted_prob: number | null;
  empirical_yes_rate: number | null;
  calibration_gap: number | null;
}

export interface CalibrationSummary {
  resolved_count: number;
  open_count: number;
  voided_count: number;
  mean_brier_score: number | null;
  mean_log_loss: number | null;
  accuracy_pct: number | null;
  expected_calibration_error: number | null;
  buckets: CalibrationBucket[];
  note: string;
}

export interface AgentScorecard {
  agent_id: string;
  resolved_predictions: number;
  open_predictions: number;
  voided_predictions: number;
  mean_brier_score: number | null;
  mean_log_loss: number | null;
  accuracy_pct: number | null;
  expected_calibration_error: number | null;
  comparison_eligible: boolean;
  comparison_status: string;
  min_resolved_samples_required: number;
  rank: number | null;
  tasks_completed: number;
  tasks_escalated: number;
  tasks_total: number;
}

export interface ScorecardsPayload {
  min_resolved_samples_required: number;
  comparable_agents_count: number;
  scorecards: AgentScorecard[];
}

export interface PredictionRecord {
  prediction_id: string;
  agent_id: string;
  market_id: string;
  venue: string;
  subject_key: string;
  question: string;
  predicted_prob: number;
  market_implied_prob: number | null;
  confidence: number;
  rationale: string;
  evidence: Array<Record<string, unknown>>;
  provenance: DataProvenance;
  status: 'OPEN' | 'RESOLVED' | 'VOIDED';
  actual_outcome: number | null;
  brier_score: number | null;
  log_loss: number | null;
  learning_cycle_id: string | null;
  created_at: string;
  resolved_at: string | null;
  market_category?: string;
}

export interface ModelRoutingEvent {
  routing_id: string;
  task_id: string | null;
  agent_id: string;
  task_category: string;
  selected_tier: 'DETERMINISTIC' | 'ECONOMICAL_AI' | 'UNAVAILABLE' | 'BLOCKED_BUDGET';
  provider: string;
  model_name: string;
  api_key_configured: boolean;
  estimated_cost_usd: number;
  actual_cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  reason: string;
  status: string;
  created_at: string;
}

export interface ModelRoutingSummary {
  provider: string;
  provider_status: 'AVAILABLE' | 'UNAVAILABLE';
  provenance: DataProvenance;
  api_key_configured: boolean;
  api_key_exposed_to_frontend: boolean;
  policy: {
    per_task_limit_usd: number;
    daily_budget_usd: number;
    deterministic_first: boolean;
    economical_model: string;
  };
  daily_spent_usd: number;
  remaining_daily_budget_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
  tier_breakdown: Record<
    string,
    {
      count: number;
      input_tokens: number;
      output_tokens: number;
      cost_usd: number;
    }
  >;
  recent_events: ModelRoutingEvent[];
}

export interface ConfigVersionRecord {
  config_version_id: string;
  config_domain: string;
  version: number;
  config: Record<string, unknown>;
  status: 'ACTIVE' | 'CANDIDATE' | 'ROLLED_BACK';
  proposed_by: string;
  reviewed_by: string | null;
  source_cycle_id: string | null;
  regression_case_id: string | null;
  regression_passed: boolean;
  change_reason: string;
  created_at: string;
  activated_at: string | null;
}

export interface LearningCycleRecord {
  cycle_id: string;
  stage: string;
  source_type: string;
  source_ref_id: string;
  agent_id: string;
  subject_key: string;
  hypothesis: string;
  decision_summary: string;
  outcome_matched: boolean | null;
  regression_case_id: string | null;
  applied_flag: boolean;
  updated_at: string;
}

export interface RegressionCaseRecord {
  case_id: string;
  category: string;
  title: string;
  status: string;
  last_run_status: string | null;
  last_run_detail: string | null;
  created_at: string;
}

export interface OperationalHealthReport {
  provenance: DataProvenance;
  health_status: 'NOMINAL' | 'DEGRADED' | 'HALTED_BY_KILL_SWITCH';
  supervisor_running: boolean;
  supervisor_uptime_seconds: number;
  supervisor_ticks: number;
  critical_failures_count: number;
  last_supervisor_error: string | null;
  active_workers_count: number;
  unhealthy_workers_count: number;
  stalled_workers_count: number;
  stalled_workers: Array<Record<string, unknown>>;
  worker_uptime_avg_seconds: number;
  tasks_queued: number;
  tasks_active: number;
  tasks_completed: number;
  tasks_failed: number;
  tasks_escalated: number;
  tasks_blocked: number;
  abandoned_tasks_count: number;
  abandoned_tasks: Array<Record<string, unknown>>;
  long_running_tasks_count: number;
  long_running_tasks: Array<Record<string, unknown>>;
  expired_leases_active_count: number;
  expired_leases_historical_count: number;
  avg_task_duration_ms: number;
  recovery_attempts_total: number;
  recovery_successes: number;
  recovery_escalations: number;
  recovery_success_rate_pct: number;
  critical_events_count: number;
  warning_events_count: number;
  memory_rss_mb: number;
  api_spend_today_usd: number;
  checked_at: string;
}

export interface SystemEventRecord {
  event_id: string;
  severity: string;
  component: string;
  event_type: string;
  message: string;
  detail: Record<string, unknown>;
  acknowledged: boolean;
  created_at: string;
}

export type SoakMode = 'SHORT_VALIDATION' | 'SOAK_24H' | 'SOAK_48H' | 'SOAK_72H';

export interface SoakRunRecord {
  run_id: string;
  mode: SoakMode;
  run_label: 'SOAK_TEST' | 'SHORT_VALIDATION';
  status: 'RUNNING' | 'PASSED' | 'FAILED' | 'STOPPED_EARLY' | 'UNEXPECTED_SHUTDOWN';
  duration_target_seconds: number;
  tick_interval_seconds: number;
  started_at: string;
  ends_at: string;
  ended_at: string | null;
  elapsed_seconds: number;
  progress_pct: number;
  supervisor_uptime_seconds: number;
  tasks_submitted: number;
  tasks_completed: number;
  tasks_failed: number;
  worker_failures: number;
  recovery_events: number;
  db_errors: number;
  memory_rss_mb: number | null;
  api_spend_usd: number;
  unexpected_shutdowns: number;
  kill_switch_events: number;
  failure_reason: string | null;
  metrics: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ConcurrencyStressRunRecord {
  stress_id: string;
  status: 'PASSED' | 'FAILED';
  config: Record<string, unknown>;
  total_claims_attempted: number;
  successful_claims: number;
  rejected_duplicates: number;
  ownership_violations: number;
  recovery_successes: number;
  deadlocks_detected: number;
  starved_tasks: number;
  avg_queue_latency_ms: number;
  throughput_tasks_per_sec: number;
  kill_switch_halted: boolean;
  db_contention_retries: number;
  summary: Record<string, unknown>;
  started_at: string;
  ended_at: string;
}

export interface HistoricalValidationReport {
  provenance: DataProvenance;
  resolved_count: number;
  open_excluded_count: number;
  voided_excluded_count: number;
  min_resolved_samples_required: number;
  comparative_claim_allowed: boolean;
  comparative_verdict: string;
  agent_metrics: {
    mean_brier_score: number | null;
    brier_ci_95: [number, number] | null;
    mean_log_loss: number | null;
    log_loss_ci_95: [number, number] | null;
    expected_calibration_error: number | null;
    directional_accuracy_pct: number | null;
    accuracy_wilson_ci_95_pct: [number, number] | null;
  };
  baselines: {
    base_rate_prob: number | null;
    base_rate_brier_score: number | null;
    base_rate_log_loss: number | null;
    base_rate_accuracy_pct: number | null;
    market_implied_sample_size: number;
    market_implied_brier_score: number | null;
    market_implied_log_loss: number | null;
    market_implied_accuracy_pct: number | null;
    agent_brier_on_market_subset: number | null;
    paired_brier_diff_mean?: number | null;
    paired_brier_diff_ci_95: [number, number] | null;
    brier_skill_vs_market: number | null;
  };
  by_category: Array<{
    market_category: string;
    resolved_count: number;
    mean_brier_score: number;
    mean_log_loss: number;
    accuracy_pct: number;
  }>;
  by_agent: Array<{
    agent_id: string;
    resolved_count: number;
    mean_brier_score: number;
    mean_log_loss: number;
    accuracy_pct: number;
    comparative_eligible: boolean;
  }>;
  note: string;
}

export interface ResearchValidationRecord {
  validation_id: string;
  agent_id: string;
  source_identifier: string;
  source_timestamp: string | null;
  retrieval_timestamp: string;
  evidence_summary: string;
  verification_status: string;
  freshness_ok: boolean;
  conflict_detected: boolean;
  duplicate_market_detected: boolean;
  injection_detected: boolean;
  contract_equivalence_ok: boolean;
  confidence: number;
  payload: Record<string, unknown>;
  findings: string[];
  created_at: string;
}

export interface AdversarialReviewRecord {
  review_id: string;
  subject_key: string;
  conclusion_agent: string;
  conclusion_summary: string;
  proposer_evidence: Array<Record<string, unknown>>;
  challenger_agent: string;
  challenge_notes: string;
  opposing_evidence: Array<Record<string, unknown>>;
  arbiter_agent: string;
  verdict: 'VERIFIED' | 'DISPROVED' | 'UNVERIFIED';
  checks: Array<{
    check_id: string;
    failed: boolean;
    detail: string;
  }>;
  learning_cycle_id: string | null;
  created_at: string;
  resolved_at: string;
}

export interface PaperStressRunRecord {
  stress_id: string;
  scenario_name: string;
  pair_id: string;
  status: string;
  proven_simulated: boolean;
  risk_gate_approved: boolean;
  expected_edge_pct: number | null;
  realized_simulated_edge_pct: number | null;
  simulated_pnl_usd: number;
  fees_usd: number;
  slippage_usd: number;
  fill_rate_pct: number;
  latency_ms: number;
  unwind_loss_usd: number;
  rejection_reason: string | null;
  learning_cycle_id: string | null;
  metrics: Record<string, unknown>;
  created_at: string;
}

export interface PaperStressSuiteResponse {
  provenance: DataProvenance;
  live_trading_enabled: boolean;
  scenarios_executed: number;
  approved_count: number;
  rejected_count: number;
  risk_gate_rejection_rate_pct: number;
  total_simulated_pnl_usd: number;
  total_unwind_loss_usd: number;
  results: PaperStressRunRecord[];
}

export interface IntelligenceCommandCenterPayload {
  phase: string;
  live_trading_enabled: boolean;
  supervisor: SupervisorStatusPayload;
  tasks: AgentTaskRecord[];
  blockers: BlockerEventRecord[];
  calibration: CalibrationSummary;
  scorecards: ScorecardsPayload;
  predictions: PredictionRecord[];
  model_routing: ModelRoutingSummary;
  configs: ConfigVersionRecord[];
  memory_counts: Record<string, number>;
  learning_cycles: LearningCycleRecord[];
  regression_cases: RegressionCaseRecord[];
  operational_health: OperationalHealthReport;
  system_events: SystemEventRecord[];
  soak_runs: SoakRunRecord[];
  concurrency_stress_runs: ConcurrencyStressRunRecord[];
  historical_validation: HistoricalValidationReport;
  research_validations: ResearchValidationRecord[];
  adversarial_reviews: AdversarialReviewRecord[];
  paper_stress_runs: PaperStressRunRecord[];
  generated_at: string;
}

// ----------------------------------------------------------------------
// Stage 4A: Authentication, RBAC, Sessions & Security Audit Types
// ----------------------------------------------------------------------

export type UserRole = 'OWNER' | 'OPERATOR' | 'ANALYST' | 'READ_ONLY';

export interface UserPermissions {
  can_read_dashboards: boolean;
  can_submit_research: boolean;
  can_evaluate_risk_dry_run: boolean;
  can_propose_configs: boolean;
  can_run_scans: boolean;
  can_manage_pairs: boolean;
  can_execute_paper_trades: boolean;
  can_dispatch_agents: boolean;
  can_scale_agents: boolean;
  can_resolve_predictions: boolean;
  can_route_models: boolean;
  can_run_validation_labs: boolean;
  can_toggle_kill_switch: boolean;
  can_activate_configs: boolean;
  can_manage_users: boolean;
  can_manage_all_sessions: boolean;
  can_view_security_audit: boolean;
}

export interface AuthenticatedUserRecord {
  user_id: string;
  username: string;
  email: string | null;
  role: UserRole;
  session_id: string;
  is_active: boolean;
  must_rotate_password: boolean;
  permissions: UserPermissions;
}

export interface AuthStatusResponse {
  authenticated: boolean;
  needs_owner_bootstrap: boolean;
  env_mode: string;
  cookie_secure: boolean;
  tls_required: boolean;
  csrf_header_name: string;
  user: AuthenticatedUserRecord | null;
}

export interface AdminUserRecord {
  user_id: string;
  username: string;
  email: string | null;
  role: UserRole;
  is_active: boolean;
  must_rotate_password: boolean;
  failed_login_attempts: number;
  locked_until: string | null;
  last_login_at: string | null;
  created_at: string;
  updated_at: string;
  created_by: string;
}

export interface SessionRecord {
  session_id: string;
  user_id: string;
  username: string;
  role_snapshot: UserRole;
  ip_address: string;
  user_agent: string;
  created_at: string;
  expires_at: string;
  last_seen_at: string;
  revoked_at: string | null;
  revoked_reason: string | null;
}

export interface SecurityAuditLogRecord {
  audit_id: string;
  event_type: string;
  outcome: string;
  user_id: string | null;
  username: string | null;
  role: string | null;
  ip_address: string;
  endpoint: string;
  method: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface PasswordResetTokenResponse {
  reset_id: string;
  user_id: string;
  username: string;
  reset_token: string;
  expires_at: string;
}

