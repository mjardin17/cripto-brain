import type {
  AdminUserRecord,
  AdversarialReviewRecord,
  AgentMemoryEntry,
  AgentTaskRecord,
  AuthStatusResponse,
  AuthenticatedUserRecord,
  ConcurrencyStressRunRecord,
  ConfigVersionRecord,
  CrossVenueOpportunityRecord,
  CrossVenuePairRecord,
  CrossVenuePairRegisterResponse,
  CrossVenueScanResponse,
  PairDiscoveryResponse,
  PairReviewResponse,
  CouncilDefinition,
  CouncilSession,
  EventReactionRecord,
  PatternAnalysis,
  PatternPrediction,
  FluctuationAnalysis,
  FluctuationForecastResponse,
  DashboardPayload,
  HealthPayload,
  HistoricalValidationReport,
  IntelligenceCommandCenterPayload,
  KalshiBookSnapshotResponse,
  KalshiMarketsListResponse,
  KillSwitchState,
  MarketBookSnapshotResponse,
  MarketDriverSnapshot,
  MarketIntelligenceOverview,
  ModelRoutingEvent,
  MovementForecastRecord,
  OperationalHealthReport,
  PaperBlotterResponse,
  PaperExecuteResponse,
  PaperExecutionScenario,
  PaperStressSuiteResponse,
  PasswordResetTokenResponse,
  PredictionRecord,
  ResearchValidationRecord,
  RiskAuditEntry,
  RiskEvalResponse,
  ScanResponse,
  SecurityAuditLogRecord,
  SessionRecord,
  SoakMode,
  SoakRunRecord,
  SystemEventRecord,
  UserRole,
} from '../types/certainty';
import { parseApiJsonResponse, type RiskDryRunFormState } from '../utils/formatters';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let unauthorizedHandler: ((detail: string) => void) | null = null;
let forbiddenHandler: ((detail: string) => void) | null = null;

export function registerAuthHandlers(handlers: {
  onUnauthorized?: (detail: string) => void;
  onForbidden?: (detail: string) => void;
}): void {
  unauthorizedHandler = handlers.onUnauthorized ?? null;
  forbiddenHandler = handlers.onForbidden ?? null;
}

function getCsrfTokenFromCookie(): string | null {
  if (typeof document === 'undefined' || !document.cookie) return null;
  const parts = document.cookie.split(';');
  for (const part of parts) {
    const trimmed = part.trim();
    if (trimmed.startsWith('certainty_csrf=')) {
      return decodeURIComponent(trimmed.slice('certainty_csrf='.length));
    }
  }
  return null;
}

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const method = (init?.method || 'GET').toUpperCase();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  };
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = getCsrfTokenFromCookie();
    if (csrf) {
      headers['X-CSRF-Token'] = csrf;
    }
  }

  const response = await fetch(url, {
    ...init,
    credentials: 'same-origin',
    headers,
  });

  const rawText = await response.text();
  const parsed = parseApiJsonResponse(
    rawText,
    response.headers.get('content-type') || '',
    response.ok,
    response.status
  );

  if (!parsed.ok) {
    const detail = parsed.errorDetail || `HTTP ${parsed.status}`;
    const isAuthAttempt =
      url.startsWith('/api/auth/login') ||
      url.startsWith('/api/auth/bootstrap-owner') ||
      url.startsWith('/api/auth/reset-password');
    if (parsed.status === 401 && !isAuthAttempt && unauthorizedHandler) {
      unauthorizedHandler(detail);
    } else if (parsed.status === 403 && !isAuthAttempt && forbiddenHandler) {
      forbiddenHandler(detail);
    }
    throw new ApiError(parsed.status, detail);
  }

  return parsed.data as T;
}

