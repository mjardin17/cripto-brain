import type {
  AgentMemoryEntry,
  DashboardPayload,
  HealthPayload,
  KillSwitchState,
  MarketBookSnapshotResponse,
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
};
