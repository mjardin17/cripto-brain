import type {
  AgentMemoryEntry,
  CrossVenueOpportunityRecord,
  CrossVenuePairRecord,
  CrossVenuePairRegisterResponse,
  CrossVenueScanResponse,
  DashboardPayload,
  HealthPayload,
  KalshiBookSnapshotResponse,
  KalshiMarketsListResponse,
  KillSwitchState,
  MarketBookSnapshotResponse,
  PaperBlotterResponse,
  PaperExecuteResponse,
  PaperExecutionScenario,
  RiskAuditEntry,
  RiskEvalResponse,
  ScanResponse,
} from '../types/certainty';
import type { RiskDryRunFormState } from '../utils/formatters';

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const errBody = await response.json();
      detail = errBody.detail || errBody.error || detail;
    } catch {
      // Ignore parse error
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
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
};
