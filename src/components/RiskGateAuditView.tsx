import { useState } from 'react';
import type {
  KillSwitchState,
  RiskAuditEntry,
  RiskEvalResponse,
  RiskPolicyConfig,
  UserPermissions,
} from '../types/certainty';
import {
  formatPct,
  formatTimestampShort,
  formatUsd,
  getRiskDryRunPreset,
  type RiskDryRunFormState,
} from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface RiskGateAuditViewProps {
  policy: RiskPolicyConfig;
  killSwitch: KillSwitchState;
  audits: RiskAuditEntry[];
  permissions?: UserPermissions;
  onToggleKillSwitch: (active: boolean, reason: string) => Promise<void>;
  onEvaluateDryRun: (payload: RiskDryRunFormState) => Promise<RiskEvalResponse>;
}

export function RiskGateAuditView({
  policy,
  killSwitch,
  audits,
  permissions,
  onToggleKillSwitch,
  onEvaluateDryRun,
}: RiskGateAuditViewProps) {
  const canToggleKillSwitch = permissions?.can_toggle_kill_switch ?? true;
  const canEvaluateDryRun = permissions?.can_evaluate_risk_dry_run ?? true;
  const [ksReason, setKsReason] = useState('Operator risk control verification');
  const [ksSubmitting, setKsSubmitting] = useState(false);

  const [formState, setFormState] = useState<RiskDryRunFormState>(() =>
    getRiskDryRunPreset('VALID_TRADE')
  );
  const [evalResult, setEvalResult] = useState<RiskEvalResponse | null>(null);
  const [evalError, setEvalError] = useState<string | null>(null);
  const [evaluating, setEvaluating] = useState(false);

  const handleKillSwitch = async () => {
    setKsSubmitting(true);
    try {
      await onToggleKillSwitch(!killSwitch.active, ksReason.trim() || 'Operator toggle');
    } finally {
      setKsSubmitting(false);
    }
  };

  const handleEvaluate = async (e: React.FormEvent) => {
    e.preventDefault();
    setEvaluating(true);
    setEvalError(null);
    try {
      const res = await onEvaluateDryRun(formState);
      setEvalResult(res);
    } catch (err) {
      setEvalError(err instanceof Error ? err.message : 'Risk Gate evaluation failed');
    } finally {
      setEvaluating(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Kill Switch & Policy Bounds */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">Global Kill Switch</h2>
            <ProvenanceLabel provenance="LIVE" />
          </div>

          <div className="p-3.5 rounded-lg bg-[#0E1522] border border-slate-800">
            <div className="text-xs text-slate-400">Current State</div>
            <div
              className={`mt-1 text-lg font-mono font-semibold ${
                killSwitch.active ? 'text-rose-400' : 'text-emerald-400'
              }`}
              data-testid="kill-switch-status"
            >
              {killSwitch.active ? 'ACTIVE — HALTED' : 'INACTIVE — MONITORING'}
            </div>
            {killSwitch.reason ? (
              <div className="mt-1 text-xs text-slate-400">Reason: {killSwitch.reason}</div>
            ) : null}
          </div>

          <div>
            <label htmlFor="ks-reason-input" className="block text-xs font-medium text-slate-300 mb-1.5">
              Governance Audit Reason
            </label>
            <input
              id="ks-reason-input"
              type="text"
              value={ksReason}
              onChange={(e) => setKsReason(e.target.value)}
              className="w-full px-3 py-2 text-xs bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
            />
          </div>

          <button
            type="button"
            disabled={ksSubmitting || ksReason.trim().length < 3 || !canToggleKillSwitch}
            title={canToggleKillSwitch ? 'Toggle global kill switch' : 'Requires OWNER role'}
            onClick={handleKillSwitch}
            className={`w-full px-4 py-2.5 text-xs font-semibold rounded-lg transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${
              killSwitch.active
                ? 'bg-emerald-600 hover:bg-emerald-500 text-white'
                : 'bg-rose-600 hover:bg-rose-500 text-white'
            }`}
          >
            {ksSubmitting
              ? 'Updating Kill Switch...'
              : !canToggleKillSwitch
                ? 'Kill Switch Locked (OWNER Required)'
                : killSwitch.active
                  ? 'Deactivate Kill Switch'
                  : 'Activate Emergency Kill Switch'}
          </button>
        </section>

        <section className="lg:col-span-2 border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">
              Immutable Phase 1 Fail-Closed Policy
            </h2>
            <span className="text-xs font-mono text-slate-400">Non-overridable by LLM / UI</span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Min Intra-Venue Edge</div>
              <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
                {policy.min_intra_venue_edge_pct.toFixed(2)}%
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Min Cross-Venue Edge</div>
              <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
                {policy.min_cross_venue_edge_pct.toFixed(2)}%
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Max Liquidity Part.</div>
              <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
                {policy.max_liquidity_participation_pct.toFixed(1)}%
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Stale-Data Cutoff</div>
              <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
                {(policy.max_data_age_ms / 1000).toFixed(1)}s
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Polymarket Venue Cap</div>
              <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
                {formatUsd(policy.max_venue_exposure_usd.polymarket)}
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Tier 1 Daily Loss Limit</div>
              <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
                {formatUsd(policy.daily_loss_limits_usd.tier1)}
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Max Leg Imbalance</div>
              <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
                {policy.max_leg_imbalance_ratio.toFixed(1)}x
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Live Trading Lock</div>
              <div className="mt-1 font-mono text-sm font-semibold text-emerald-400">
                {policy.live_trading_enabled ? 'UNLOCKED' : 'LOCKED'}
              </div>
            </div>
          </div>
        </section>
      </div>

      {/* Interactive Risk Gate Dry-Run Evaluator */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-800">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">
              Interactive Risk Gate Authorization Verifier
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Every dry-run check executes against the independent Python Risk Gate and appends to the DuckDB audit log.
            </p>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {(
              [
                ['VALID_TRADE', 'Valid Trade'],
                ['LOW_EDGE', 'Edge < 1.5%'],
                ['STALE_DATA', 'Stale Book'],
                ['AI_OVERRIDE', 'AI Override Attempt'],
                ['LIVE_ATTEMPT', 'Live Mode Attempt'],
              ] as const
            ).map(([presetKey, label]) => (
              <button
                key={presetKey}
                type="button"
                onClick={() => setFormState(getRiskDryRunPreset(presetKey))}
                className="px-2.5 py-1 text-xs bg-[#0E1522] hover:bg-slate-800 border border-slate-700 rounded text-slate-300 transition-colors"
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <form onSubmit={handleEvaluate} className="grid grid-cols-1 sm:grid-cols-3 lg:grid-cols-6 gap-3 items-end text-xs">
          <div>
            <label htmlFor="rg-market" className="block text-slate-400 mb-1">Market ID</label>
            <input
              id="rg-market"
              type="text"
              value={formState.market_id}
              onChange={(e) => setFormState({ ...formState, market_id: e.target.value })}
              className="w-full px-2.5 py-2 font-mono bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
            />
          </div>
          <div>
            <label htmlFor="rg-edge" className="block text-slate-400 mb-1">Net Edge (%)</label>
            <input
              id="rg-edge"
              type="number"
              step="0.05"
              value={formState.net_edge_pct}
              onChange={(e) => setFormState({ ...formState, net_edge_pct: Number(e.target.value) })}
              className="w-full px-2.5 py-2 font-mono tabular-nums bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
            />
          </div>
          <div>
            <label htmlFor="rg-part" className="block text-slate-400 mb-1">Participation (%)</label>
            <input
              id="rg-part"
              type="number"
              step="0.5"
              value={formState.liquidity_participation_pct}
              onChange={(e) =>
                setFormState({ ...formState, liquidity_participation_pct: Number(e.target.value) })
              }
              className="w-full px-2.5 py-2 font-mono tabular-nums bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
            />
          </div>
          <div>
            <label htmlFor="rg-age" className="block text-slate-400 mb-1">Data Age (ms)</label>
            <input
              id="rg-age"
              type="number"
              step="100"
              value={formState.data_age_ms}
              onChange={(e) => setFormState({ ...formState, data_age_ms: Number(e.target.value) })}
              className="w-full px-2.5 py-2 font-mono tabular-nums bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
            />
          </div>
          <div>
            <label htmlFor="rg-mode" className="block text-slate-400 mb-1">Execution Mode</label>
            <select
              id="rg-mode"
              value={formState.execution_mode}
              onChange={(e) =>
                setFormState({
                  ...formState,
                  execution_mode: e.target.value as RiskDryRunFormState['execution_mode'],
                })
              }
              className="w-full px-2.5 py-2 font-mono bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
            >
              <option value="PAPER_RESEARCH">PAPER_RESEARCH</option>
              <option value="PAPER_EXECUTION">PAPER_EXECUTION</option>
              <option value="LIVE">LIVE</option>
            </select>
          </div>
          <button
            type="submit"
            disabled={evaluating || !canEvaluateDryRun}
            title={canEvaluateDryRun ? 'Run Risk Gate evaluation' : 'Requires ANALYST role or higher'}
            className="px-4 py-2 font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors h-[34px]"
          >
            {evaluating ? 'Evaluating...' : !canEvaluateDryRun ? 'Read-Only' : 'Evaluate Risk Gate'}
          </button>
        </form>

        {evalError ? (
          <div className="p-3 rounded-lg bg-rose-950/30 border border-rose-900 text-xs text-rose-300">
            {evalError}
          </div>
        ) : null}

        {evalResult ? (
          <div
            className="p-4 rounded-lg bg-[#0E1522] border border-slate-800 space-y-3"
            data-testid="risk-eval-result"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-xs font-mono">
                <span
                  className={`font-semibold ${
                    evalResult.approved ? 'text-emerald-400' : 'text-rose-400'
                  }`}
                >
                  {evalResult.decision}
                </span>
                <span className="text-slate-500"> · </span>
                <span className="text-slate-300">{evalResult.audit_id}</span>
              </div>
              <div className="text-xs text-slate-400">{evalResult.summary}</div>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 text-xs">
              {evalResult.checks.map((chk) => (
                <div
                  key={chk.rule_id}
                  className="px-2.5 py-2 rounded bg-[#0B111B] border border-slate-800/80 flex items-start justify-between gap-2"
                >
                  <div className="min-w-0">
                    <div className="font-mono text-[11px] text-slate-300 truncate">{chk.rule_id}</div>
                    <div className="text-[11px] text-slate-500 truncate" title={chk.detail}>
                      {chk.detail}
                    </div>
                  </div>
                  <span
                    className={`font-mono font-semibold shrink-0 ${
                      chk.passed ? 'text-emerald-400' : 'text-rose-400'
                    }`}
                  >
                    {chk.passed ? 'PASS' : 'FAIL'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </section>

      {/* Full Audit Trail */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-slate-800 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-100">
            Immutable Risk Gate Audit Trail ({audits.length})
          </h2>
          <ProvenanceLabel provenance="LIVE" detail="DuckDB Persisted" />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-slate-800 bg-[#0E1522] text-slate-400 font-medium">
                <th className="py-2.5 px-4">Audit ID</th>
                <th className="py-2.5 px-3">Market</th>
                <th className="py-2.5 px-3">Mode</th>
                <th className="py-2.5 px-3 text-right">Net Edge</th>
                <th className="py-2.5 px-3 text-right">Part. %</th>
                <th className="py-2.5 px-3">Decision</th>
                <th className="py-2.5 px-4">Reason</th>
                <th className="py-2.5 px-4">Decided At</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/80">
              {audits.map((a) => (
                <tr key={a.audit_id} className="hover:bg-slate-800/30">
                  <td className="py-2.5 px-4 font-mono text-slate-200">{a.audit_id}</td>
                  <td className="py-2.5 px-3 font-mono text-slate-300">{a.market_id}</td>
                  <td className="py-2.5 px-3 font-mono text-slate-400">{a.execution_mode}</td>
                  <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                    {formatPct(a.net_edge_pct, 2, true)}
                  </td>
                  <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-300">
                    {formatPct(a.liquidity_participation_pct, 2)}
                  </td>
                  <td
                    className={`py-2.5 px-3 font-mono font-semibold ${
                      a.decision === 'APPROVED' ? 'text-emerald-400' : 'text-rose-400'
                    }`}
                  >
                    {a.decision}
                  </td>
                  <td className="py-2.5 px-4 text-slate-400 max-w-xs truncate" title={a.summary}>
                    {a.summary}
                  </td>
                  <td className="py-2.5 px-4 font-mono tabular-nums text-slate-500 whitespace-nowrap">
                    {formatTimestampShort(a.decided_at)}
                  </td>
                </tr>
              ))}
              {audits.length === 0 ? (
                <tr>
                  <td colSpan={8} className="py-8 text-center text-slate-500">
                    No Risk Gate decisions logged yet.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
