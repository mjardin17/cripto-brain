import type { ArbitrageOpportunity, DataProvenance } from '../types/certainty';

export function formatUsd(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  const absVal = Math.abs(value).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return value < 0 ? `-$${absVal}` : `$${absVal}`;
}

export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  return `$${value.toFixed(4)}`;
}

export function formatPct(value: number | null | undefined, decimals = 2, signed = false): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  const sign = signed && value > 0 ? '+' : '';
  return `${sign}${value.toFixed(decimals)}%`;
}

export function formatShares(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  return value.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

export function formatTimestampShort(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

export type OpportunityFilter = 'ALL' | 'APPROVED' | 'DENIED' | 'POSITIVE_GROSS';

export function filterOpportunities(
  items: ArbitrageOpportunity[],
  query: string,
  filter: OpportunityFilter
): ArbitrageOpportunity[] {
  const q = query.trim().toLowerCase();
  return items.filter((item) => {
    if (filter === 'APPROVED' && !item.risk_gate_approved) return false;
    if (filter === 'DENIED' && item.risk_gate_approved) return false;
    if (filter === 'POSITIVE_GROSS' && (item.gross_edge_pct === null || item.gross_edge_pct <= 0)) {
      return false;
    }
    if (!q) return true;
    return (
      item.question.toLowerCase().includes(q) ||
      item.market_id.toLowerCase().includes(q) ||
      item.risk_gate_reason.toLowerCase().includes(q)
    );
  });
}

export function describeProvenance(provenance: DataProvenance): {
  label: string;
  toneClass: string;
} {
  switch (provenance) {
    case 'LIVE':
    case 'LIVE_SNAPSHOT':
      return {
        label: provenance === 'LIVE_SNAPSHOT' ? 'LIVE SNAPSHOT' : 'LIVE',
        toneClass: 'text-emerald-400',
      };
    case 'SIMULATED':
      return {
        label: 'SIMULATED',
        toneClass: 'text-amber-400',
      };
    case 'HISTORICAL':
      return {
        label: 'HISTORICAL',
        toneClass: 'text-violet-300',
      };
    case 'SHORT_VALIDATION':
      return {
        label: 'SHORT VALIDATION',
        toneClass: 'text-cyan-300',
      };
    case 'SOAK_TEST':
      return {
        label: 'SOAK TEST',
        toneClass: 'text-emerald-300',
      };
    case 'UNAVAILABLE':
    default:
      return {
        label: 'UNAVAILABLE',
        toneClass: 'text-slate-400',
      };
  }
}

export interface RiskDryRunFormState {
  venue: string;
  arbitrage_type: 'intra_venue' | 'cross_venue';
  strategy_tier: 'tier1' | 'tier2' | 'tier3';
  execution_mode: 'PAPER_RESEARCH' | 'PAPER_EXECUTION' | 'LIVE';
  market_id: string;
  requested_notional_usd: number;
  current_venue_exposure_usd: number;
  daily_tier_loss_usd: number;
  net_edge_pct: number;
  liquidity_participation_pct: number;
  data_age_ms: number;
  leg_imbalance_ratio: number;
  identical_settlement_verified: boolean;
  partial_fill_protection_enabled: boolean;
  ai_suggested_override: boolean;
}

export function getRiskDryRunPreset(
  preset: 'VALID_TRADE' | 'LOW_EDGE' | 'STALE_DATA' | 'AI_OVERRIDE' | 'LIVE_ATTEMPT'
): RiskDryRunFormState {
  const base: RiskDryRunFormState = {
    venue: 'polymarket',
    arbitrage_type: 'intra_venue',
    strategy_tier: 'tier1',
    execution_mode: 'PAPER_RESEARCH',
    market_id: 'DRYRUN-POLY-559651',
    requested_notional_usd: 120,
    current_venue_exposure_usd: 0,
    daily_tier_loss_usd: 0,
    net_edge_pct: 2.1,
    liquidity_participation_pct: 4.5,
    data_age_ms: 420,
    leg_imbalance_ratio: 1.25,
    identical_settlement_verified: true,
    partial_fill_protection_enabled: true,
    ai_suggested_override: false,
  };

  switch (preset) {
    case 'LOW_EDGE':
      return { ...base, net_edge_pct: 0.85 };
    case 'STALE_DATA':
      return { ...base, data_age_ms: 24000 };
    case 'AI_OVERRIDE':
      return { ...base, net_edge_pct: 0.5, ai_suggested_override: true };
    case 'LIVE_ATTEMPT':
      return { ...base, execution_mode: 'LIVE' };
    case 'VALID_TRADE':
    default:
      return base;
  }
}

export function summarizeExecutionOutcome(status: string): {
  label: string;
  toneClass: string;
} {
  switch (status) {
    case 'HEDGED_FULL_FILL':
      return { label: 'HEDGED (FULL)', toneClass: 'text-emerald-400' };
    case 'HEDGED_PARTIAL_LEG1':
      return { label: 'HEDGED (PARTIAL LEG 1)', toneClass: 'text-sky-400' };
    case 'PARTIAL_HEDGE_WITH_UNWIND':
      return { label: 'PARTIAL + UNWIND', toneClass: 'text-amber-400' };
    case 'UNWOUND_SECOND_LEG_FAILURE':
      return { label: 'UNWOUND (LEG 2 FAIL)', toneClass: 'text-amber-300' };
    case 'REJECTED_BY_RISK_GATE':
      return { label: 'DENIED BY RISK GATE', toneClass: 'text-rose-400' };
    default:
      return { label: status, toneClass: 'text-slate-300' };
  }
}

export function formatScoreMetric(value: number | null | undefined, decimals = 4): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  return value.toFixed(decimals);
}

export function formatProbPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  return `${(value * 100).toFixed(1)}%`;
}

