import { useState } from 'react';
import { Activity, AlertTriangle, PlayCircle, RefreshCw, ShieldCheck, Sliders, Users } from 'lucide-react';
import { certaintyApi } from '../api/client';
import type {
  CouncilDeliberationRecord,
  MarketIntelligenceOverview,
  UserPermissions,
} from '../types/certainty';
import {
  formatDurationSeconds,
  formatPct,
  formatProbPct,
  formatScoreMetric,
  formatTimestampShort,
  formatUsd,
  summarizeDriverLearningWeight,
} from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface MarketCouncilsViewProps {
  data: MarketIntelligenceOverview | null;
  loading: boolean;
  permissions?: UserPermissions;
  onRefresh: () => Promise<void>;
}

function CouncilPanel({ council }: { council: CouncilDeliberationRecord | null }) {
  if (!council) {
    return (
      <div className="border border-slate-800 bg-[#111827] rounded-xl p-5 text-xs text-slate-400">
        No deliberation recorded yet. Convene both AI Councils above to generate independent Crypto &amp; Stocks analyses.
      </div>
    );
  }

  const isCrypto = council.council_type === 'CRYPTO';
  return (
    <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <div>
          <div className="flex items-center gap-2">
            <Users className={`w-4 h-4 ${isCrypto ? 'text-amber-400' : 'text-sky-400'}`} />
            <h3 className="text-sm font-semibold text-slate-100">
              {isCrypto ? 'Crypto AI Council (BTC / ETH)' : 'Stocks AI Council (SPY / QQQ / IBIT)'}
            </h3>
            <ProvenanceLabel provenance={council.provenance} detail={council.horizon} />
          </div>
          <div className="mt-1 text-[11px] font-mono text-slate-500">
            {council.deliberation_id} · Snapshot {council.snapshot_id} · {formatTimestampShort(council.created_at)}
          </div>
        </div>
        <div className="text-right font-mono">
          <div
            className={`text-xs font-semibold ${
              council.directional_bias === 'BULLISH'
                ? 'text-emerald-400'
                : council.directional_bias === 'BEARISH'
                ? 'text-rose-400'
                : 'text-amber-300'
            }`}
          >
            {council.directional_bias} · {council.regime_verdict}
          </div>
          <div className="text-[11px] text-slate-400 tabular-nums">
            Move Prob: {formatProbPct(council.significant_move_prob)} · Up Bias:{' '}
            {formatProbPct(council.up_prob)} · Conf {Math.round(council.confidence * 100)}%
          </div>
        </div>
      </div>

      {/* WHY & WHEN Synthesis */}
      <div className="grid grid-cols-1 gap-2.5 text-xs">
        <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
          <div className="text-[11px] font-mono uppercase tracking-wider text-sky-400 mb-1">
            WHY Prices Are Fluctuating
          </div>
          <p className="text-slate-200 leading-relaxed">{council.why_explanation}</p>
        </div>
        <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800">
          <div className="text-[11px] font-mono uppercase tracking-wider text-amber-400 mb-1">
            WHEN Significant Movement Likely
          </div>
          <p className="text-slate-200 leading-relaxed">{council.when_explanation}</p>
        </div>
      </div>

      {/* 5 Council Members */}
      <div className="space-y-2">
        <div className="text-xs font-medium text-slate-300">
          Council Member Positions (5 Independent Roles)
        </div>
        <div className="space-y-2">
          {council.agent_opinions.map((op) => (
            <div
              key={op.agent_id}
              className="p-3 rounded-lg bg-[#0B111B] border border-slate-800/90 text-xs space-y-1"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono font-semibold text-slate-100">{op.role}</span>
                <div className="flex items-center gap-2 font-mono text-[11px]">
                  <span className="text-sky-400">{op.stance}</span>
                  <span className="text-slate-400 tabular-nums">
                    Move {formatProbPct(op.significant_move_prob)} · Up {formatProbPct(op.up_prob)}
                  </span>
                </div>
              </div>
              <p className="text-slate-300 leading-relaxed">{op.rationale}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Inter-Agent Challenge / Dissent */}
      <div className="space-y-2">
        <div className="text-xs font-medium text-slate-300">Inter-Agent Challenge &amp; Debate</div>
        <div className="space-y-2">
          {council.challenges.map((ch, idx) => (
            <div
              key={`${ch.challenger}-${idx}`}
              className="p-2.5 rounded-lg bg-amber-950/20 border border-amber-900/50 text-xs space-y-1"
            >
              <div className="font-mono text-[11px] text-amber-300">
                {ch.challenger} challenged {ch.target} — {ch.claim_challenged}
              </div>
              <div className="text-slate-300">{ch.challenge}</div>
              <div className="text-emerald-300 font-mono text-[11px]">Resolution: {ch.resolution}</div>
            </div>
          ))}
        </div>
      </div>

      {council.risk_warnings.length > 0 ? (
        <div className="p-3 rounded-lg bg-rose-950/25 border border-rose-900/60 text-xs text-rose-200 space-y-1">
          <div className="flex items-center gap-1.5 font-mono text-[11px] font-semibold text-rose-300">
            <AlertTriangle className="w-3.5 h-3.5" />
            Risk Sentinel Warnings (Paper-Only / Fail-Closed)
          </div>
          {council.risk_warnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

export function MarketCouncilsView({
  data,
  loading,
  permissions,
  onRefresh,
}: MarketCouncilsViewProps) {
  const canSubmit = permissions?.can_submit_research ?? true;
  const canResolve = permissions?.can_resolve_predictions ?? true;

  const [horizon, setHorizon] = useState<'4H' | '12H' | '24H' | '48H'>('24H');
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const runAction = async (key: string, fn: () => Promise<string>) => {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      const msg = await fn();
      setNotice(msg);
      await onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Market intelligence action failed');
    } finally {
      setBusy(null);
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

  const snap = data?.latest_snapshot ?? null;
  const btc = snap?.assets?.BTC;
  const eth = snap?.assets?.ETH;
  const spy = snap?.assets?.SPY;
  const qqq = snap?.assets?.QQQ;
  const ibit = snap?.assets?.IBIT;
  const okxBtc = snap?.derivatives?.BTC;
  const cal = data?.calibration_live_only;
  const scheduler = data?.scheduler_state;
  const driverWeights = data?.driver_weights ?? [];

  return (
    <div className="space-y-6">
      {/* Action Header Bar */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-4 flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-base font-semibold text-slate-100">
              Market Drivers &amp; Stocks / Crypto AI Councils
            </h1>
            <ProvenanceLabel
              provenance={snap?.provenance ?? 'UNAVAILABLE'}
              detail={snap ? `${snap.feed_health.live_count}/${snap.feed_health.total_count} Live Feeds` : 'Not Ingested'}
            />
          </div>
          <p className="mt-0.5 text-xs text-slate-400">
            Free public market feeds, cross-asset correlations, causal WHY/WHEN movement intelligence, separate Crypto &amp; Stocks AI Councils, and contamination-isolated Brier learning.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="Forecast horizon"
            value={horizon}
            onChange={(e) => setHorizon(e.target.value as '4H' | '12H' | '24H' | '48H')}
            className="px-2.5 py-1.5 text-xs font-mono bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
          >
            <option value="4H">4H Window</option>
            <option value="12H">12H Window</option>
            <option value="24H">24H Window</option>
            <option value="48H">48H Window</option>
          </select>

          <button
            type="button"
            disabled={busy !== null || !canSubmit}
            onClick={() =>
              void runAction('ingest', async () => {
                const res = await certaintyApi.ingestMarketDrivers();
                return `Ingested live snapshot ${res.snapshot_id} (${res.feed_health.live_count}/${res.feed_health.total_count} feeds LIVE, ${res.why_drivers.length} causal drivers).`;
              })
            }
            className="px-3 py-1.5 text-xs font-medium bg-slate-800 hover:bg-slate-700 disabled:opacity-50 border border-slate-700 text-slate-100 rounded-lg inline-flex items-center gap-1.5"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${busy === 'ingest' ? 'animate-spin' : ''}`} />
            Ingest Live Drivers
          </button>

          <button
            type="button"
            disabled={busy !== null || !canSubmit}
            onClick={() =>
              void runAction('convene', async () => {
                const res = await certaintyApi.conveneAiCouncils({
                  councils: ['CRYPTO', 'STOCKS'],
                  horizon,
                  refresh_drivers: true,
                  create_forecasts: true,
                });
                return `Convened Crypto & Stocks AI Councils (${horizon}) on ${res.snapshot_id}; opened ${res.created_forecasts.length} verifiable movement forecast(s).`;
              })
            }
            className="px-3.5 py-1.5 text-xs font-semibold bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white rounded-lg inline-flex items-center gap-1.5"
          >
            <Activity className="w-3.5 h-3.5" />
            Convene Both Councils
          </button>

          <button
            type="button"
            disabled={busy !== null || !canResolve}
            title={canResolve ? 'Auto-resolve matured LIVE forecasts' : 'Requires OPERATOR or OWNER role'}
            onClick={() =>
              void runAction('resolve-due', async () => {
                const res = await certaintyApi.resolveDueMovementForecasts(false);
                return `Checked due forecasts: resolved ${res.resolved_count} matured LIVE forecast(s).`;
              })
            }
            className="px-3 py-1.5 text-xs font-medium bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 text-white rounded-lg inline-flex items-center gap-1.5"
          >
            <ShieldCheck className="w-3.5 h-3.5" />
            Resolve Due
          </button>

          <button
            type="button"
            disabled={busy !== null || !canResolve || !canSubmit}
            title={
              canResolve
                ? 'Ingest live market feeds, resolve due LIVE forecasts, and replenish missing open councils'
                : 'Requires OPERATOR or OWNER role'
            }
            onClick={() =>
              void runAction('learning-cycle', async () => {
                const res = await certaintyApi.runMarketLearningCycle({
                  horizon,
                  force_convene: false,
                });
                if (res.skipped) {
                  return `Learning cycle skipped (${res.reason ?? 'GUARD'}).`;
                }
                return `Learning cycle completed on ${res.snapshot_id ?? 'snapshot'}: resolved ${res.resolved_count ?? 0} due forecast(s), convened ${(res.convened_councils ?? []).length} missing council(s).`;
              })
            }
            className="px-3 py-1.5 text-xs font-medium bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg inline-flex items-center gap-1.5"
          >
            <PlayCircle className="w-3.5 h-3.5" />
            Run Learning Cycle
          </button>
        </div>
      </section>

      {/* Autonomous Market-Driver & Learning Cadence Status Bar */}
      {scheduler ? (
        <section className="border border-slate-800/90 bg-[#0E1522] rounded-xl px-4 py-2.5 flex flex-wrap items-center justify-between gap-3 text-xs">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="font-mono uppercase tracking-wider text-[11px] text-sky-400">
              Autonomous Learning Cadence
            </span>
            <span className="px-2 py-0.5 rounded bg-slate-800 border border-slate-700 font-mono text-[11px] text-slate-200">
              {scheduler.last_status} · every {formatDurationSeconds(scheduler.cadence_seconds)}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-4 font-mono text-[11px] text-slate-400 tabular-nums">
            <span>Cycles: {scheduler.cycles_completed}</span>
            <span>Auto-Resolved: {scheduler.total_auto_resolved}</span>
            <span>Auto-Convened: {scheduler.total_auto_convened}</span>
            <span>Last Run: {scheduler.last_run_at ? formatTimestampShort(scheduler.last_run_at) : 'Pending first cadence'}</span>
          </div>
        </section>
      ) : null}

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

      {error ? (
        <div
          role="alert"
          className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200 flex items-center justify-between gap-4"
        >
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)} className="text-rose-300 hover:text-white">
            Dismiss
          </button>
        </div>
      ) : null}

      {/* Top 4 Market & Calibration KPIs */}
      <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">BTC / ETH Spot (Coinbase)</span>
            <ProvenanceLabel provenance={btc?.status === 'LIVE' ? 'LIVE' : 'UNAVAILABLE'} />
          </div>
          <div className="text-lg font-mono font-semibold tabular-nums text-slate-100">
            {formatUsd(btc?.price_usd, 0)} · {formatPct(btc?.change_24h_pct, 2, true)}
          </div>
          <div className="text-xs font-mono text-slate-400 tabular-nums">
            ETH {formatUsd(eth?.price_usd, 0)} ({formatPct(eth?.change_24h_pct, 2, true)}) · Vol{' '}
            {btc?.volume_surge_ratio ? `${btc.volume_surge_ratio.toFixed(2)}x` : '—'}
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Stocks &amp; Spot ETF (SPY/QQQ/IBIT)</span>
            <ProvenanceLabel provenance={spy?.status === 'LIVE' ? 'LIVE' : 'UNAVAILABLE'} />
          </div>
          <div className="text-lg font-mono font-semibold tabular-nums text-slate-100">
            SPY {formatUsd(spy?.price_usd, 2)} · QQQ {formatUsd(qqq?.price_usd, 2)}
          </div>
          <div className="text-xs font-mono text-slate-400 tabular-nums">
            IBIT {formatUsd(ibit?.price_usd, 2)} ({formatPct(ibit?.change_24h_pct, 2, true)}) · BTC↔QQQ r=
            {snap?.correlations?.BTC_QQQ?.correlation?.toFixed(2) ?? '—'}
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Leverage &amp; Sentiment</span>
            <ProvenanceLabel provenance={okxBtc?.status === 'LIVE' ? 'LIVE' : 'UNAVAILABLE'} />
          </div>
          <div className="text-lg font-mono font-semibold tabular-nums text-slate-100">
            F&amp;G: {snap?.fear_greed_value ?? '—'}/100 ({snap?.sentiment?.classification ?? '—'})
          </div>
          <div className="text-xs font-mono text-slate-400 tabular-nums">
            Funding {okxBtc?.funding_annualized_pct != null ? `${okxBtc.funding_annualized_pct}%` : '—'} ·
            L/S {okxBtc?.long_short_account_ratio ?? '—'}
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Clean LIVE Brier Calibration</span>
            <ProvenanceLabel provenance="LIVE" detail="Isolated" />
          </div>
          <div className="text-base font-mono font-semibold tabular-nums text-slate-100 flex flex-wrap items-baseline gap-x-2">
            <span className="whitespace-nowrap">Move {formatScoreMetric(cal?.mean_brier_score, 4)}</span>
            <span className="text-slate-600" aria-hidden="true">·</span>
            <span className="whitespace-nowrap">Dir {formatScoreMetric(cal?.mean_directional_brier_score, 4)}</span>
          </div>
          <div className="text-xs font-mono text-slate-400 tabular-nums">
            Dir Acc {cal?.directional_accuracy_pct != null ? `${cal.directional_accuracy_pct}%` : '—'} · Resolved:{' '}
            {cal?.resolved_count ?? 0} · Open: {cal?.open_count ?? 0} · Excluded:{' '}
            {cal?.excluded_non_live_or_unverified_count ?? 0}
          </div>
        </div>
      </section>

      {/* Causal WHY / WHEN & Catalyst Feed */}
      {snap ? (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
          <section className="xl:col-span-2 border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-slate-100">
                  Causal Price Fluctuation Drivers (WHY) &amp; Movement Window (WHEN)
                </h2>
                <p className="text-xs text-slate-400 mt-0.5">
                  Multi-factor attribution combining Coinbase spot/whale volume, OKX leverage, IBIT/FBTC ETF proxy, SPY/QQQ Pearson correlations, and Fed/CNBC catalysts.
                </p>
              </div>
              <span className="px-2.5 py-1 rounded bg-slate-800 text-xs font-mono text-amber-300">
                {snap.when_signals.movement_regime} ({snap.when_signals.expected_window})
              </span>
            </div>

            <div className="space-y-2.5">
              {snap.why_drivers.map((drv) => (
                <div
                  key={drv.driver_id}
                  className="p-3 rounded-lg bg-[#0E1522] border border-slate-800 text-xs space-y-1"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-slate-100">{drv.category}</span>
                    <span
                      className={`font-mono tabular-nums font-semibold ${
                        drv.impact_score > 0.1
                          ? 'text-emerald-400'
                          : drv.impact_score < -0.1
                          ? 'text-rose-400'
                          : 'text-slate-300'
                      }`}
                    >
                      Impact {drv.impact_score >= 0 ? '+' : ''}
                      {drv.impact_score.toFixed(2)} · Weight {drv.magnitude.toFixed(2)}
                    </span>
                  </div>
                  <p className="text-slate-300">{drv.explanation}</p>
                </div>
              ))}
            </div>

            <div className="p-3.5 rounded-lg bg-[#0B111B] border border-slate-800 text-xs space-y-1.5">
              <div className="font-mono text-[11px] uppercase tracking-wider text-amber-400">
                WHEN Triggers (Urgency {(snap.when_signals.movement_urgency_score * 100).toFixed(0)}%)
              </div>
              {snap.when_signals.trigger_reasons.map((r, idx) => (
                <div key={idx} className="text-slate-300">
                  • {r}
                </div>
              ))}
            </div>
          </section>

          <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-100">
                Fed, Macro &amp; Geopolitical News
              </h2>
              <ProvenanceLabel
                provenance="LIVE"
                detail={`${snap.macro_news.blocked_injection_count} Blocked`}
              />
            </div>
            <div className="text-xs font-mono text-slate-400">
              Fed: {snap.macro_news.fed_policy_count} · Macro: {snap.macro_news.macro_economic_count} ·
              Geopolitical: {snap.macro_news.geopolitical_risk_count}
            </div>
            <div className="space-y-2 max-h-[380px] overflow-y-auto pr-1">
              {snap.macro_news.top_headlines.slice(0, 8).map((item, i) => (
                <div
                  key={i}
                  className="p-2.5 rounded-lg bg-[#0E1522] border border-slate-800 text-xs space-y-1"
                >
                  <div className="flex items-center justify-between gap-2 font-mono text-[10px] text-slate-400">
                    <span>{item.source}</span>
                    <span>{item.categories.join(', ')}</span>
                  </div>
                  <div className="text-slate-200 font-medium leading-snug">{item.title}</div>
                </div>
              ))}
            </div>
          </section>
        </div>
      ) : (
        <div className="border border-slate-800 bg-[#111827] rounded-xl p-6 text-xs text-slate-300">
          No live market driver snapshot stored yet. Click <strong>Ingest Live Drivers</strong> or{' '}
          <strong>Convene Both Councils</strong> to fetch Coinbase, OKX, Yahoo Finance (SPY/QQQ/IBIT/FBTC),
          Fear &amp; Greed, Federal Reserve RSS, and CNBC News RSS.
        </div>
      )}

      {/* Separate Crypto & Stocks AI Councils */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <CouncilPanel council={data?.crypto_council ?? null} />
        <CouncilPanel council={data?.stocks_council ?? null} />
      </div>

      {/* Closed-Loop Adaptive Causal Driver Weights (Clean LIVE Only) */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="flex items-center gap-2">
              <Sliders className="w-4 h-4 text-sky-400" />
              <h2 className="text-sm font-semibold text-slate-100">
                Adaptive Causal Driver Weights (Clean LIVE Feedback Loop)
              </h2>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Each resolved clean LIVE forecast reinforces accurate causal drivers and dampens misleading signals within bounded [0.45x, 1.55x] limits. TEST and SIMULATED data never alter weights.
            </p>
          </div>
          <ProvenanceLabel provenance="LIVE" detail={`${driverWeights.length} Active Weight(s)`} />
        </div>

        {driverWeights.length === 0 ? (
          <div className="p-4 rounded-lg bg-[#0E1522] border border-slate-800 text-xs text-slate-400">
            All causal driver weights are at baseline (1.00x). Once at least one clean LIVE movement forecast resolves, driver hit/miss attribution will update these weights and calibrate future AI Council probabilities.
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {driverWeights.map((w) => {
              const summary = summarizeDriverLearningWeight(w.weight, w.sample_count, w.cumulative_skill);
              return (
                <div
                  key={w.weight_key}
                  className="p-3.5 rounded-lg bg-[#0E1522] border border-slate-800 space-y-2 text-xs"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono font-semibold text-slate-100">
                      {w.council_type} · {w.driver_id}
                    </span>
                    <span className={`px-2 py-0.5 rounded border font-mono text-[10px] ${summary.toneClass}`}>
                      {summary.label}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] text-slate-400 tabular-nums">
                    <span>
                      Hits/Misses: {w.hit_count}/{w.miss_count} ({w.sample_count} samples)
                    </span>
                    <span className={w.cumulative_skill >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                      Skill {w.cumulative_skill >= 0 ? '+' : ''}
                      {w.cumulative_skill.toFixed(2)}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* Verifiable Movement Forecast Tracker */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">
              Movement Forecasts &amp; Brier Accuracy Ledger
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Unverified, TEST, and SIMULATED forecasts are strictly quarantined from live calibration and learning priors.
            </p>
          </div>
          <ProvenanceLabel provenance="LIVE" detail="Outcome Feedback Loop" />
        </div>

        <div className="overflow-x-auto max-h-80 overflow-y-auto">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-800 text-slate-400">
              <tr>
                <th className="py-2 pr-3 font-medium">Forecast</th>
                <th className="py-2 pr-3 font-medium">Council / Target</th>
                <th className="py-2 pr-3 font-medium">Move Prob / Threshold</th>
                <th className="py-2 pr-3 font-medium">Baseline → Actual</th>
                <th className="py-2 pr-3 font-medium">Status / Brier</th>
                <th className="py-2 font-medium">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/80">
              {(data?.forecasts ?? []).length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-4 text-slate-500">
                    No movement forecasts recorded yet.
                  </td>
                </tr>
              ) : (
                (data?.forecasts ?? []).slice(0, 20).map((f) => (
                  <tr key={f.forecast_id}>
                    <td className="py-2 pr-3 font-mono text-[11px]">
                      <div className="text-slate-200">{f.forecast_id}</div>
                      <div className="text-slate-500">
                        {f.provenance} · {f.verified_clean ? 'CLEAN' : 'QUARANTINED'}
                      </div>
                    </td>
                    <td className="py-2 pr-3 font-mono text-slate-200">
                      {f.council_type} · {f.symbol} ({f.horizon})
                    </td>
                    <td className="py-2 pr-3 font-mono tabular-nums text-slate-200">
                      {formatProbPct(f.predicted_move_prob)} (|r|≥{f.move_threshold_pct}%)
                    </td>
                    <td className="py-2 pr-3 font-mono tabular-nums text-slate-300">
                      {formatUsd(f.baseline_price, 2)} →{' '}
                      {f.actual_price != null ? formatUsd(f.actual_price, 2) : 'Pending'}
                      {f.actual_return_pct != null ? ` (${formatPct(f.actual_return_pct, 2, true)})` : ''}
                    </td>
                    <td className="py-2 pr-3 font-mono text-[11px]">
                      <div>
                        <span
                          className={
                            f.status === 'RESOLVED'
                              ? 'text-emerald-400'
                              : f.status === 'VOIDED'
                              ? 'text-slate-400'
                              : 'text-amber-300'
                          }
                        >
                          {f.status}
                        </span>
                        {f.brier_score != null ? ` · Move ${f.brier_score.toFixed(4)}` : ''}
                        {f.directional_brier_score != null
                          ? ` · Dir ${f.directional_brier_score.toFixed(4)} (${f.directional_hit ? 'HIT' : 'MISS'})`
                          : ''}
                      </div>
                      {(f.learning_cycle_id || (f.driver_signals?.length ?? 0) > 0) ? (
                        <div className="text-slate-500">
                          {f.learning_cycle_id ? `${f.learning_cycle_id} · ` : ''}
                          {(f.driver_signals?.length ?? 0)} driver(s) attributed
                        </div>
                      ) : null}
                    </td>
                    <td className="py-2">
                      {f.status === 'OPEN' ? (
                        <div className="flex items-center gap-1.5">
                          <button
                            type="button"
                            disabled={busy !== null || !canResolve}
                            onClick={() =>
                              void runAction(`void-${f.forecast_id}`, async () => {
                                await certaintyApi.resolveMovementForecast(f.forecast_id, {
                                  void_reason: 'Operator voided from calibration ledger',
                                });
                                return `Voided ${f.forecast_id} (excluded from Brier metrics).`;
                              })
                            }
                            className="px-2 py-0.5 text-[11px] font-mono bg-slate-800 hover:bg-slate-700 disabled:opacity-40 text-slate-200 rounded"
                          >
                            VOID
                          </button>
                        </div>
                      ) : (
                        <span className="font-mono text-[11px] text-slate-500">Finalized</span>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