export const certaintyApi = {
  getHealth: (probeVenues = true) =>
    apiFetch<HealthPayload>(`/api/health?probe_venues=${probeVenues}`),

  getDashboard: () => apiFetch<DashboardPayload>('/api/dashboard'),

  runPolymarketScan: (params: {
    limit: number;
    target_shares: number;
    slippage_buffer_bps: number;
  }) =>
    apiFetch<ScanResponse>('/api/scan/polymarket', {
      method: 'POST',
      body: JSON.stringify(params),
    }),

  getMarketBook: (marketId: string) =>
    apiFetch<MarketBookSnapshotResponse>(`/api/markets/${encodeURIComponent(marketId)}/book`),

  evaluateRiskGate: (payload: RiskDryRunFormState) =>
    apiFetch<RiskEvalResponse>('/api/risk-gate/evaluate', {
      method: 'POST',
      body: JSON.stringify({
        ...payload,
        executable: true,
      }),
    }),

  toggleKillSwitch: (payload: { active: boolean; reason: string; actor?: string }) =>
    apiFetch<KillSwitchState>('/api/risk-gate/kill-switch', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getAuditLog: (limit = 100) =>
    apiFetch<{ count: number; items: RiskAuditEntry[] }>(`/api/risk-gate/audit?limit=${limit}`),

  createMemory: (payload: {
    category: string;
    title: string;
    summary: string;
    status?: string;
  }) =>
    apiFetch<AgentMemoryEntry>('/api/memory', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getKalshiMarkets: (limit = 15) =>
    apiFetch<KalshiMarketsListResponse>(`/api/kalshi/markets?limit=${limit}`),

  getKalshiBook: (ticker: string, depth = 15) =>
    apiFetch<KalshiBookSnapshotResponse>(
      `/api/kalshi/markets/${encodeURIComponent(ticker)}/book?depth=${depth}`
    ),

  getCrossVenuePairs: (limit = 100) =>
    apiFetch<{ count: number; items: CrossVenuePairRecord[] }>(
      `/api/cross-venue/pairs?limit=${limit}`
    ),

  registerCrossVenuePair: (payload: {
    pair_id?: string;
    poly_market_id: string;
    kalshi_ticker: string;
    resolution_source_verified: boolean;
    predicate_verified: boolean;
    polarity_inverted: boolean;
    void_rules_compatible: boolean;
    allow_early_close_variance: boolean;
    notes?: string;
  }) =>
    apiFetch<CrossVenuePairRegisterResponse>('/api/cross-venue/pairs', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  discoverCrossVenuePairs: (payload: { persist: boolean; min_similarity?: number }) =>
    apiFetch<PairDiscoveryResponse>('/api/cross-venue/discover', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getCrossVenuePairReview: (pairId: string) =>
    apiFetch<PairReviewResponse>(
      `/api/cross-venue/pairs/${encodeURIComponent(pairId)}/review`
    ),

  rejectCrossVenuePair: (pairId: string, reason: string) =>
    apiFetch<{ pair: CrossVenuePairRecord }>(
      `/api/cross-venue/pairs/${encodeURIComponent(pairId)}/reject`,
      { method: 'POST', body: JSON.stringify({ reason }) }
    ),

  getCouncils: () =>
    apiFetch<{ councils: CouncilDefinition[] }>('/api/councils'),

  getCouncilSessions: (domain: 'STOCKS' | 'CRYPTO', limit = 10) =>
    apiFetch<{ count: number; items: CouncilSession[] }>(
      `/api/councils/${domain}/sessions?limit=${limit}`
    ),

  conveneCouncil: (domain: 'STOCKS' | 'CRYPTO', topic?: string) =>
    apiFetch<CouncilSession>(`/api/councils/${domain}/convene`, {
      method: 'POST',
      body: JSON.stringify({ topic: topic ?? null }),
    }),

  getEventReactions: (domain?: 'STOCKS' | 'CRYPTO') =>
    apiFetch<{ count: number; items: EventReactionRecord[] }>(
      `/api/patterns/events${domain ? `?domain=${domain}` : ''}`
    ),

  recordEventReaction: (payload: {
    domain: string;
    event_type: string;
    event_label: string;
    occurred_at: string;
    reaction_direction: string;
    narrative: string;
    asset_label?: string | null;
    market_label?: string | null;
    reaction_magnitude_pct?: number | null;
    sources?: string[];
    keywords?: string[];
  }) =>
    apiFetch<{ reaction: EventReactionRecord }>('/api/patterns/events', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  analyzeEventPattern: (domain: 'STOCKS' | 'CRYPTO', eventType: string) =>
    apiFetch<PatternAnalysis>(
      `/api/patterns/analyze?domain=${domain}&event_type=${encodeURIComponent(eventType)}`
    ),

  predictEventPattern: (domain: 'STOCKS' | 'CRYPTO', eventType: string, description?: string) =>
    apiFetch<PatternPrediction>('/api/patterns/predict', {
      method: 'POST',
      body: JSON.stringify({ domain, event_type: eventType, description: description ?? null }),
    }),

  analyzeFluctuations: (domain: 'STOCKS' | 'CRYPTO') =>
    apiFetch<FluctuationAnalysis>(`/api/fluctuations/analyze?domain=${domain}`),

  forecastFluctuations: (domain: 'STOCKS' | 'CRYPTO', horizonDays = 30) =>
    apiFetch<FluctuationForecastResponse>(
      `/api/fluctuations/forecast?domain=${domain}&horizon_days=${horizonDays}`
    ),

  importCuratedHistory: () =>
    apiFetch<{ imported: number; skipped_duplicates: number; total_curated: number; purged_test_analogs: number }>(
      '/api/patterns/import-curated',
      { method: 'POST', body: JSON.stringify({}) }
    ),

  getCrossVenueOpportunities: (limit = 100) =>
    apiFetch<{ count: number; items: CrossVenueOpportunityRecord[] }>(
      `/api/cross-venue/opportunities?limit=${limit}`
    ),

  scanCrossVenue: (payload: {
    pair_id?: string;
    target_shares: number;
    slippage_buffer_bps: number;
  }) =>
    apiFetch<CrossVenueScanResponse>('/api/cross-venue/scan', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  executePaperTrade: (payload: {
    pair_id: string;
    direction?: 'POLY_YES_KALSHI_NO' | 'KALSHI_YES_POLY_NO';
    target_shares: number;
    slippage_buffer_bps?: number;
    max_leg_slippage_bps?: number;
    scenario?: PaperExecutionScenario;
    ai_suggested_override?: boolean;
  }) =>
    apiFetch<PaperExecuteResponse>('/api/paper/execute', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  getPaperBlotter: (limit = 100) =>
    apiFetch<PaperBlotterResponse>(`/api/paper/blotter?limit=${limit}`),

  // Stage 3D: Intelligence & Agent Command Center
  getIntelligenceCommandCenter: () =>
    apiFetch<IntelligenceCommandCenterPayload>('/api/intelligence/command-center'),

  startAgentLoop: (payload?: { interval_seconds?: number; max_tasks_per_tick?: number }) =>
    apiFetch<{ started: boolean; duplicate_prevented: boolean }>('/api/agents/loop/start', {
      method: 'POST',
      body: JSON.stringify(payload ?? { interval_seconds: 5.0, max_tasks_per_tick: 4 }),
    }),

  stopAgentLoop: () =>
    apiFetch<{ stopped: boolean; shutdown_recovered_count: number }>('/api/agents/loop/stop', {
      method: 'POST',
      body: JSON.stringify({}),
    }),

  dispatchAgentTasks: (maxTasks = 4) =>
    apiFetch<{ halted: boolean; dispatched_count: number; completed_count: number }>(
      '/api/agents/dispatch',
      {
        method: 'POST',
        body: JSON.stringify({ max_tasks: maxTasks }),
      }
    ),

  scaleAgentWorkers: (payload: { max_concurrency?: number; target_concurrency?: number }) =>
    apiFetch<Record<string, unknown>>('/api/agents/scale', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  submitAgentTask: (payload: {
    task_type: string;
    assigned_agent: string;
    priority?: number;
    dedup_key?: string;
    payload?: Record<string, unknown>;
  }) =>
    apiFetch<{ created: boolean; duplicate_prevented: boolean; task: AgentTaskRecord }>(
      '/api/agents/tasks',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),

  createPrediction: (payload: {
    agent_id: string;
    market_id: string;
    venue: string;
    subject_key: string;
    question: string;
    predicted_prob: number;
    market_implied_prob?: number | null;
    confidence?: number;
    rationale?: string;
    provenance?: string;
  }) =>
    apiFetch<PredictionRecord>('/api/intelligence/predictions', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  resolvePrediction: (
    predictionId: string,
    payload: { outcome: 0 | 1 | 'VOID'; resolution_source?: string }
  ) =>
    apiFetch<PredictionRecord>(
      `/api/intelligence/predictions/${encodeURIComponent(predictionId)}/resolve`,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),

  routeModelRequest: (payload: {
    agent_id: string;
    task_category: string;
    prefer_ai: boolean;
    estimated_input_tokens?: number;
    estimated_output_tokens?: number;
  }) =>
    apiFetch<ModelRoutingEvent>('/api/intelligence/model-routing/route', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  rollbackConfig: (payload: {
    config_domain: string;
    target_version: number;
    actor?: string;
    reason?: string;
  }) =>
    apiFetch<ConfigVersionRecord>('/api/intelligence/configs/rollback', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // Stage 3E: Autonomous Operations & Validation Labs
  getOperationsHealth: () =>
    apiFetch<OperationalHealthReport>('/api/operations/health'),

  getSystemEvents: (limit = 50) =>
    apiFetch<{ count: number; items: SystemEventRecord[] }>(`/api/operations/events?limit=${limit}`),

  startSoakRun: (payload: {
    mode: SoakMode;
    duration_seconds?: number;
    tick_interval_seconds?: number;
  }) =>
    apiFetch<SoakRunRecord>('/api/operations/soak/start', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  stopSoakRun: (payload?: { run_id?: string; reason?: string }) =>
    apiFetch<SoakRunRecord>('/api/operations/soak/stop', {
      method: 'POST',
      body: JSON.stringify(payload ?? {}),
    }),

  runConcurrencyStress: (payload?: { task_count?: number; worker_count?: number }) =>
    apiFetch<ConcurrencyStressRunRecord>('/api/validation/concurrency-stress', {
      method: 'POST',
      body: JSON.stringify(payload ?? { task_count: 12, worker_count: 4 }),
    }),

  getHistoricalValidation: (minResolvedSamples = 10) =>
    apiFetch<HistoricalValidationReport>(
      `/api/validation/predictions/historical?min_resolved_samples=${minResolvedSamples}`
    ),

  validateResearchSubmission: (payload: {
    agent_id: string;
    source_identifier: string;
    source_timestamp?: string | null;
    retrieval_timestamp?: string | null;
    evidence_summary: string;
    market_id?: string;
    market_question?: string;
    raw_external_text?: string;
    confidence?: number;
    conflicting_claims?: Array<Record<string, unknown>>;
  }) =>
    apiFetch<ResearchValidationRecord>('/api/validation/research', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  runPaperStress: (payload?: { scenarios?: string[]; target_shares?: number }) =>
    apiFetch<PaperStressSuiteResponse>('/api/validation/paper-stress', {
      method: 'POST',
      body: JSON.stringify(payload ?? { target_shares: 25 }),
    }),

  runAdversarialReview: (payload: {
    subject_key: string;
    conclusion_agent: string;
    conclusion_summary: string;
    proposer_evidence: Array<Record<string, unknown>>;
    metrics?: Record<string, unknown>;
    opposing_evidence?: Array<Record<string, unknown>>;
  }) =>
    apiFetch<AdversarialReviewRecord>('/api/validation/adversarial-review', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // Stage 4A: Authentication, Sessions, Users & Security Audit Log
  getAuthStatus: () => apiFetch<AuthStatusResponse>('/api/auth/status'),

  bootstrapOwner: (payload: {
    username: string;
    password: string;
    email?: string;
    bootstrap_token?: string;
  }) =>
    apiFetch<{ enrolled: boolean; user: AuthenticatedUserRecord; expires_at: string }>(
      '/api/auth/bootstrap-owner',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),

  login: (payload: { username: string; password: string }) =>
    apiFetch<{ authenticated: boolean; user: AuthenticatedUserRecord; expires_at: string }>(
      '/api/auth/login',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),

  logout: () =>
    apiFetch<{ logged_out: boolean }>('/api/auth/logout', {
      method: 'POST',
      body: JSON.stringify({}),
    }),

  getMe: () => apiFetch<{ user: AuthenticatedUserRecord }>('/api/auth/me'),

  changePassword: (payload: { current_password: string; new_password: string }) =>
    apiFetch<{ password_changed: boolean; user: AuthenticatedUserRecord; expires_at: string }>(
      '/api/auth/change-password',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),

  confirmPasswordReset: (payload: { reset_token: string; new_password: string }) =>
    apiFetch<{ reset_completed: boolean; username: string; sessions_revoked: number }>(
      '/api/auth/reset-password/confirm',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),

  getOwnSessions: () =>
    apiFetch<{ count: number; items: SessionRecord[] }>('/api/auth/sessions'),

  revokeOwnSession: (sessionId: string, reason = 'Revoked by user') =>
    apiFetch<{ session_id: string; revoked: boolean }>(
      `/api/auth/sessions/${encodeURIComponent(sessionId)}/revoke`,
      {
        method: 'POST',
        body: JSON.stringify({ reason }),
      }
    ),

  adminListUsers: () =>
    apiFetch<{ count: number; items: AdminUserRecord[] }>('/api/admin/users'),

  adminCreateUser: (payload: {
    username: string;
    password: string;
    role: UserRole;
    email?: string;
    must_rotate_password?: boolean;
  }) =>
    apiFetch<{ user: AdminUserRecord }>('/api/admin/users', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  adminUpdateUserRole: (userId: string, role: UserRole) =>
    apiFetch<{ user: AdminUserRecord }>(
      `/api/admin/users/${encodeURIComponent(userId)}/role`,
      {
        method: 'PATCH',
        body: JSON.stringify({ role }),
      }
    ),

  adminDeactivateUser: (userId: string) =>
    apiFetch<{ user: AdminUserRecord }>(
      `/api/admin/users/${encodeURIComponent(userId)}/deactivate`,
      {
        method: 'POST',
        body: JSON.stringify({}),
      }
    ),

  adminReactivateUser: (userId: string) =>
    apiFetch<{ user: AdminUserRecord }>(
      `/api/admin/users/${encodeURIComponent(userId)}/reactivate`,
      {
        method: 'POST',
        body: JSON.stringify({}),
      }
    ),

  adminIssueResetToken: (userId: string, ttlMinutes = 30) =>
    apiFetch<PasswordResetTokenResponse>(
      `/api/admin/users/${encodeURIComponent(userId)}/reset-token`,
      {
        method: 'POST',
        body: JSON.stringify({ ttl_minutes: ttlMinutes }),
      }
    ),

  adminListSessions: (includeRevoked = false) =>
    apiFetch<{ count: number; items: SessionRecord[] }>(
      `/api/admin/sessions?include_revoked=${includeRevoked}`
    ),

  adminRevokeSession: (sessionId: string, reason = 'Revoked by OWNER') =>
    apiFetch<{ session_id: string; revoked: boolean }>(
      `/api/admin/sessions/${encodeURIComponent(sessionId)}/revoke`,
      {
        method: 'POST',
        body: JSON.stringify({ reason }),
      }
    ),

  adminListAuditLogs: (limit = 120) =>
    apiFetch<{ count: number; immutable: boolean; items: SecurityAuditLogRecord[] }>(
      `/api/admin/audit-logs?limit=${limit}`
    ),

  // Multi-Factor Market Drivers & Stocks/Crypto AI Councils
  getMarketIntelligenceOverview: (horizon: '4H' | '12H' | '24H' | '48H' = '24H') =>
    apiFetch<MarketIntelligenceOverview>(`/api/market-intelligence/overview?horizon=${horizon}`),

  ingestMarketDrivers: () =>
    apiFetch<MarketDriverSnapshot>('/api/market-intelligence/ingest', {
      method: 'POST',
      body: JSON.stringify({}),
    }),

  conveneAiCouncils: (payload?: {
    councils?: Array<'CRYPTO' | 'STOCKS'>;
    horizon?: '4H' | '12H' | '24H' | '48H';
    refresh_drivers?: boolean;
    create_forecasts?: boolean;
  }) =>
    apiFetch<{
      snapshot_id: string;
      provenance: string;
      verified_clean: boolean;
      live_trading_enabled: boolean;
      deliberations: Record<string, unknown>;
      created_forecasts: MovementForecastRecord[];
      snapshot: MarketDriverSnapshot;
    }>('/api/market-intelligence/councils/convene', {
      method: 'POST',
      body: JSON.stringify(
        payload ?? { horizon: '24H', refresh_drivers: true, create_forecasts: true }
      ),
    }),

  resolveMovementForecast: (
    forecastId: string,
    payload: { actual_price?: number; void_reason?: string }
  ) =>
    apiFetch<MovementForecastRecord>(
      `/api/market-intelligence/forecasts/${encodeURIComponent(forecastId)}/resolve`,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      }
    ),

  resolveDueMovementForecasts: (forceAllLive = false) =>
    apiFetch<{ resolved_count: number; items: MovementForecastRecord[] }>(
      '/api/market-intelligence/forecasts/resolve-due',
      {
        method: 'POST',
        body: JSON.stringify({ force_all_live: forceAllLive }),
      }
    ),
};

