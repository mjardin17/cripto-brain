import test from 'node:test';
import assert from 'node:assert/strict';
import {
  computeBudgetUtilizationPct,
  describeProvenance,
  filterOpportunities,
  formatDurationSeconds,
  formatPct,
  formatPrice,
  formatProbPct,
  formatScoreMetric,
  formatUsd,
  getRiskDryRunPreset,
  summarizeAgentTaskStatus,
  summarizeExecutionOutcome,
  summarizeValidationStatus,
} from './formatters';
import type { ArbitrageOpportunity } from '../types/certainty';

const sampleOpps: ArbitrageOpportunity[] = [
  {
    opp_id: 'OPP-1',
    scan_id: 'SCAN-1',
    market_id: '101',
    condition_id: '0x1',
    question: 'Will CPI exceed 3.0%?',
    slug: 'cpi-3',
    venue: 'polymarket',
    target_shares: 25,
    yes_vwap: 0.45,
    no_vwap: 0.51,
    combined_raw_cost: 0.96,
    taker_fees_usd: 0.2,
    slippage_buffer_usd: 0.04,
    total_executable_cost: 0.9696,
    guaranteed_payout: 1.0,
    gross_edge_pct: 4.0,
    net_edge_pct: 3.04,
    liquidity_participation_pct: 5.0,
    leg_imbalance_ratio: 1.1,
    executable: true,
    risk_gate_approved: true,
    risk_gate_reason: 'APPROVED: All checks passed',
    data_age_ms: 200,
    evaluated_at: '2026-11-12T18:00:00Z',
  },
  {
    opp_id: 'OPP-2',
    scan_id: 'SCAN-1',
    market_id: '202',
    condition_id: '0x2',
    question: 'Will Fed cut 25bps?',
    slug: 'fed-cut',
    venue: 'polymarket',
    target_shares: 25,
    yes_vwap: 0.50,
    no_vwap: 0.51,
    combined_raw_cost: 1.01,
    taker_fees_usd: 0.25,
    slippage_buffer_usd: 0.04,
    total_executable_cost: 1.0216,
    guaranteed_payout: 1.0,
    gross_edge_pct: -1.0,
    net_edge_pct: -2.16,
    liquidity_participation_pct: 4.0,
    leg_imbalance_ratio: 1.3,
    executable: true,
    risk_gate_approved: false,
    risk_gate_reason: 'DENIED (MIN_NET_EDGE)',
    data_age_ms: 220,
    evaluated_at: '2026-11-12T18:00:00Z',
  },
];

test('currency and percentage formatting use explicit tabular output', () => {
  assert.equal(formatUsd(2500), '$2,500.00');
  assert.equal(formatUsd(-12.5), '-$12.50');
  assert.equal(formatUsd(null), '—');
  assert.equal(formatPrice(0.4512), '$0.4512');
  assert.equal(formatPct(2.15, 2, true), '+2.15%');
  assert.equal(formatPct(-0.26, 2, true), '-0.26%');
});

test('opportunity filter handles search query and Risk Gate approval status', () => {
  assert.equal(filterOpportunities(sampleOpps, '', 'ALL').length, 2);
  assert.equal(filterOpportunities(sampleOpps, '', 'APPROVED').length, 1);
  assert.equal(filterOpportunities(sampleOpps, '', 'DENIED').length, 1);
  assert.equal(filterOpportunities(sampleOpps, 'fed', 'ALL')[0]?.market_id, '202');
});

test('provenance descriptor, execution outcome formatter, and dry-run presets behave deterministically', () => {
  assert.equal(describeProvenance('LIVE').label, 'LIVE');
  assert.equal(describeProvenance('SIMULATED').label, 'SIMULATED');
  assert.equal(describeProvenance('UNAVAILABLE').label, 'UNAVAILABLE');
  assert.equal(summarizeExecutionOutcome('UNWOUND_SECOND_LEG_FAILURE').label, 'UNWOUND (LEG 2 FAIL)');
  assert.equal(summarizeExecutionOutcome('REJECTED_BY_RISK_GATE').label, 'DENIED BY RISK GATE');
  const aiPreset = getRiskDryRunPreset('AI_OVERRIDE');
  assert.equal(aiPreset.ai_suggested_override, true);
  const livePreset = getRiskDryRunPreset('LIVE_ATTEMPT');
  assert.equal(livePreset.execution_mode, 'LIVE');
});

test('Stage 3D probability, task status, and model budget formatters handle bounds cleanly', () => {
  assert.equal(formatScoreMetric(0.04123, 4), '0.0412');
  assert.equal(formatScoreMetric(null), '—');
  assert.equal(formatProbPct(0.725), '72.5%');
  assert.equal(formatProbPct(undefined), '—');
  assert.equal(summarizeAgentTaskStatus('COMPLETED').toneClass, 'text-emerald-400');
  assert.equal(summarizeAgentTaskStatus('ESCALATED').toneClass, 'text-rose-400');
  assert.equal(summarizeAgentTaskStatus('BLOCKED').toneClass, 'text-amber-400');
  assert.equal(computeBudgetUtilizationPct(0.125, 0.5), 25);
  assert.equal(computeBudgetUtilizationPct(1.2, 0.5), 100);
  assert.equal(computeBudgetUtilizationPct(0.1, 0), 0);
});

test('Stage 3E provenance labels, duration formatter, and validation status helpers distinguish modes clearly', () => {
  assert.equal(describeProvenance('HISTORICAL').label, 'HISTORICAL');
  assert.equal(describeProvenance('SHORT_VALIDATION').label, 'SHORT VALIDATION');
  assert.equal(describeProvenance('SOAK_TEST').label, 'SOAK TEST');
  assert.equal(formatDurationSeconds(42.34), '42.3s');
  assert.equal(formatDurationSeconds(3665), '1h 01m 05s');
  assert.equal(formatDurationSeconds(null), '—');
  assert.equal(summarizeValidationStatus('REJECTED_INJECTION').toneClass, 'text-rose-400');
  assert.equal(summarizeValidationStatus('VERIFIED').toneClass, 'text-emerald-400');
  assert.equal(summarizeValidationStatus('STOPPED_EARLY').toneClass, 'text-amber-400');
});

