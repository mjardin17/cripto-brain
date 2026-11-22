import { useState } from 'react';
import { Activity, RefreshCw, Search, ShieldAlert, TrendingUp } from 'lucide-react';
import { certaintyApi } from '../api/client';
import type {
  ModelProviderRecord,
  SolanaCandidateEvidence,
  SolanaOverviewResponse,
  UserPermissions,
} from '../types/certainty';
import {
  formatSolanaTokenPrice,
  formatTimestampShort,
  formatUsd,
  summarizeSolanaCandidateStatus,
} from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface SolanaDeskViewProps {
  data: SolanaOverviewResponse | null;
  loading: boolean;
  providers?: ModelProviderRecord[];
  permissions?: UserPermissions;
  onRefresh: () => Promise<void>;
}

function renderBoolCheck(label: string, val: boolean | null | undefined) {
  if (val === true) {
    return (
      <span className="font-mono text-[11px] text-emerald-400">
        {label}:OK
      </span>
    );
  }
  if (val === false) {
    return (
      <span className="font-mono text-[11px] text-rose-400">
        {label}:FAIL
      </span>
    );
  }
  return (
    <span className="font-mono text-[11px] text-amber-400">
      {label}:MISSING
    </span>
  );
}

function renderPctCheck(label: string, val: number | null | undefined, maxAllowed: number) {
  if (val === null || val === undefined || !Number.isFinite(val)) {
    return (
      <span className="font-mono text-[11px] text-amber-400">
        {label}:MISSING
      </span>
    );
  }
  const ok = val <= maxAllowed;
  return (
    <span className={`font-mono text-[11px] ${ok ? 'text-emerald-400' : 'text-rose-400'}`}>
      {label}:{val.toFixed(1)}%
    </span>
  );
}

function renderMinPctCheck(label: string, val: number | null | undefined, minRequired: number) {
  if (val === null || val === undefined || !Number.isFinite(val)) {
    return (
      <span className="font-mono text-[11px] text-amber-400">
        {label}:MISSING
      </span>
    );
  }
  const ok = val >= minRequired;
  return (
    <span className={`font-mono text-[11px] ${ok ? 'text-emerald-400' : 'text-rose-400'}`}>
      {label}:{val.toFixed(1)}%
    </span>
  );
}

function summarizeEvidenceFlags(
  evidence: SolanaCandidateEvidence,
  maxTop10Pct: number,
  minLpLockedPct: number
) {
  return (
    <div className="flex flex-wrap gap-x-2.5 gap-y-0.5">
      {renderBoolCheck('Mint', evidence.mint_authority_revoked)}
      {renderBoolCheck('Freeze', evidence.freeze_authority_revoked)}
      {renderMinPctCheck('LP', evidence.lp_locked_pct, minLpLockedPct)}
      {renderPctCheck('Top10', evidence.top10_holders_pct, maxTop10Pct)}
      {renderBoolCheck('Dev', evidence.dev_wallet_clear)}
      {renderBoolCheck('Sell', evidence.sell_simulation_passed)}
    </div>
  );
}

