import { useCallback, useEffect, useState } from 'react';
import { certaintyApi } from '../api/client';
import type { ArenaEvaluationReport, DatasetSplit } from '../types/certainty';
import { formatScoreMetric, formatTimestampShort } from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface ArenaOutOfSamplePanelProps {
  refreshSignal?: number;
}

export function ArenaOutOfSamplePanel({ refreshSignal = 0 }: ArenaOutOfSamplePanelProps) {
  const [splitFilter, setSplitFilter] = useState<'ALL' | DatasetSplit>('ALL');
  const [minSamples, setMinSamples] = useState<number>(10);
  const [report, setReport] = useState<ArenaEvaluationReport | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadReport = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await certaintyApi.getArenaEvaluation({
        dataset_split: splitFilter === 'ALL' ? undefined : splitFilter,
        min_resolved_samples: minSamples,
      });
      setReport(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load Arena evaluation report');
    } finally {
      setLoading(false);
    }
  }, [splitFilter, minSamples]);

  useEffect(() => {
    void loadReport();
  }, [loadReport, refreshSignal]);

  const snapshots = report?.snapshots ?? [];
  const comparisons = report?.same_snapshot_comparisons ?? [];
  const bySplit = report?.by_split ?? [];
  const byCategory = report?.by_task_category ?? [];

  return (
    <section
      data-testid="arena-oos-evaluation-panel"
      className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h2 className="text-sm font-semibold text-slate-100">
              Out-of-Sample Arena Evaluation &amp; Dataset Split Integrity
            </h2>
            <ProvenanceLabel
              provenance={report ? report.provenance : 'UNAVAILABLE'}
              detail="TEST Split Quarantined · Routing Score Excluded"
            />
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Compares AI Orchestra Arena models strictly on identical dataset snapshots, benchmarks, and timeframes.
            TEST-split outcomes never tune prompts, routing, or strategy weights.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <label className="flex items-center gap-1.5 text-slate-400">
            <span>Split</span>
            <select
              aria-label="Filter evaluation by dataset split"
              value={splitFilter}
              onChange={(e) => setSplitFilter(e.target.value as 'ALL' | DatasetSplit)}
              className="px-2 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            >
              <option value="ALL">ALL SPLITS</option>
              <option value="TRAIN">TRAIN</option>
              <option value="VALIDATION">VALIDATION</option>
              <option value="TEST">TEST (HELD-OUT)</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-slate-400">
            <span>Min N</span>
            <select
              aria-label="Minimum matched resolved samples"
              value={minSamples}
              onChange={(e) => setMinSamples(Number(e.target.value))}
              className="px-2 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            >
              <option value={10}>10 (Default)</option>
              <option value={5}>5</option>
              <option value={3}>3</option>
              <option value={1}>1 (Inspect)</option>
            </select>
          </label>
          <button
            type="button"
            onClick={() => void loadReport()}
            disabled={loading}
            className="px-3 py-1.5 font-medium text-slate-200 bg-slate-800 hover:bg-slate-700 disabled:opacity-50 border border-slate-700 rounded-lg whitespace-nowrap"
          >
            {loading ? 'Refreshing...' : 'Refresh Evaluation'}
          </button>
        </div>
      </div>

      {error ? (
        <div role="alert" className="p-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200">
          {error}
        </div>
      ) : null}

      {/* Partition Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
        {(['TRAIN', 'VALIDATION', 'TEST'] as DatasetSplit[]).map((sp) => {
          const row = bySplit.find((b) => b.dataset_split === sp);
          const snapCount = snapshots.filter((s) => s.split === sp).length;
          return (
            <div
              key={sp}
              className="p-3.5 rounded-lg bg-[#0E1522] border border-slate-800 space-y-1.5"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono font-semibold text-slate-100">{sp}</span>
                <span
                  className={`font-mono text-[11px] font-semibold ${
                    sp === 'TEST' ? 'text-amber-300' : 'text-emerald-400'
                  }`}
                >
                  {sp === 'TEST' ? 'QUARANTINED (NO TUNING)' : 'TUNING PERMITTED'}
                </span>
              </div>
              <div className="grid grid-cols-3 gap-2 pt-1 font-mono tabular-nums text-[11px]">
                <div>
                  <div className="text-slate-500">Snapshots</div>
                  <div className="text-slate-200">{snapCount}</div>
                </div>
                <div>
                  <div className="text-slate-500">Resolved</div>
                  <div className="text-slate-200">
                    {row?.resolved_count ?? 0}{' '}
                    <span className="text-slate-500">({row?.open_count ?? 0} open)</span>
                  </div>
                </div>
                <div>
                  <div className="text-slate-500">Brier / ECE</div>
                  <div className="text-slate-200">
                    {formatScoreMetric(row?.mean_brier_score ?? null, 4)} /{' '}
                    {formatScoreMetric(row?.expected_calibration_error ?? null, 3)}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Identical-Snapshot Head-to-Head Comparisons */}
      <div className="space-y-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="font-semibold text-slate-200">
            Identical-Snapshot Model Comparisons ({comparisons.length})
          </span>
          <span className="font-mono text-[11px] text-slate-400">
            Verified Forecast Rank (Brier -&gt; Log Loss) · Arena Routing Score Shown Separately
          </span>
        </div>

        {comparisons.length === 0 ? (
          <div className="p-4 rounded-lg bg-[#0E1522] border border-slate-800 text-xs text-slate-500">
            No resolved Arena forecasts yet for this filter. Models are only compared once verified settlements resolve on identical dataset snapshots.
          </div>
        ) : (
          <div className="space-y-3">
            {comparisons.slice(0, 6).map((cohort) => (
              <div
                key={`${cohort.dataset_snapshot_id}-${cohort.benchmark_version}-${cohort.task_category}-${cohort.timeframe}-${cohort.dataset_split}`}
                className="rounded-lg bg-[#0E1522] border border-slate-800 overflow-hidden"
              >
                <div className="px-3.5 py-2.5 border-b border-slate-800 flex flex-wrap items-center justify-between gap-2 text-xs">
                  <div className="font-mono text-slate-200">
                    <span className="font-semibold text-sky-300">{cohort.dataset_snapshot_id || 'UNREGISTERED'}</span>
                    {' · '}
                    <span>{cohort.dataset_split}</span>
                    {' · '}
                    <span className="text-slate-400">{cohort.task_category}</span>
                    {' · '}
                    <span className="text-slate-400">{cohort.benchmark_version}</span>
                  </div>
                  <span
                    className={`font-mono text-[11px] font-semibold ${
                      cohort.comparison_eligible ? 'text-emerald-400' : 'text-amber-400'
                    }`}
                  >
                    {cohort.comparison_status}
                  </span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="border-b border-slate-800/80 text-slate-400">
                      <tr>
                        <th className="py-1.5 px-3 font-medium">Verified Rank</th>
                        <th className="py-1.5 px-3 font-medium">Model</th>
                        <th className="py-1.5 px-3 font-medium text-right">Matched N</th>
                        <th className="py-1.5 px-3 font-medium text-right">Brier</th>
                        <th className="py-1.5 px-3 font-medium text-right">Log Loss</th>
                        <th className="py-1.5 px-3 font-medium text-right">ECE</th>
                        <th className="py-1.5 px-3 font-medium text-right">Accuracy</th>
                        <th className="py-1.5 px-3 font-medium text-right">Arena Routing (Diagnostic)</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/70 font-mono tabular-nums">
                      {cohort.models.map((m) => (
                        <tr key={m.model_key}>
                          <td className="py-1.5 px-3 text-slate-200">
                            {m.rank !== null ? `#${m.rank}` : 'UNRANKED'}
                          </td>
                          <td className="py-1.5 px-3 text-slate-100">{m.model_key}</td>
                          <td className="py-1.5 px-3 text-right text-slate-300">
                            {m.verified_forecast_metrics.resolved_count}
                          </td>
                          <td className="py-1.5 px-3 text-right text-emerald-300">
                            {formatScoreMetric(m.verified_forecast_metrics.mean_brier_score, 4)}
                          </td>
                          <td className="py-1.5 px-3 text-right text-slate-200">
                            {formatScoreMetric(m.verified_forecast_metrics.mean_log_loss, 4)}
                          </td>
                          <td className="py-1.5 px-3 text-right text-slate-300">
                            {formatScoreMetric(m.verified_forecast_metrics.expected_calibration_error, 4)}
                          </td>
                          <td className="py-1.5 px-3 text-right text-slate-300">
                            {m.verified_forecast_metrics.accuracy_pct !== null
                              ? `${m.verified_forecast_metrics.accuracy_pct.toFixed(1)}%`
                              : '—'}
                          </td>
                          <td className="py-1.5 px-3 text-right text-slate-400">
                            {m.arena_routing_metadata.avg_arena_routing_score !== null
                              ? `${m.arena_routing_metadata.avg_arena_routing_score.toFixed(3)} (excl.)`
                              : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Registered Dataset Snapshots & Task-Category Breakdown */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 text-xs">
        <div className="rounded-lg bg-[#0E1522] border border-slate-800 p-3.5 space-y-2">
          <div className="font-semibold text-slate-200">
            Registered Dataset Snapshots ({snapshots.length})
          </div>
          {snapshots.length === 0 ? (
            <div className="text-slate-500">
              No dataset snapshots registered yet. Synced Arena benchmark runs register immutable SHA-256 partitions here.
            </div>
          ) : (
            <div className="overflow-x-auto max-h-44 overflow-y-auto">
              <table className="w-full text-left text-[11px] font-mono">
                <thead className="border-b border-slate-800 text-slate-400">
                  <tr>
                    <th className="py-1 pr-2">Snapshot</th>
                    <th className="py-1 pr-2">Split</th>
                    <th className="py-1 pr-2">Window End</th>
                    <th className="py-1">Hash</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/70">
                  {snapshots.slice(0, 10).map((s) => (
                    <tr key={s.dataset_snapshot_id}>
                      <td className="py-1.5 pr-2 text-slate-100">
                        <div>{s.dataset_snapshot_id}</div>
                        <div className="text-slate-500">{s.dataset_version}</div>
                      </td>
                      <td
                        className={`py-1.5 pr-2 font-semibold ${
                          s.split === 'TEST' ? 'text-amber-300' : 'text-sky-300'
                        }`}
                      >
                        {s.split}
                      </td>
                      <td className="py-1.5 pr-2 text-slate-300 whitespace-nowrap">
                        {formatTimestampShort(s.window_end)}
                      </td>
                      <td className="py-1.5 text-slate-400 truncate max-w-[120px]" title={s.snapshot_hash}>
                        {s.snapshot_hash.slice(0, 18)}...
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="rounded-lg bg-[#0E1522] border border-slate-800 p-3.5 space-y-2">
          <div className="font-semibold text-slate-200">
            Verified Performance by Task Category ({byCategory.length})
          </div>
          {byCategory.length === 0 ? (
            <div className="text-slate-500">
              No resolved task-category aggregates yet.
            </div>
          ) : (
            <div className="overflow-x-auto max-h-44 overflow-y-auto">
              <table className="w-full text-left text-[11px] font-mono tabular-nums">
                <thead className="border-b border-slate-800 text-slate-400">
                  <tr>
                    <th className="py-1 pr-2">Task Category</th>
                    <th className="py-1 pr-2 text-right">N</th>
                    <th className="py-1 pr-2 text-right">Brier</th>
                    <th className="py-1 pr-2 text-right">Log Loss</th>
                    <th className="py-1 text-right">ECE</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/70">
                  {byCategory.map((c) => (
                    <tr key={c.task_category}>
                      <td className="py-1.5 pr-2 text-slate-100">{c.task_category}</td>
                      <td className="py-1.5 pr-2 text-right text-slate-300">{c.resolved_count}</td>
                      <td className="py-1.5 pr-2 text-right text-emerald-300">
                        {formatScoreMetric(c.mean_brier_score, 4)}
                      </td>
                      <td className="py-1.5 pr-2 text-right text-slate-200">
                        {formatScoreMetric(c.mean_log_loss, 4)}
                      </td>
                      <td className="py-1.5 text-right text-slate-300">
                        {formatScoreMetric(c.expected_calibration_error, 4)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