export function summarizeAgentTaskStatus(status: string): {
  label: string;
  toneClass: string;
} {
  switch (status) {
    case 'COMPLETED':
      return { label: 'COMPLETED', toneClass: 'text-emerald-400' };
    case 'RUNNING':
    case 'LEASED':
      return { label: status, toneClass: 'text-sky-400' };
    case 'QUEUED':
    case 'PENDING':
      return { label: status, toneClass: 'text-slate-300' };
    case 'BLOCKED':
      return { label: 'BLOCKED', toneClass: 'text-amber-400' };
    case 'ESCALATED':
    case 'FAILED':
    case 'CANCELLED':
      return { label: status, toneClass: 'text-rose-400' };
    default:
      return { label: status, toneClass: 'text-slate-300' };
  }
}

export function computeBudgetUtilizationPct(spentUsd: number, dailyCapUsd: number): number {
  if (!Number.isFinite(spentUsd) || !Number.isFinite(dailyCapUsd) || dailyCapUsd <= 0) {
    return 0;
  }
  return Math.max(0, Math.min(100, (spentUsd / dailyCapUsd) * 100));
}

export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return '—';
  }
  if (seconds < 60) {
    return `${seconds.toFixed(1)}s`;
  }
  const totalSec = Math.floor(seconds);
  const hrs = Math.floor(totalSec / 3600);
  const mins = Math.floor((totalSec % 3600) / 60);
  const remSec = totalSec % 60;
  if (hrs > 0) {
    return `${hrs}h ${String(mins).padStart(2, '0')}m ${String(remSec).padStart(2, '0')}s`;
  }
  return `${mins}m ${String(remSec).padStart(2, '0')}s`;
}

export function summarizeValidationStatus(status: string): {
  label: string;
  toneClass: string;
} {
  switch (status) {
    case 'VERIFIED':
    case 'PASSED':
    case 'NOMINAL':
      return { label: status, toneClass: 'text-emerald-400' };
    case 'RUNNING':
      return { label: status, toneClass: 'text-sky-400' };
    case 'STOPPED_EARLY':
    case 'UNCERTAIN_INSUFFICIENT_EVIDENCE':
    case 'UNVERIFIED':
    case 'DEGRADED':
      return { label: status, toneClass: 'text-amber-400' };
    case 'REJECTED_INJECTION':
    case 'DISPROVED':
    case 'STALE_DATA':
    case 'CONFLICTING_EVIDENCE':
    case 'CONTRACT_MISMATCH_BLOCKED':
    case 'UNEXPECTED_SHUTDOWN':
    case 'FAILED':
    case 'CRITICAL':
      return { label: status, toneClass: 'text-rose-400' };
    default:
      return { label: status, toneClass: 'text-slate-300' };
  }
}

export const ROLE_LEVELS: Record<'READ_ONLY' | 'ANALYST' | 'OPERATOR' | 'OWNER', number> = {
  READ_ONLY: 10,
  ANALYST: 20,
  OPERATOR: 30,
  OWNER: 40,
};