export function SolanaDeskView({
  data,
  loading,
  providers = [],
  permissions,
  onRefresh,
}: SolanaDeskViewProps) {
  const canResearch = permissions?.can_submit_research ?? true;
  const canPaperTrade = permissions?.can_execute_paper_trades ?? true;

  const [discoveryQuery, setDiscoveryQuery] = useState('SOL');
  const [mintInput, setMintInput] = useState('');
  const [symbolInput, setSymbolInput] = useState('');
  const [analystMode, setAnalystMode] = useState<'ai' | 'deterministic'>('ai');
  const [preferredProvider, setPreferredProvider] = useState<string>('');
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const runAction = async (key: string, fn: () => Promise<string>) => {
    setBusyAction(key);
    setActionError(null);
    setNotice(null);
    try {
      const msg = await fn();
      setNotice(msg);
      await onRefresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Solana desk action failed');
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
        <div>Solana Memecoin Research &amp; Paper Desk state unavailable.</div>
        <button
          type="button"
          onClick={() => void onRefresh()}
          className="px-3.5 py-2 text-xs font-semibold bg-sky-600 hover:bg-sky-500 text-white rounded-lg"
        >
          Load Solana Desk State
        </button>
      </div>
    );
  }

  const { ledger, config, candidates, positions, events, kill_switch_active } = data;
  const openPositions = positions.filter((p) => p.status === 'OPEN');
  const closedPositions = positions.filter((p) => p.status === 'CLOSED');
  const aiProviderChoices = providers.filter((p) => p.provider_id !== 'local_deterministic');

  return (
    <div className="space-y-6">
      {/* Header Banner */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 flex flex-wrap items-center justify-between gap-4">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-base font-semibold text-slate-100">
              Solana Memecoin Research &amp; Simulated Paper Desk
            </h1>
            <ProvenanceLabel provenance="SIMULATED" detail="Paper Ledger Only" />
            <span
              className={`text-xs font-mono font-semibold ${
                kill_switch_active ? 'text-rose-400' : 'text-emerald-400'
              }`}
            >
              {kill_switch_active ? 'HALTED BY KILL SWITCH' : 'FAIL-CLOSED ACTIVE'}
            </span>
          </div>
          <p className="text-xs text-slate-400 max-w-3xl">
            Six-agent pipeline (SCOUT → FILTER → ANALYST → EXECUTOR → RISK → LOGGER) collecting read-only DexScreener, GeckoTerminal, Solana RPC, RugCheck, and Jupiter quote evidence. Missing data always stays unverified and fails closed. Live trading and wallet signing are permanently disabled.
          </p>
        </div>
        <div className="text-left sm:text-right font-mono text-xs text-slate-400 space-y-1">
          <div>
            Max Size: {((config.max_position_fraction ?? 0.08) * 100).toFixed(0)}% (
            {formatUsd(ledger.max_next_notional_usd)})
          </div>
          <div>
            Stops: -{((config.hard_stop_fraction ?? 0.06) * 100).toFixed(0)}% Hard · +
            {((config.trailing_activation_fraction ?? 0.12) * 100).toFixed(0)}% Arm / -
            {((config.trailing_drawdown_fraction ?? 0.06) * 100).toFixed(0)}% Trail
          </div>
        </div>
      </section>

      {notice ? (
        <div
          role="status"
          className="px-4 py-3 rounded-lg border border-sky-800/70 bg-sky-950/30 text-xs text-sky-200 flex items-start sm:items-center justify-between gap-3"
        >
          <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="shrink-0 text-sky-300 hover:text-white">
            Dismiss
          </button>
        </div>
      ) : null}

      {actionError ? (
        <div
          role="alert"
          className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200 flex items-start sm:items-center justify-between gap-3"
        >
          <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{actionError}</span>
          <button type="button" onClick={() => setActionError(null)} className="shrink-0 text-rose-300 hover:text-white">
            Dismiss
          </button>
        </div>
      ) : null}

      {/* Ledger KPI Strip */}
      <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Simulated Paper Equity</span>
            <ProvenanceLabel provenance="SIMULATED" />
          </div>
          <div className="text-2xl font-mono font-semibold tabular-nums text-slate-50">
            {formatUsd(ledger.total_equity_usd)}
          </div>
          <div className="text-xs font-mono text-slate-400">
            Cash {formatUsd(ledger.cash_balance_usd)} · Open {formatUsd(ledger.open_positions_value_usd)}
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Simulated Realized &amp; Unrealized PnL</span>
            <ProvenanceLabel provenance="SIMULATED" />
          </div>
          <div
            className={`text-2xl font-mono font-semibold tabular-nums ${
              ledger.realized_pnl_usd + ledger.unrealized_pnl_usd < 0
                ? 'text-rose-400'
                : ledger.realized_pnl_usd + ledger.unrealized_pnl_usd > 0
                ? 'text-emerald-400'
                : 'text-slate-100'
            }`}
          >
            {formatUsd(ledger.realized_pnl_usd + ledger.unrealized_pnl_usd)}
          </div>
          <div className="text-xs font-mono text-slate-400">
            Realized {formatUsd(ledger.realized_pnl_usd)} · Open {formatUsd(ledger.unrealized_pnl_usd)}
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Simulated Positions &amp; Win Rate</span>
            <ProvenanceLabel provenance="SIMULATED" />
          </div>
          <div className="text-2xl font-mono font-semibold tabular-nums text-slate-100">
            {ledger.open_count} Open · {ledger.closed_count} Closed
          </div>
          <div className="text-xs font-mono text-slate-400">
            Win Rate {ledger.win_rate_pct.toFixed(1)}% · Avg {ledger.avg_realized_pnl_pct.toFixed(2)}%
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-4 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">Deterministic Exit Ledger</span>
            <ProvenanceLabel provenance="LIVE" detail="DuckDB" />
          </div>
          <div className="text-2xl font-mono font-semibold tabular-nums text-slate-100">
            {ledger.trailing_stop_exits} Trail · {ledger.hard_stop_exits} Hard
          </div>
          <div className="text-xs font-mono text-slate-400">
            Start {formatUsd(ledger.starting_balance_usd)} · Cap {formatUsd(ledger.max_next_notional_usd)}
          </div>
        </div>
      </section>

      {/* Discovery & Mint Evaluation Controls */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100 flex items-center gap-2">
                <Search className="w-4 h-4 text-sky-400" />
                1. SCOUT — Public Pool Discovery (DexScreener)
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Queries read-only public Solana liquidity pools and records raw pair evidence in DuckDB.
              </p>
            </div>
            <ProvenanceLabel provenance="LIVE" detail="Read-Only" />
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              void runAction('sol-discover', async () => {
                const res = await certaintyApi.discoverSolanaPools({
                  query: discoveryQuery.trim() || 'SOL',
                  limit: 12,
                });
                return `Discovered ${res.count} live Solana pool candidates for '${discoveryQuery.trim() || 'SOL'}'.`;
              });
            }}
            className="flex flex-col sm:flex-row gap-2.5 text-xs"
          >
            <input
              aria-label="Solana pool discovery query"
              type="text"
              value={discoveryQuery}
              onChange={(e) => setDiscoveryQuery(e.target.value)}
              placeholder="Query (e.g., SOL, BONK, WIF)"
              className="flex-1 px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            />
            <button
              type="submit"
              disabled={busyAction !== null || !canResearch}
              title={canResearch ? 'Discover live Solana pools' : 'Requires ANALYST role or higher'}
              className="px-4 py-2 font-semibold bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg whitespace-nowrap"
            >
              {busyAction === 'sol-discover' ? 'Discovering...' : 'Discover Live Pools'}
            </button>
          </form>
        </section>

        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100 flex items-center gap-2">
                <ShieldAlert className="w-4 h-4 text-amber-400" />
                2. FILTER + ANALYST — Fail-Closed Mint Pipeline
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Collects Mint/Freeze/Holder RPC, RugCheck, and Jupiter quote checks. Analysts can never override safety failures.
              </p>
            </div>
            <ProvenanceLabel provenance="LIVE" detail="Fail-Closed" />
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              const trimmedMint = mintInput.trim();
              if (!trimmedMint) return;
              void runAction('sol-eval', async () => {
                const res = await certaintyApi.evaluateSolanaMint({
                  mint: trimmedMint,
                  symbol: symbolInput.trim() || null,
                  analyst_mode: analystMode,
                  preferred_provider: preferredProvider || null,
                });
                const reasonStr =
                  res.reasons && res.reasons.length > 0 ? ` (${res.reasons.join(', ')})` : '';
                return `Mint ${trimmedMint} -> ${res.status}${reasonStr}${
                  res.score !== undefined ? ` · score=${res.score.toFixed(2)} (${res.analyst_provider})` : ''
                }`;
              });
            }}
            className="grid grid-cols-1 sm:grid-cols-12 gap-2.5 text-xs"
          >
            <input
              aria-label="Solana token mint address"
              type="text"
              required
              value={mintInput}
              onChange={(e) => setMintInput(e.target.value)}
              placeholder="Solana Mint Address"
              className="sm:col-span-8 px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            />
            <input
              aria-label="Token symbol (optional)"
              type="text"
              value={symbolInput}
              onChange={(e) => setSymbolInput(e.target.value)}
              placeholder="Symbol"
              className="sm:col-span-4 px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            />
            <select
              aria-label="Analyst scoring mode"
              value={analystMode}
              onChange={(e) => setAnalystMode(e.target.value as 'ai' | 'deterministic')}
              className="sm:col-span-6 px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
            >
              <option value="ai">AI Router</option>
              <option value="deterministic">Deterministic</option>
            </select>
            <select
              aria-label="Preferred AI provider"
              value={preferredProvider}
              onChange={(e) => setPreferredProvider(e.target.value)}
              disabled={analystMode === 'deterministic'}
              className="sm:col-span-6 px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 disabled:opacity-50"
            >
              <option value="">Auto Fallback</option>
              {aiProviderChoices.map((p) => (
                <option key={p.provider_id} value={p.provider_id}>
                  {p.provider_id} ({p.status})
                </option>
              ))}
            </select>
            <div className="sm:col-span-12 flex justify-end">
              <button
                type="submit"
                disabled={busyAction !== null || !canResearch || kill_switch_active}
                title={
                  kill_switch_active
                    ? 'Blocked while Kill Switch is active'
                    : canResearch
                    ? 'Run full SCOUT -> FILTER -> ANALYST pipeline'
                    : 'Requires ANALYST role or higher'
                }
                className="px-4 py-2 font-semibold bg-amber-600 hover:bg-amber-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg whitespace-nowrap"
              >
                {busyAction === 'sol-eval' ? 'Evaluating On-Chain Evidence...' : 'Evaluate Mint (Fail-Closed)'}
              </button>
            </div>
          </form>
        </section>
      </div>

      {/* Evaluated & Discovered Candidates Table */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">
              3. Token Candidates &amp; Fail-Closed On-Chain Safety Checks ({candidates.length})
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Min LP Lock {(config.min_lp_locked_pct ?? 95).toFixed(0)}% · Min 1h Vol{' '}
              {formatUsd(config.min_volume_1h_usd ?? 50000, 0)} · Top10 ≤{' '}
              {(config.max_top10_holders_pct ?? 20).toFixed(0)}% · Dev Clear · Score ≥{' '}
              {(config.min_analyst_score ?? 0.6).toFixed(2)}
            </p>
          </div>
          <ProvenanceLabel provenance="LIVE" detail="DexScreener + RPC + RugCheck" />
        </div>

        <div className="overflow-x-auto max-h-[380px] overflow-y-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead className="sticky top-0 z-10 border-b border-slate-800 bg-[#0E1522] text-slate-400">
              <tr>
                <th className="py-2.5 px-4 font-medium">Token / Mint</th>
                <th className="py-2.5 px-3 font-medium text-right">Price / Liq / 1h Vol</th>
                <th className="py-2.5 px-3 font-medium">Fail-Closed Evidence</th>
                <th className="py-2.5 px-3 font-medium">Analyst</th>
                <th className="py-2.5 px-3 font-medium">Status</th>
                <th className="py-2.5 px-4 font-medium text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/80">
              {candidates.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-6 px-4 text-center text-slate-500">
                    No Solana candidates recorded yet. Discover live pools or evaluate a mint above.
                  </td>
                </tr>
              ) : (
                candidates.map((cand) => {
                  const st = summarizeSolanaCandidateStatus(cand.status);
                  const canOpen =
                    cand.status === 'PAPER_QUOTE_REQUIRED' && canPaperTrade && !kill_switch_active;
                  return (
                    <tr key={cand.mint} className="hover:bg-slate-800/30 align-top">
                      <td className="py-2.5 px-4">
                        <div className="font-semibold text-slate-100">
                          {cand.symbol}{' '}
                          <span className="font-mono text-[11px] text-slate-500">
                            {cand.dex_id || cand.source}
                          </span>
                        </div>
                        <button
                          type="button"
                          onClick={() => {
                            setMintInput(cand.mint);
                            setSymbolInput(cand.symbol);
                          }}
                          title="Load mint into evaluator"
                          className="font-mono text-[11px] text-sky-400 hover:text-sky-300 mt-0.5 text-left"
                        >
                          {cand.mint}
                        </button>
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200 whitespace-nowrap">
                        <div>{formatSolanaTokenPrice(cand.price_usd)}</div>
                        <div className="text-[11px] text-slate-400">
                          L {cand.liquidity_usd !== null ? formatUsd(cand.liquidity_usd, 0) : '—'} · V{' '}
                          {cand.volume_1h_usd !== null ? formatUsd(cand.volume_1h_usd, 0) : '—'}
                        </div>
                      </td>
                      <td className="py-2.5 px-3 min-w-[230px]">
                        {summarizeEvidenceFlags(
                          cand.evidence || ({} as SolanaCandidateEvidence),
                          config.max_top10_holders_pct ?? 20,
                          config.min_lp_locked_pct ?? 95
                        )}
                        {cand.rejection_reasons.length > 0 ? (
                          <div className="mt-1 font-mono text-[11px] text-rose-400">
                            {cand.rejection_reasons.join(', ')}
                          </div>
                        ) : null}
                      </td>
                      <td className="py-2.5 px-3 max-w-[220px]">
                        <div className="font-mono text-slate-200">
                          {cand.analyst_score !== null ? cand.analyst_score.toFixed(2) : '—'}{' '}
                          <span className="text-[11px] text-slate-500">
                            {cand.analyst_provider || ''}
                          </span>
                        </div>
                        <div className="text-[11px] text-slate-400 line-clamp-2">
                          {cand.analyst_rationale || 'Pending evaluation'}
                        </div>
                      </td>
                      <td className={`py-2.5 px-3 font-mono font-semibold whitespace-nowrap ${st.toneClass}`}>
                        {st.label}
                      </td>
                      <td className="py-2.5 px-4 text-right whitespace-nowrap">
                        {cand.status === 'PAPER_QUOTE_REQUIRED' ? (
                          <button
                            type="button"
                            disabled={busyAction !== null || !canOpen}
                            title={
                              kill_switch_active
                                ? 'Blocked by Kill Switch'
                                : canPaperTrade
                                ? 'Open simulated paper position (max 8% cap)'
                                : 'Requires OPERATOR or OWNER role'
                            }
                            onClick={() =>
                              void runAction(`open-${cand.mint}`, async () => {
                                const opened = await certaintyApi.openSolanaPaperPosition({
                                  mint: cand.mint,
                                });
                                return `Opened SIMULATED paper position ${opened.position.position_id} for ${opened.position.symbol} ($${opened.position.notional_usd.toFixed(2)} @ ${formatSolanaTokenPrice(opened.position.entry_price)}).`;
                              })
                            }
                            className="px-2.5 py-1 text-[11px] font-semibold bg-emerald-700 hover:bg-emerald-600 disabled:opacity-40 disabled:cursor-not-allowed text-white rounded"
                          >
                            Open Paper ({formatUsd(ledger.max_next_notional_usd)})
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={busyAction !== null || !canResearch || kill_switch_active}
                            onClick={() =>
                              void runAction(`reval-${cand.mint}`, async () => {
                                const ev = await certaintyApi.evaluateSolanaMint({
                                  mint: cand.mint,
                                  symbol: cand.symbol,
                                  analyst_mode: analystMode,
                                  preferred_provider: preferredProvider || null,
                                });
                                return `Evaluated ${cand.symbol} -> ${ev.status}${
                                  ev.reasons && ev.reasons.length ? ` (${ev.reasons.join(', ')})` : ''
                                }.`;
                              })
                            }
                            className="px-2.5 py-1 text-[11px] font-medium bg-slate-800 hover:bg-slate-700 disabled:opacity-40 disabled:cursor-not-allowed border border-slate-700 text-slate-200 rounded"
                          >
                            Evaluate
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Simulated Paper Positions & Six-Agent Audit Trail */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <section className="xl:col-span-2 border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100 flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-emerald-400" />
                4. Simulated Paper Positions ({openPositions.length} Open / {closedPositions.length} Closed)
              </h2>
              <p className="text-xs text-slate-400 mt-0.5">
                Paper-only ledger tracking entry, mark, peak price, trailing-stop arm state, and realized exit reason.
              </p>
            </div>
            <div className="flex items-center gap-2">
              <ProvenanceLabel provenance="SIMULATED" />
              <button
                type="button"
                disabled={busyAction !== null || openPositions.length === 0 || !canPaperTrade}
                title={
                  openPositions.length === 0
                    ? 'No open positions to mark'
                    : canPaperTrade
                    ? 'Refresh live DexScreener marks and evaluate hard/trailing stops'
                    : 'Requires OPERATOR or OWNER role'
                }
                onClick={() =>
                  void runAction('mark-pos', async () => {
                    const res = await certaintyApi.markSolanaPaperPositions();
                    return `Marked ${res.updated_positions.length} open position(s); ${res.closed_positions.length} exited by risk stops. Cash=${formatUsd(res.balance_usd)}.`;
                  })
                }
                className="px-3 py-1.5 text-xs font-semibold bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg inline-flex items-center gap-1.5"
              >
                <RefreshCw className="w-3.5 h-3.5" />
                Mark Open Positions
              </button>
            </div>
          </div>

          <div className="overflow-x-auto max-h-72 overflow-y-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-slate-800 text-slate-400">
                <tr>
                  <th className="py-2 pr-3 font-medium">Position</th>
                  <th className="py-2 pr-3 font-medium text-right">Entry / Mark / Peak</th>
                  <th className="py-2 pr-3 font-medium text-right">Notional</th>
                  <th className="py-2 pr-3 font-medium">Trailing / Exit</th>
                  <th className="py-2 font-medium text-right">Sim PnL</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80 font-mono">
                {positions.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="py-5 text-slate-500 font-sans">
                      No simulated Solana paper positions opened yet.
                    </td>
                  </tr>
                ) : (
                  positions.map((pos) => {
                    const livePnlPct =
                      pos.status === 'CLOSED'
                        ? pos.realized_pnl_pct
                        : ((pos.mark_price - pos.entry_price) / pos.entry_price) * 100;
                    const livePnlUsd =
                      pos.status === 'CLOSED'
                        ? pos.realized_pnl_usd
                        : pos.notional_usd * ((pos.mark_price - pos.entry_price) / pos.entry_price);
                    return (
                      <tr key={pos.position_id}>
                        <td className="py-2 pr-3">
                          <div className="text-slate-100 font-semibold">{pos.symbol}</div>
                          <div className="text-[11px] text-slate-500">{pos.position_id}</div>
                        </td>
                        <td className="py-2 pr-3 text-right tabular-nums text-slate-200 whitespace-nowrap">
                          {formatSolanaTokenPrice(pos.entry_price)} → {formatSolanaTokenPrice(pos.mark_price)}
                          <div className="text-[11px] text-slate-500">
                            Peak {formatSolanaTokenPrice(pos.peak_price)}
                          </div>
                        </td>
                        <td className="py-2 pr-3 text-right tabular-nums text-slate-200">
                          {formatUsd(pos.notional_usd)}
                        </td>
                        <td className="py-2 pr-3 text-[11px]">
                          <div
                            className={
                              pos.status === 'OPEN'
                                ? pos.trailing_active
                                  ? 'text-emerald-400'
                                  : 'text-sky-400'
                                : 'text-slate-400'
                            }
                          >
                            {pos.status}{' '}
                            {pos.status === 'OPEN'
                              ? pos.trailing_active
                                ? '· TRAILING ARMED'
                                : '· HARD -6% WATCH'
                              : ''}
                          </div>
                          {pos.exit_reason ? (
                            <div className="text-slate-500">{pos.exit_reason}</div>
                          ) : null}
                        </td>
                        <td
                          className={`py-2 text-right tabular-nums whitespace-nowrap ${
                            livePnlUsd < 0
                              ? 'text-rose-400'
                              : livePnlUsd > 0
                              ? 'text-emerald-400'
                              : 'text-slate-300'
                          }`}
                        >
                          {formatUsd(livePnlUsd)} ({livePnlPct >= 0 ? '+' : ''}
                          {livePnlPct.toFixed(2)}%)
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
            <h2 className="text-sm font-semibold text-slate-100 flex items-center gap-2">
              <Activity className="w-4 h-4 text-sky-400" />
              5. Six-Agent Audit Trail ({events.length})
            </h2>
            <ProvenanceLabel provenance="LIVE" detail="LOGGER" />
          </div>

          <div className="space-y-2.5 text-xs max-h-72 overflow-y-auto pr-1">
            {events.length === 0 ? (
              <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800 text-slate-500">
                No Solana six-agent audit events logged yet.
              </div>
            ) : (
              events.slice(0, 25).map((ev) => (
                <div
                  key={ev.event_id}
                  className="p-2.5 rounded-lg bg-[#0E1522] border border-slate-800 space-y-1"
                >
                  <div className="flex items-center justify-between gap-2 font-mono text-[11px]">
                    <span className="text-sky-400 font-semibold">{ev.agent}</span>
                    <span className="text-slate-400">{ev.event_type}</span>
                  </div>
                  <div className="font-mono text-[11px] text-slate-500 truncate">
                    {ev.mint ? `${ev.mint} · ` : ''}
                    {formatTimestampShort(ev.created_at)}
                  </div>
                </div>
              ))
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
