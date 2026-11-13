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