export function hasRoleAtLeast(
  role: 'READ_ONLY' | 'ANALYST' | 'OPERATOR' | 'OWNER' | null | undefined,
  minRole: 'READ_ONLY' | 'ANALYST' | 'OPERATOR' | 'OWNER'
): boolean {
  if (!role) return false;
  return (ROLE_LEVELS[role] ?? 0) >= ROLE_LEVELS[minRole];
}

export function canAccessWorkspaceTab(
  role: 'READ_ONLY' | 'ANALYST' | 'OPERATOR' | 'OWNER' | null | undefined,
  tab: string
): boolean {
  if (!role) return false;
  if (tab === 'access') {
    return role === 'OWNER';
  }
  return hasRoleAtLeast(role, 'READ_ONLY');
}

export function describeUserRole(role: string): {
  label: string;
  toneClass: string;
} {
  switch (role) {
    case 'OWNER':
      return { label: 'OWNER', toneClass: 'text-amber-300 border-amber-700/70 bg-amber-950/30' };
    case 'OPERATOR':
      return { label: 'OPERATOR', toneClass: 'text-sky-300 border-sky-700/70 bg-sky-950/30' };
    case 'ANALYST':
      return { label: 'ANALYST', toneClass: 'text-emerald-300 border-emerald-700/70 bg-emerald-950/30' };
    case 'READ_ONLY':
    default:
      return { label: 'READ ONLY', toneClass: 'text-slate-300 border-slate-700 bg-slate-800/70' };
  }
}

export function summarizeAuditOutcome(outcome: string): {
  label: string;
  toneClass: string;
} {
  switch (outcome.toUpperCase()) {
    case 'SUCCESS':
      return { label: 'SUCCESS', toneClass: 'text-emerald-400' };
    case 'DENIED':
      return { label: 'DENIED', toneClass: 'text-rose-400' };
    default:
      return { label: outcome.toUpperCase(), toneClass: 'text-amber-400' };
  }
}

export interface ParsedApiResponse {
  ok: boolean;
  status: number;
  data: unknown | null;
  errorDetail: string | null;
}

export function parseApiJsonResponse(
  rawText: string,
  contentType: string,
  ok: boolean,
  status: number
): ParsedApiResponse {
  const ct = (contentType || '').toLowerCase();
  const trimmed = (rawText || '').trimStart();
  const looksLikeHtml =
    ct.includes('text/html') ||
    trimmed.startsWith('<!') ||
    trimmed.toLowerCase().startsWith('<html');

  let parsedBody: unknown = null;
  if (!looksLikeHtml && trimmed.length > 0) {
    try {
      parsedBody = JSON.parse(rawText);
    } catch {
      parsedBody = null;
    }
  }

  if (!ok) {
    let detail = `HTTP ${status}`;
    if (parsedBody && typeof parsedBody === 'object') {
      const obj = parsedBody as Record<string, unknown>;
      if (typeof obj.detail === 'string' && obj.detail) detail = obj.detail;
      else if (typeof obj.error === 'string' && obj.error) detail = obj.error;
    } else if (looksLikeHtml) {
      detail = `Backend API returned HTTP ${status} HTML instead of JSON. FastAPI backend may be starting or unavailable.`;
    }
    return { ok: false, status, data: null, errorDetail: detail };
  }

  if (looksLikeHtml) {
    return {
      ok: false,
      status: 503,
      data: null,
      errorDetail:
        'Backend service is still starting or unreachable (proxy returned HTML instead of JSON).',
    };
  }

  if (parsedBody === null) {
    return {
      ok: false,
      status: 502,
      data: null,
      errorDetail: 'Backend returned empty or invalid JSON response.',
    };
  }

  return { ok: true, status, data: parsedBody, errorDetail: null };
}

export function formatDriverWeightMultiplier(weight: number | null | undefined): string {
  if (weight === null || weight === undefined || !Number.isFinite(weight)) {
    return '1.00x';
  }
  return `${weight.toFixed(2)}x`;
}

