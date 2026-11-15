import { useState } from 'react';
import { Play, RotateCcw, Square, Zap } from 'lucide-react';
import { certaintyApi } from '../api/client';
import type { IntelligenceCommandCenterPayload, UserPermissions } from '../types/certainty';
import {
  computeBudgetUtilizationPct,
  formatProbPct,
  formatScoreMetric,
  formatTimestampShort,
  formatUsd,
  summarizeAgentTaskStatus,
} from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';
import { OperationsStage3EPanels } from './OperationsStage3EPanels';

interface IntelligenceCenterViewProps {
  data: IntelligenceCommandCenterPayload | null;
  loading: boolean;
  permissions?: UserPermissions;
  onRefresh: () => Promise<void>;
}

const ROLE_DEFAULT_TASKS: Record<string, string> = {
  MARKET_INTELLIGENCE: 'CHECK_FEED_HEALTH',
  ARBITRAGE_RESEARCH: 'ARBITRAGE_ANALYSIS',
  PROBABILITY_RESEARCH: 'CALIBRATE_PROBABILITY',
  RISK_INTELLIGENCE: 'AUDIT_RISK_STATE',
  LEARNING: 'CONSOLIDATE_LEARNING',
};

export function IntelligenceCenterView({
  data,
  loading,
  permissions,
  onRefresh,
}: IntelligenceCenterViewProps) {
  const canDispatchAgents = permissions?.can_dispatch_agents ?? true;
  const canScaleAgents = permissions?.can_scale_agents ?? true;
  const canSubmitResearch = permissions?.can_submit_research ?? true;
  const canResolvePreds = permissions?.can_resolve_predictions ?? true;
  const canRouteModels = permissions?.can_route_models ?? true;
  const canActivateConfigs = permissions?.can_activate_configs ?? true;
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Task submission form state
  const [taskAgent, setTaskAgent] = useState<string>('MARKET_INTELLIGENCE');
  const [taskType, setTaskType] = useState<string>('CHECK_FEED_HEALTH');
  const [taskPriority, setTaskPriority] = useState<number>(5);
  const [taskDedupKey, setTaskDedupKey] = useState<string>('');

  // Advisory prediction form state
  const [predAgent, setPredAgent] = useState<string>('probability_research_agent');
  const [predMarketId, setPredMarketId] = useState<string>('POLY-FED-HOLD');
  const [predVenue, setPredVenue] = useState<string>('polymarket');
  const [predSubject, setPredSubject] = useState<string>('fomc_rate_hold');
  const [predQuestion, setPredQuestion] = useState<string>('Will FOMC hold target rate unchanged at next meeting?');
  const [predProb, setPredProb] = useState<string>('0.74');
  const [predImplied, setPredImplied] = useState<string>('0.68');

  const runAction = async (key: string, fn: () => Promise<string>) => {
    setBusyAction(key);
    setActionError(null);
    setNotice(null);
    try {
      const msg = await fn();
      setNotice(msg);
      await onRefresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Action failed');
    } finally {
      setBusyAction(null);
    }
  };

  if (loading && !data) {
    return (
      <div className="space-y-4">
        <div className="h-28 rounded-xl bg-[#111827] border border-slate-800 animate-pulse" />
        <div className="h-72 rounded-xl bg-[#111827] border border-slate-800 animate-pulse" />
      </div>
    );
  }

  if (!data) {
    return (
      <div className="border border-slate-800 bg-[#111827] rounded-xl p-6 text-sm text-slate-300 space-y-3">
        <div>Intelligence Command Center data unavailable.</div>
        <button
          type="button"
          onClick={() => void onRefresh()}
          className="px-3.5 py-2 text-xs font-semibold bg-sky-600 hover:bg-sky-500 text-white rounded-lg"
        >
          Load Intelligence State
        </button>
      </div>
    );
  }

  const { supervisor, calibration, scorecards, predictions, model_routing, configs, tasks, blockers } =
    data;
  const loopState = supervisor.background_loop;
  const budgetPct = computeBudgetUtilizationPct(
    model_routing.daily_spent_usd,
    model_routing.policy.daily_budget_usd
  );
  const selectedProfile = supervisor.agents.find((a) => a.role === taskAgent);
  const supportedTypes = selectedProfile?.supports_task_types ?? [ROLE_DEFAULT_TASKS[taskAgent] || 'CHECK_FEED_HEALTH'];

  return (
    <div className="space-y-6">
      {/* Operational Notice / Error */}
      {notice ? (
        <div
          role="status"
          className="px-4 py-3 rounded-lg border border-sky-800/70 bg-sky-950/30 text-xs text-sky-200 flex items-center justify-between gap-4"
        >
          <span>{notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="text-sky-300 hover:text-white">
            Dismiss
          </button>
        </div>
      ) : null}
      {actionError ? (
        <div
          role="alert"
          className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200 flex items-center justify-between gap-4"
        >
          <span>{actionError}</span>
          <button type="button" onClick={() => setActionError(null)} className="text-rose-300 hover:text-white">
            Dismiss
          </button>
        </div>
      ) : null}

      {/* Top KPI & Supervisor Control Header */}
      <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Supervisor Loop</span>
            <ProvenanceLabel provenance="LIVE" />
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <span
              className={`text-lg font-mono font-semibold ${
                loopState.halted_by_kill_switch
                  ? 'text-rose-400'
                  : loopState.running
                  ? 'text-emerald-400'
                  : 'text-amber-400'
              }`}
            >
              {loopState.halted_by_kill_switch
                ? 'HALTED (KILL SWITCH)'
                : loopState.running
                ? 'RUNNING'
                : 'STOPPED'}
            </span>
            <span className="text-xs font-mono tabular-nums text-slate-400">
              {loopState.ticks} ticks · {loopState.interval_seconds}s
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2 pt-1">
            {loopState.running ? (
              <button
                type="button"
                disabled={busyAction !== null || !canDispatchAgents}
                title={canDispatchAgents ? 'Stop background Supervisor loop' : 'Requires OPERATOR or OWNER role'}
                onClick={() =>
                  void runAction('loop-stop', async () => {
                    const res = await certaintyApi.stopAgentLoop();
                    return `Background Supervisor loop stopped cleanly (${res.shutdown_recovered_count} active leases reclaimed).`;
                  })
                }
                className="px-2.5 py-1.5 text-xs font-medium bg-slate-800 hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed border border-slate-700 text-slate-200 rounded-lg inline-flex items-center gap-1.5"
              >
                <Square className="w-3 h-3 text-rose-400" />
                Stop Loop
              </button>
            ) : (
              <button
                type="button"
                disabled={busyAction !== null || !canDispatchAgents}
                title={canDispatchAgents ? 'Start background Supervisor loop' : 'Requires OPERATOR or OWNER role'}
                onClick={() =>
                  void runAction('loop-start', async () => {
                    const res = await certaintyApi.startAgentLoop({
                      interval_seconds: 5.0,
                      max_tasks_per_tick: supervisor.policy.max_concurrency,
                    });
                    return res.duplicate_prevented
                      ? 'Duplicate loop already running.'
                      : 'Started lifecycle-managed background Supervisor loop (5s interval).';
                  })
                }
                className="px-2.5 py-1.5 text-xs font-medium bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg inline-flex items-center gap-1.5"
              >
                <Play className="w-3 h-3" />
                Start Loop
              </button>
            )}
            <button
              type="button"
              disabled={busyAction !== null || supervisor.kill_switch_active || !canDispatchAgents}
              title={canDispatchAgents ? 'Dispatch queued tasks now' : 'Requires OPERATOR or OWNER role'}
              onClick={() =>
                void runAction('dispatch', async () => {
                  const res = await certaintyApi.dispatchAgentTasks(supervisor.policy.max_concurrency);
                  return res.halted
                    ? 'Dispatch halted by Kill Switch.'
                    : `Dispatched ${res.dispatched_count} tasks (${res.completed_count} completed).`;
                })
              }
              className="px-2.5 py-1.5 text-xs font-medium bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg inline-flex items-center gap-1.5"
            >
              <Zap className="w-3 h-3" />
              Dispatch Now
            </button>
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Worker Pool &amp; Tasks</span>
            <ProvenanceLabel provenance="LIVE" detail="DuckDB" />
          </div>
          <div className="text-lg font-mono font-semibold tabular-nums text-slate-100">
            {supervisor.active_workers_count} / {supervisor.policy.max_concurrency} Workers
          </div>
          <div className="text-xs font-mono text-slate-400">
            Queued: {supervisor.task_counts_by_status.QUEUED ?? 0} · Completed:{' '}
            {supervisor.task_counts_by_status.COMPLETED ?? 0} · Escalated:{' '}
            {supervisor.task_counts_by_status.ESCALATED ?? 0}
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-slate-400 whitespace-nowrap">Calibration</span>
            <ProvenanceLabel
              provenance={calibration.resolved_count > 0 ? 'SIMULATED' : 'UNAVAILABLE'}
              detail={`${calibration.resolved_count} res.`}
            />
          </div>
          <div className="text-lg font-mono font-semibold tabular-nums text-slate-100">
            Brier: {formatScoreMetric(calibration.mean_brier_score, 4)}
          </div>
          <div className="text-xs font-mono text-slate-400">
            ECE: {formatScoreMetric(calibration.expected_calibration_error, 4)} · Log Loss:{' '}
            {formatScoreMetric(calibration.mean_log_loss, 4)}
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Model Routing Spend</span>
            <ProvenanceLabel
              provenance={model_routing.provenance}
              detail={model_routing.provider_status}
            />
          </div>
          <div className="text-lg font-mono font-semibold tabular-nums text-slate-100">
            {formatUsd(model_routing.daily_spent_usd, 4)} /{' '}
            {formatUsd(model_routing.policy.daily_budget_usd, 2)}
          </div>
          <div className="text-xs font-mono text-slate-400">
            Cap/Task: {formatUsd(model_routing.policy.per_task_limit_usd, 2)} · Tokens:{' '}
            {model_routing.total_input_tokens + model_routing.total_output_tokens}
          </div>
        </div>
      </section>

      {/* Section 1 & 2: Specialized Agents + Worker Pool & Concurrency */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <section className="xl:col-span-2 border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                1. Specialized Agent Roles &amp; Safety Boundaries
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Deterministic-first research agents. Live trading, wallet signing, Risk Gate overrides, and Tier 2/3 activation are permanently forbidden.
              </p>
            </div>
            <ProvenanceLabel provenance="LIVE" detail="Advisory Only" />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
            {supervisor.agents.map((ag) => (
              <div
                key={ag.role}
                className="p-3.5 rounded-lg bg-[#0E1522] border border-slate-800 space-y-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono font-semibold text-sky-400">{ag.role}</span>
                  <span className="font-mono text-[11px] text-slate-500">{ag.agent_id}</span>
                </div>
                <p className="text-slate-300 leading-relaxed">{ag.description}</p>
                <div className="text-[11px] font-mono text-slate-400">
                  Tasks: {ag.supports_task_types.join(', ')}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">
              2. Worker Pool &amp; Concurrency
            </h2>
            <ProvenanceLabel provenance="LIVE" />
          </div>

          <div className="p-3.5 rounded-lg bg-[#0E1522] border border-slate-800 space-y-3 text-xs">
            <div className="flex items-center justify-between">
              <span className="text-slate-400">Max Concurrency Cap</span>
              <span className="font-mono font-semibold text-slate-100">
                {supervisor.policy.max_concurrency} (Hard Cap {supervisor.policy.hard_max_concurrency})
              </span>
            </div>
            <div className="flex items-center justify-between gap-2">
              <label htmlFor="max-conc-select" className="text-slate-400">
                Adjust Bound
              </label>
              <select
                id="max-conc-select"
                value={supervisor.policy.max_concurrency}
                disabled={busyAction !== null || !canScaleAgents}
                title={canScaleAgents ? 'Adjust max concurrency' : 'Requires OPERATOR or OWNER role'}
                onChange={(e) => {
                  const val = Number(e.target.value);
                  void runAction('scale-cap', async () => {
                    await certaintyApi.scaleAgentWorkers({ max_concurrency: val });
                    return `Updated bounded max_concurrency to ${val}.`;
                  });
                }}
                className="px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono disabled:opacity-50"
              >
                {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
                  <option key={n} value={n}>
                    {n} workers
                  </option>
                ))}
              </select>
            </div>
            <div className="grid grid-cols-2 gap-2 pt-1 text-[11px] font-mono text-slate-400">
              <div>Startup reaped: {loopState.startup_recovered_count}</div>
              <div>Dup loops blocked: {loopState.duplicate_starts_blocked}</div>
              <div>Max retries: {supervisor.policy.max_retries}</div>
              <div>Lease TTL: {supervisor.policy.lease_seconds}s</div>
            </div>
          </div>

          <div className="space-y-2 text-xs">
            <div className="text-slate-400 font-medium">
              Active Workers ({supervisor.active_workers.length})
            </div>
            {supervisor.active_workers.length === 0 ? (
              <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800 text-slate-500">
                Idle pool scaled to 0 active workers (spawns automatically on queued demand).
              </div>
            ) : (
              <div className="space-y-2">
                {supervisor.active_workers.map((w) => (
                  <div
                    key={w.worker_id}
                    className="p-2.5 rounded-lg bg-[#0E1522] border border-slate-800 flex items-center justify-between gap-2"
                  >
                    <div className="min-w-0">
                      <div className="font-mono text-slate-200 truncate">{w.worker_id}</div>
                      <div className="text-[11px] text-slate-400">{w.agent_role}</div>
                    </div>
                    <span className="font-mono text-[11px] text-emerald-400">{w.status}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      </div>

      {/* Section 3 & 4: Task Queue & Blocker Timeline */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <section className="xl:col-span-2 border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                3. Persistent Task Queue &amp; Execution History ({tasks.length})
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Deduplicated, lease-fenced research tasks persisted in DuckDB across restarts.
              </p>
            </div>
            <ProvenanceLabel provenance="LIVE" detail="DuckDB Persisted" />
          </div>

          {/* Submit Task Form */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void runAction('submit-task', async () => {
                const res = await certaintyApi.submitAgentTask({
                  assigned_agent: taskAgent,
                  task_type: taskType,
                  priority: taskPriority,
                  dedup_key: taskDedupKey.trim() || undefined,
                  payload: {},
                });
                return res.duplicate_prevented
                  ? `Duplicate active task prevented; returned existing ${res.task.task_id}.`
                  : `Submitted ${res.task.task_id} (${res.task.task_type} -> ${res.task.assigned_agent}).`;
              });
            }}
            className="grid grid-cols-1 sm:grid-cols-12 gap-3 p-3.5 rounded-lg bg-[#0E1522] border border-slate-800 text-xs items-end"
          >
            <div className="sm:col-span-4">
              <label htmlFor="task-agent-select" className="block text-slate-300 mb-1">
                Agent Role
              </label>
              <select
                id="task-agent-select"
                value={taskAgent}
                onChange={(e) => {
                  const nextRole = e.target.value;
                  setTaskAgent(nextRole);
                  setTaskType(ROLE_DEFAULT_TASKS[nextRole] || 'CHECK_FEED_HEALTH');
                }}
                className="w-full px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
              >
                {supervisor.agents.map((a) => (
                  <option key={a.role} value={a.role}>
                    {a.role}
                  </option>
                ))}
              </select>
            </div>
            <div className="sm:col-span-4">
              <label htmlFor="task-type-select" className="block text-slate-300 mb-1">
                Task Type
              </label>
              <select
                id="task-type-select"
                value={taskType}
                onChange={(e) => setTaskType(e.target.value)}
                className="w-full px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
              >
                {supportedTypes.map((tt) => (
                  <option key={tt} value={tt}>
                    {tt}
                  </option>
                ))}
              </select>
            </div>
            <div className="sm:col-span-4">
              <label htmlFor="task-dedup-input" className="block text-slate-300 mb-1">
                Priority / Dedup Key
              </label>
              <div className="flex gap-1.5">
                <input
                  id="task-priority-input"
                  aria-label="Task priority"
                  type="number"
                  min={1}
                  max={10}
                  value={taskPriority}
                  onChange={(e) => setTaskPriority(Number(e.target.value))}
                  className="w-14 shrink-0 px-2 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
                />
                <input
                  id="task-dedup-input"
                  type="text"
                  value={taskDedupKey}
                  onChange={(e) => setTaskDedupKey(e.target.value)}
                  placeholder="Dedup key (optional)"
                  className="flex-1 min-w-0 px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 placeholder:text-slate-500"
                />
              </div>
            </div>
            <div className="sm:col-span-12 flex justify-end">
              <button
                type="submit"
                disabled={busyAction !== null || !canDispatchAgents}
                title={canDispatchAgents ? 'Queue research task' : 'Requires OPERATOR or OWNER role'}
                className="px-4 py-2 font-semibold bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg whitespace-nowrap"
              >
                {canDispatchAgents ? 'Queue Bounded Task' : 'Queue Locked (OPERATOR Required)'}
              </button>
            </div>
          </form>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-800 text-slate-400">
                <tr>
                  <th className="py-2 pr-3 font-medium">Task</th>
                  <th className="py-2 pr-3 font-medium">Agent</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                  <th className="py-2 pr-3 font-medium">Attempts</th>
                  <th className="py-2 pr-3 font-medium">Updated</th>
                  <th className="py-2 font-medium">Detail</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {tasks.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="py-4 text-slate-500">
                      No tasks recorded yet. Queue a bounded research task above.
                    </td>
                  </tr>
                ) : (
                  tasks.slice(0, 14).map((t) => {
                    const st = summarizeAgentTaskStatus(t.status);
                    return (
                      <tr key={t.task_id} className="hover:bg-slate-800/30">
                        <td className="py-2.5 pr-3">
                          <div className="font-mono text-slate-100">{t.task_type}</div>
                          <div className="font-mono text-[11px] text-slate-500">{t.task_id}</div>
                        </td>
                        <td className="py-2.5 pr-3 font-mono text-slate-300">{t.assigned_agent}</td>
                        <td className={`py-2.5 pr-3 font-mono font-semibold ${st.toneClass}`}>
                          {st.label}
                        </td>
                        <td className="py-2.5 pr-3 font-mono tabular-nums text-slate-300">
                          {t.attempt_count} / {t.max_retries} (rec {t.recovery_attempts})
                        </td>
                        <td className="py-2.5 pr-3 font-mono text-slate-400">
                          {formatTimestampShort(t.updated_at)}
                        </td>
                        <td className="py-2.5 text-slate-400 max-w-xs truncate">
                          {t.escalation_reason || (t.worker_id ? `Worker ${t.worker_id}` : '—')}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">
              4. Blocker Timeline &amp; Recovery ({blockers.length})
            </h2>
            <ProvenanceLabel provenance="LIVE" />
          </div>

          <div className="space-y-3 text-xs max-h-[420px] overflow-y-auto pr-1">
            {blockers.length === 0 ? (
              <div className="p-3.5 rounded-lg bg-[#0E1522] border border-slate-800 text-slate-500">
                No blocker incidents recorded yet.
              </div>
            ) : (
              blockers.slice(0, 12).map((b) => (
                <div key={b.blocker_id} className="p-3 rounded-lg bg-[#0E1522] border border-slate-800 space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono font-semibold text-amber-400">{b.blocker_class}</span>
                    <span className="font-mono text-[11px] text-slate-400">{b.outcome}</span>
                  </div>
                  <div className="text-slate-300 line-clamp-2">{b.error_message}</div>
                  <div className="font-mono text-[11px] text-slate-400">
                    Strategy: {b.selected_strategy || 'ESCALATED'} · Memory hits: {b.memory_hits.length}
                  </div>
                  <div className="font-mono text-[11px] text-slate-500">
                    {b.blocker_id} · {formatTimestampShort(b.created_at)}
                  </div>
                </div>
              ))
            )}
          </div>
        </section>
      </div>

      {/* Section 5 & 6: Prediction Calibration + Agent Scorecards */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                5. Prediction Calibration &amp; Accuracy
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">{calibration.note}</p>
            </div>
            <ProvenanceLabel
              provenance={calibration.resolved_count > 0 ? 'SIMULATED' : 'UNAVAILABLE'}
              detail={`Resolved=${calibration.resolved_count} · Open=${calibration.open_count} · Void=${calibration.voided_count}`}
            />
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Mean Brier</div>
              <div className="mt-1 font-mono font-semibold tabular-nums text-slate-100">
                {formatScoreMetric(calibration.mean_brier_score, 4)}
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Log Loss</div>
              <div className="mt-1 font-mono font-semibold tabular-nums text-slate-100">
                {formatScoreMetric(calibration.mean_log_loss, 4)}
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">Accuracy</div>
              <div className="mt-1 font-mono font-semibold tabular-nums text-slate-100">
                {calibration.accuracy_pct !== null ? `${calibration.accuracy_pct.toFixed(1)}%` : '—'}
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
              <div className="text-slate-400">ECE (10-bin)</div>
              <div className="mt-1 font-mono font-semibold tabular-nums text-slate-100">
                {formatScoreMetric(calibration.expected_calibration_error, 4)}
              </div>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-800 text-slate-400">
                <tr>
                  <th className="py-1.5 pr-3 font-medium">Prob Bucket</th>
                  <th className="py-1.5 pr-3 font-medium">Resolved N</th>
                  <th className="py-1.5 pr-3 font-medium">Avg Pred</th>
                  <th className="py-1.5 pr-3 font-medium">Empirical YES</th>
                  <th className="py-1.5 font-medium">Gap</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80 font-mono tabular-nums">
                {calibration.buckets.map((b) => (
                  <tr key={b.bucket_index}>
                    <td className="py-1.5 pr-3 text-slate-300">{b.range_label}</td>
                    <td className="py-1.5 pr-3 text-slate-200">{b.count}</td>
                    <td className="py-1.5 pr-3 text-slate-300">{formatProbPct(b.avg_predicted_prob)}</td>
                    <td className="py-1.5 pr-3 text-slate-300">{formatProbPct(b.empirical_yes_rate)}</td>
                    <td className="py-1.5 text-slate-400">{formatScoreMetric(b.calibration_gap, 4)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                6. Agent Performance Scorecards
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Minimum {scorecards.min_resolved_samples_required} resolved predictions required before agents become rank-comparable.
              </p>
            </div>
            <ProvenanceLabel
              provenance={scorecards.comparable_agents_count > 0 ? 'SIMULATED' : 'UNAVAILABLE'}
              detail={`${scorecards.comparable_agents_count} comparable`}
            />
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-800 text-slate-400">
                <tr>
                  <th className="py-2 pr-3 font-medium">Agent</th>
                  <th className="py-2 pr-3 font-medium">Resolved</th>
                  <th className="py-2 pr-3 font-medium">Brier</th>
                  <th className="py-2 pr-3 font-medium">Tasks</th>
                  <th className="py-2 font-medium">Comparison Gate</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {scorecards.scorecards.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="py-4 text-slate-500">
                      No agent prediction or task history recorded yet.
                    </td>
                  </tr>
                ) : (
                  scorecards.scorecards.map((sc) => (
                    <tr key={sc.agent_id}>
                      <td className="py-2 pr-3 font-mono text-slate-100 whitespace-nowrap">
                        {sc.agent_id}
                      </td>
                      <td className="py-2 pr-3 font-mono tabular-nums text-slate-300 whitespace-nowrap">
                        {sc.resolved_predictions} <span className="text-slate-500">({sc.open_predictions} open)</span>
                      </td>
                      <td className="py-2 pr-3 font-mono tabular-nums text-slate-200 whitespace-nowrap">
                        {formatScoreMetric(sc.mean_brier_score, 4)}
                      </td>
                      <td className="py-2 pr-3 font-mono tabular-nums text-slate-300 whitespace-nowrap">
                        {sc.tasks_completed} ok / {sc.tasks_escalated} esc
                      </td>
                      <td
                        className={`py-2 font-mono text-[11px] font-semibold whitespace-nowrap ${
                          sc.comparison_eligible ? 'text-emerald-400' : 'text-amber-400'
                        }`}
                      >
                        {sc.rank ? `RANK #${sc.rank}` : sc.comparison_status}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* Log Advisory Forecast + Quick Resolve */}
          <div className="pt-2 border-t border-slate-800 space-y-3">
            <div className="text-xs font-semibold text-slate-200">
              Log Advisory Forecast (Simulated / Research Only)
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void runAction('create-pred', async () => {
                  const p = Number(predProb);
                  const imp = predImplied.trim() ? Number(predImplied) : null;
                  const created = await certaintyApi.createPrediction({
                    agent_id: predAgent,
                    market_id: predMarketId.trim(),
                    venue: predVenue,
                    subject_key: predSubject.trim(),
                    question: predQuestion.trim(),
                    predicted_prob: p,
                    market_implied_prob: imp,
                    provenance: 'SIMULATED',
                  });
                  return `Logged OPEN prediction ${created.prediction_id} (p=${created.predicted_prob.toFixed(2)}).`;
                });
              }}
              className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 text-xs"
            >
              <div>
                <label htmlFor="pred-agent-input" className="block text-slate-400 mb-1">
                  Agent ID
                </label>
                <input
                  id="pred-agent-input"
                  type="text"
                  value={predAgent}
                  onChange={(e) => setPredAgent(e.target.value)}
                  className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
                />
              </div>
              <div>
                <label htmlFor="pred-market-input" className="block text-slate-400 mb-1">
                  Market ID &amp; Venue
                </label>
                <div className="flex gap-1.5">
                  <input
                    id="pred-market-input"
                    type="text"
                    value={predMarketId}
                    onChange={(e) => setPredMarketId(e.target.value)}
                    className="flex-1 min-w-0 px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
                  />
                  <select
                    aria-label="Prediction venue"
                    value={predVenue}
                    onChange={(e) => setPredVenue(e.target.value)}
                    className="px-2 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 shrink-0"
                  >
                    <option value="polymarket">polymarket</option>
                    <option value="kalshi">kalshi</option>
                  </select>
                </div>
              </div>
              <div>
                <label htmlFor="pred-prob-input" className="block text-slate-400 mb-1">
                  Pred Prob / Implied
                </label>
                <div className="flex gap-1.5">
                  <input
                    id="pred-prob-input"
                    type="number"
                    step="0.01"
                    min="0.01"
                    max="0.99"
                    value={predProb}
                    onChange={(e) => setPredProb(e.target.value)}
                    className="w-1/2 px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
                  />
                  <input
                    aria-label="Market implied probability"
                    type="number"
                    step="0.01"
                    min="0.01"
                    max="0.99"
                    value={predImplied}
                    onChange={(e) => setPredImplied(e.target.value)}
                    className="w-1/2 px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
                  />
                </div>
              </div>
              <div>
                <label htmlFor="pred-subject-input" className="block text-slate-400 mb-1">
                  Subject Key
                </label>
                <input
                  id="pred-subject-input"
                  type="text"
                  value={predSubject}
                  onChange={(e) => setPredSubject(e.target.value)}
                  className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
                />
              </div>
              <div className="sm:col-span-2 flex flex-col sm:flex-row gap-2 sm:items-end">
                <div className="flex-1 min-w-0">
                  <label htmlFor="pred-question-input" className="block text-slate-400 mb-1">
                    Market Question
                  </label>
                  <input
                    id="pred-question-input"
                    type="text"
                    value={predQuestion}
                    onChange={(e) => setPredQuestion(e.target.value)}
                    className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
                  />
                </div>
                <button
                  type="submit"
                  disabled={busyAction !== null || !canSubmitResearch}
                  title={canSubmitResearch ? 'Record advisory prediction' : 'Requires ANALYST role or higher'}
                  className="px-4 py-1.5 font-semibold bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg whitespace-nowrap shrink-0"
                >
                  {canSubmitResearch ? 'Record Prediction' : 'Read-Only'}
                </button>
              </div>
            </form>

            {/* Recent Predictions Resolution Table */}
            <div className="overflow-x-auto max-h-56 overflow-y-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-slate-800 text-slate-400">
                  <tr>
                    <th className="py-1.5 pr-2 font-medium">Prediction</th>
                    <th className="py-1.5 pr-2 font-medium">Prob</th>
                    <th className="py-1.5 pr-2 font-medium">Status / Brier</th>
                    <th className="py-1.5 font-medium">Resolve</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80">
                  {predictions.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="py-3 text-slate-500">
                        No predictions recorded yet.
                      </td>
                    </tr>
                  ) : (
                    predictions.slice(0, 8).map((p) => (
                      <tr key={p.prediction_id}>
                        <td className="py-1.5 pr-2 max-w-[200px]">
                          <div className="text-slate-200 truncate">{p.question}</div>
                          <div className="font-mono text-[11px] text-slate-500">
                            {p.prediction_id} · {p.provenance}
                          </div>
                        </td>
                        <td className="py-1.5 pr-2 font-mono tabular-nums text-slate-200">
                          {formatProbPct(p.predicted_prob)}
                        </td>
                        <td className="py-1.5 pr-2 font-mono text-[11px]">
                          <span
                            className={
                              p.status === 'RESOLVED'
                                ? 'text-emerald-400'
                                : p.status === 'VOIDED'
                                ? 'text-slate-400'
                                : 'text-amber-400'
                            }
                          >
                            {p.status}
                          </span>{' '}
                          {p.brier_score !== null ? `· Brier ${p.brier_score.toFixed(4)}` : ''}
                        </td>
                        <td className="py-1.5">
                          {p.status === 'OPEN' ? (
                            <div className="flex items-center gap-1">
                              <button
                                type="button"
                                disabled={busyAction !== null || !canResolvePreds}
                                title={canResolvePreds ? 'Resolve YES' : 'Requires OPERATOR or OWNER role'}
                                onClick={() =>
                                  void runAction(`res-${p.prediction_id}-1`, async () => {
                                    const r = await certaintyApi.resolvePrediction(p.prediction_id, {
                                      outcome: 1,
                                    });
                                    return `Resolved ${p.prediction_id} = YES (Brier=${r.brier_score?.toFixed(4)}).`;
                                  })
                                }
                                className="px-2 py-0.5 text-[11px] font-mono bg-emerald-800/80 hover:bg-emerald-700 disabled:opacity-40 disabled:cursor-not-allowed text-emerald-100 rounded"
                              >
                                YES
                              </button>
                              <button
                                type="button"
                                disabled={busyAction !== null || !canResolvePreds}
                                title={canResolvePreds ? 'Resolve NO' : 'Requires OPERATOR or OWNER role'}
                                onClick={() =>
                                  void runAction(`res-${p.prediction_id}-0`, async () => {
                                    const r = await certaintyApi.resolvePrediction(p.prediction_id, {
                                      outcome: 0,
                                    });
                                    return `Resolved ${p.prediction_id} = NO (Brier=${r.brier_score?.toFixed(4)}).`;
                                  })
                                }
                                className="px-2 py-0.5 text-[11px] font-mono bg-rose-800/80 hover:bg-rose-700 disabled:opacity-40 disabled:cursor-not-allowed text-rose-100 rounded"
                              >
                                NO
                              </button>
                              <button
                                type="button"
                                disabled={busyAction !== null || !canResolvePreds}
                                title={canResolvePreds ? 'Void prediction' : 'Requires OPERATOR or OWNER role'}
                                onClick={() =>
                                  void runAction(`res-${p.prediction_id}-v`, async () => {
                                    await certaintyApi.resolvePrediction(p.prediction_id, {
                                      outcome: 'VOID',
                                    });
                                    return `Marked ${p.prediction_id} VOIDED (excluded from calibration).`;
                                  })
                                }
                                className="px-2 py-0.5 text-[11px] font-mono bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed text-slate-200 rounded"
                              >
                                VOID
                              </button>
                            </div>
                          ) : (
                            <span className="font-mono text-[11px] text-slate-500">
                              {p.learning_cycle_id ? `Linked ${p.learning_cycle_id}` : 'Finalized'}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </div>

      {/* Section 7 & 8: Model Routing & Config Governance */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                7. Cost-Aware Model Routing &amp; API Usage
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Deterministic-first ($0.00). Server-side API key check only — never exposed to browser.
              </p>
            </div>
            <ProvenanceLabel
              provenance={model_routing.provenance}
              detail={model_routing.provider_status}
            />
          </div>

          <div className="p-3.5 rounded-lg bg-[#0E1522] border border-slate-800 space-y-2.5 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-slate-300">
                Provider Key Configured:{' '}
                <strong className="font-mono text-slate-100">
                  {model_routing.api_key_configured ? 'YES (SERVER ONLY)' : 'NO ($0 SPENT)'}
                </strong>
              </span>
              <span className="font-mono text-[11px] text-emerald-400">
                Frontend Exposure: {String(model_routing.api_key_exposed_to_frontend).toUpperCase()}
              </span>
            </div>
            <div>
              <div className="flex items-center justify-between text-slate-400 mb-1">
                <span>Daily Spend vs Hard Limit</span>
                <span className="font-mono tabular-nums text-slate-200">
                  {formatUsd(model_routing.daily_spent_usd, 4)} /{' '}
                  {formatUsd(model_routing.policy.daily_budget_usd, 2)} ({budgetPct.toFixed(1)}%)
                </span>
              </div>
              <div className="h-2 rounded-full bg-slate-800 overflow-hidden">
                <div
                  className="h-full bg-sky-500 transition-all"
                  style={{ width: `${budgetPct}%` }}
                />
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <button
                type="button"
                disabled={busyAction !== null || !canRouteModels}
                title={canRouteModels ? 'Route deterministic request' : 'Requires OPERATOR or OWNER role'}
                onClick={() =>
                  void runAction('route-det', async () => {
                    const r = await certaintyApi.routeModelRequest({
                      agent_id: 'arbitrage_research_agent',
                      task_category: 'ARBITRAGE_MATH',
                      prefer_ai: true,
                    });
                    return `Routed ${r.task_category} -> ${r.selected_tier} ($${r.actual_cost_usd.toFixed(4)}): ${r.reason}`;
                  })
                }
                className="px-2.5 py-1.5 text-xs font-medium bg-slate-800 hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed border border-slate-700 text-slate-200 rounded-lg"
              >
                Route Deterministic ($0)
              </button>
              <button
                type="button"
                disabled={busyAction !== null || !canRouteModels}
                title={canRouteModels ? 'Test economical AI route' : 'Requires OPERATOR or OWNER role'}
                onClick={() =>
                  void runAction('route-ai', async () => {
                    const r = await certaintyApi.routeModelRequest({
                      agent_id: 'learning_agent',
                      task_category: 'RESEARCH_SYNTHESIS',
                      prefer_ai: true,
                      estimated_input_tokens: 900,
                      estimated_output_tokens: 250,
                    });
                    return `Routed ${r.task_category} -> ${r.selected_tier} ($${r.actual_cost_usd.toFixed(4)}): ${r.reason}`;
                  })
                }
                className="px-2.5 py-1.5 text-xs font-medium bg-sky-700 hover:bg-sky-600 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg"
              >
                Test Economical AI Route
              </button>
            </div>
          </div>

          <div className="overflow-x-auto max-h-52 overflow-y-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-800 text-slate-400">
                <tr>
                  <th className="py-1.5 pr-2 font-medium">Category</th>
                  <th className="py-1.5 pr-2 font-medium">Tier</th>
                  <th className="py-1.5 pr-2 font-medium">Model</th>
                  <th className="py-1.5 font-medium">Cost</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80 font-mono">
                {model_routing.recent_events.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-3 text-slate-500 font-sans">
                      No model routing events recorded yet.
                    </td>
                  </tr>
                ) : (
                  model_routing.recent_events.slice(0, 8).map((ev) => (
                    <tr key={ev.routing_id}>
                      <td className="py-1.5 pr-2 text-slate-200">{ev.task_category}</td>
                      <td className="py-1.5 pr-2 text-sky-400">{ev.selected_tier}</td>
                      <td className="py-1.5 pr-2 text-slate-400">{ev.model_name}</td>
                      <td className="py-1.5 tabular-nums text-slate-200">
                        {formatUsd(ev.actual_cost_usd, 4)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                8. Versioned Configuration &amp; Safe Rollback
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Requires independent review + PASSING regression test. Risk Gate &amp; Security permissions are permanently locked.
              </p>
            </div>
            <ProvenanceLabel provenance="LIVE" detail="Self-Test Gated" />
          </div>

          <div className="overflow-x-auto max-h-72 overflow-y-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-800 text-slate-400">
                <tr>
                  <th className="py-2 pr-3 font-medium">Domain &amp; Version</th>
                  <th className="py-2 pr-3 font-medium">Status</th>
                  <th className="py-2 pr-3 font-medium">Self-Test</th>
                  <th className="py-2 pr-3 font-medium">Reason</th>
                  <th className="py-2 font-medium">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {configs.map((cfg) => (
                  <tr key={cfg.config_version_id}>
                    <td className="py-2 pr-3">
                      <div className="font-mono text-slate-100">
                        {cfg.config_domain} · v{cfg.version}
                      </div>
                      <div className="font-mono text-[11px] text-slate-500">
                        by {cfg.proposed_by}
                      </div>
                    </td>
                    <td
                      className={`py-2 pr-3 font-mono font-semibold ${
                        cfg.status === 'ACTIVE'
                          ? 'text-emerald-400'
                          : cfg.status === 'CANDIDATE'
                          ? 'text-amber-400'
                          : 'text-slate-400'
                      }`}
                    >
                      {cfg.status}
                    </td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-300">
                      {cfg.regression_passed ? 'PASSING' : 'UNVERIFIED'}
                    </td>
                    <td className="py-2 pr-3 text-slate-400 max-w-[190px] truncate">
                      {cfg.change_reason}
                    </td>
                    <td className="py-2">
                      {cfg.status !== 'ACTIVE' && cfg.regression_passed ? (
                        <button
                          type="button"
                          disabled={busyAction !== null || !canActivateConfigs}
                          title={canActivateConfigs ? 'Rollback config version' : 'Requires OWNER role'}
                          onClick={() =>
                            void runAction(`rb-${cfg.config_version_id}`, async () => {
                              const restored = await certaintyApi.rollbackConfig({
                                config_domain: cfg.config_domain,
                                target_version: cfg.version,
                                reason: `UI rollback to v${cfg.version}`,
                              });
                              return `Restored ${restored.config_domain} v${cfg.version} as active v${restored.version}.`;
                            })
                          }
                          className="px-2 py-1 text-[11px] font-medium bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed border border-slate-700 text-slate-200 rounded inline-flex items-center gap-1"
                        >
                          <RotateCcw className="w-3 h-3" />
                          Rollback
                        </button>
                      ) : (
                        <span className="font-mono text-[11px] text-slate-500">
                          {cfg.status === 'ACTIVE' ? 'CURRENT' : 'LOCKED'}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      {/* Stage 3E: Autonomous Operations & Intelligence Validation (Sections 9–14) */}
      <OperationsStage3EPanels
        data={data}
        busyAction={busyAction}
        permissions={permissions}
        runAction={runAction}
      />
    </div>
  );
}