export function summarizeDriverLearningWeight(
  weight: number | null | undefined,
  sampleCount: number | null | undefined,
  cumulativeSkill: number | null | undefined
): {
  badge: 'UPWEIGHTED' | 'DAMPENED' | 'BASELINE';
  label: string;
  toneClass: string;
} {
  const w = typeof weight === 'number' && Number.isFinite(weight) ? weight : 1.0;
  const samples = typeof sampleCount === 'number' && Number.isFinite(sampleCount) ? sampleCount : 0;
  const skill =
    typeof cumulativeSkill === 'number' && Number.isFinite(cumulativeSkill) ? cumulativeSkill : 0;

  if (samples <= 0) {
    return {
      badge: 'BASELINE',
      label: 'BASELINE (1.00x)',
      toneClass: 'text-slate-300 border-slate-700 bg-slate-800/70',
    };
  }
  if (w >= 1.05 || (w > 1.0 && skill > 0)) {
    return {
      badge: 'UPWEIGHTED',
      label: `UPWEIGHTED (${w.toFixed(2)}x)`,
      toneClass: 'text-emerald-300 border-emerald-700/70 bg-emerald-950/30',
    };
  }
  if (w <= 0.95 || (w < 1.0 && skill < 0)) {
    return {
      badge: 'DAMPENED',
      label: `DAMPENED (${w.toFixed(2)}x)`,
      toneClass: 'text-rose-300 border-rose-700/70 bg-rose-950/30',
    };
  }
  return {
    badge: 'BASELINE',
    label: `BASELINE (${w.toFixed(2)}x)`,
    toneClass: 'text-sky-300 border-sky-700/70 bg-sky-950/30',
  };
}

export function formatSolanaTokenPrice(price: number | null | undefined): string {
  if (price === null || price === undefined || !Number.isFinite(price)) {
    return '—';
  }
  if (price === 0) return '$0.00';
  if (price < 0.0001) return `$${price.toFixed(8)}`;
  if (price < 0.01) return `$${price.toFixed(6)}`;
  if (price < 1) return `$${price.toFixed(4)}`;
  return `$${price.toFixed(2)}`;
}

export function summarizeSolanaCandidateStatus(status: string): {
  label: string;
  toneClass: string;
} {
  switch (status) {
    case 'PAPER_ENTERED':
      return { label: 'PAPER ENTERED', toneClass: 'text-cyan-300' };
    case 'PAPER_QUOTE_REQUIRED':
      return { label: 'READY FOR PAPER QUOTE', toneClass: 'text-emerald-400' };
    case 'NO_TRADE':
      return { label: 'NO TRADE (SCORE/BAL)', toneClass: 'text-amber-400' };
    case 'REJECTED':
      return { label: 'REJECTED (FAIL-CLOSED)', toneClass: 'text-rose-400' };
    case 'DISCOVERED':
    default:
      return { label: status || 'DISCOVERED', toneClass: 'text-slate-300' };
  }
}

export function summarizeProviderStatus(status: string): {
  label: string;
  toneClass: string;
} {
  switch (status) {
    case 'AVAILABLE':
      return { label: 'AVAILABLE', toneClass: 'text-emerald-400' };
    case 'RATE_LIMITED':
      return { label: 'RATE LIMITED', toneClass: 'text-amber-400' };
    case 'UNAVAILABLE':
    default:
      return { label: 'UNAVAILABLE', toneClass: 'text-slate-400' };
  }
}

export function summarizeBlotterResolution(result: string | null | undefined): {
  label: string;
  toneClass: string;
  isPending: boolean;
} {
  const normalized = (result ?? 'PENDING').trim().toUpperCase();
  switch (normalized) {
    case 'YES':
      return {
        label: 'YES',
        toneClass: 'text-emerald-300 border-emerald-700/70 bg-emerald-950/30',
        isPending: false,
      };
    case 'NO':
      return {
        label: 'NO',
        toneClass: 'text-rose-300 border-rose-700/70 bg-rose-950/30',
        isPending: false,
      };
    case 'VOID':
      return {
        label: 'VOID',
        toneClass: 'text-amber-300 border-amber-700/70 bg-amber-950/30',
        isPending: false,
      };
    case 'PENDING':
    default:
      return {
        label: 'PENDING',
        toneClass: 'text-slate-300 border-slate-700 bg-slate-800/70',
        isPending: true,
      };
  }
}

export function canConfirmBlotterResolution(
  resolutionResult: string | null | undefined,
  canResolve: boolean
): boolean {
  if (!canResolve) return false;
  return summarizeBlotterResolution(resolutionResult).isPending;
}


