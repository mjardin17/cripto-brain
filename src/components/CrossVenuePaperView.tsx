import { useState } from 'react';
import { certaintyApi } from '../api/client';
import type {
  ArbitrageOpportunity,
  CrossVenueOpportunityRecord,
  CrossVenuePairRecord,
  CrossVenuePairRegisterResponse,
  KalshiBookSnapshotResponse,
  KalshiMarketSummary,
  PaperBlotterResponse,
  PaperExecuteResponse,
  PaperExecutionScenario,
  PaperPnlLedgerRecord,
  PredictionRecord,
  UserPermissions,
} from '../types/certainty';
import {
  canConfirmBlotterResolution,
  formatArenaAttribution,
  formatPct,
  formatPrice,
  formatProbPct,
  formatScoreMetric,
  formatShares,
  formatTimestampShort,
  formatUsd,
  summarizeBlotterResolution,
  summarizeExecutionOutcome,
} from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';
import { PairReviewPanel } from './PairReviewPanel';
import { SettlementVerificationPanel } from './SettlementVerificationPanel';

interface CrossVenuePaperViewProps {
  polymarketOpportunities: ArbitrageOpportunity[];
  kalshiMarkets: KalshiMarketSummary[];
  kalshiLoading: boolean;
  pairs: CrossVenuePairRecord[];
  crossOpportunities: CrossVenueOpportunityRecord[];
  blotter: PaperBlotterResponse | null;
  openPredictions?: PredictionRecord[];
  permissions?: UserPermissions;
  onRefreshKalshi: () => Promise<void>;
  onRefreshCrossVenue: () => Promise<void>;
}

type BlotterSubTab = 'orders' | 'fills' | 'positions' | 'pnl';

export function CrossVenuePaperView({
  polymarketOpportunities,
  kalshiMarkets,
  kalshiLoading,
  pairs,
  crossOpportunities,
  blotter,
  openPredictions = [],
  permissions,
  onRefreshKalshi,
  onRefreshCrossVenue,
}: CrossVenuePaperViewProps) {
  const canManagePairs = permissions?.can_manage_pairs ?? true;
  const canExecutePaper = permissions?.can_execute_paper_trades ?? true;
  const canSubmitResearch = permissions?.can_submit_research ?? true;
  const canResolveOutcomes = permissions?.can_resolve_predictions ?? canExecutePaper;
  const [selectedTicker, setSelectedTicker] = useState<string | null>(null);
  const [kalshiBook, setKalshiBook] = useState<KalshiBookSnapshotResponse | null>(null);
  const [kalshiBookLoading, setKalshiBookLoading] = useState(false);
  const [kalshiBookError, setKalshiBookError] = useState<string | null>(null);

  // Pair matcher state
  const [polyMarketId, setPolyMarketId] = useState(
    polymarketOpportunities[0]?.market_id ?? ''
  );
  const [kalshiTicker, setKalshiTicker] = useState(kalshiMarkets[0]?.ticker ?? '');
  const [predicateVerified, setPredicateVerified] = useState(false);
  const [sourceVerified, setSourceVerified] = useState(false);
  const [voidCompatible, setVoidCompatible] = useState(false);
  const [polarityInverted, setPolarityInverted] = useState(false);
  const [allowEarlyClose, setAllowEarlyClose] = useState(false);
  const [pairNotes, setPairNotes] = useState('');
  const [registerBusy, setRegisterBusy] = useState(false);
  const [registerResult, setRegisterResult] = useState<CrossVenuePairRegisterResponse | null>(null);
  const [pairError, setPairError] = useState<string | null>(null);

  // Cross-venue scanner & paper simulator state
  const [targetShares, setTargetShares] = useState(25);
  const [slippageBps, setSlippageBps] = useState(15);
  const [scenario, setScenario] = useState<PaperExecutionScenario>('LIVE_BOOKS');
  const [scanBusy, setScanBusy] = useState(false);
  const [execBusyPair, setExecBusyPair] = useState<string | null>(null);
  const [execNotice, setExecNotice] = useState<string | null>(null);
  const [lastExecution, setLastExecution] = useState<PaperExecuteResponse | null>(null);

  // Blotter sub-tab & manual outcome confirmation flow
  const [blotterTab, setBlotterTab] = useState<BlotterSubTab>('orders');
  const [confirmingEntry, setConfirmingEntry] = useState<PaperPnlLedgerRecord | null>(null);
  const [selectedResolution, setSelectedResolution] = useState<'YES' | 'NO' | 'VOID'>('YES');
  const [resolutionSource, setResolutionSource] = useState('');
  const [resolutionNotes, setResolutionNotes] = useState('');
  const [linkedPredictionId, setLinkedPredictionId] = useState('');
  const [confirmChecked, setConfirmChecked] = useState(false);
  const [resolveBusy, setResolveBusy] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const [resolveBanner, setResolveBanner] = useState<string | null>(null);

  const selectedLinkedPrediction =
    openPredictions.find((p) => p.prediction_id === linkedPredictionId) ?? null;

  const openConfirmModal = (entry: PaperPnlLedgerRecord) => {
    setConfirmingEntry(entry);
    setSelectedResolution('YES');
    setResolutionSource('');
    setResolutionNotes('');
    setLinkedPredictionId('');
    setConfirmChecked(false);
    setResolveError(null);
  };

  const closeConfirmModal = () => {
    if (resolveBusy) return;
    setConfirmingEntry(null);
    setResolveError(null);
  };

  const handleConfirmResolution = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!confirmingEntry || !confirmChecked || resolveBusy) return;
    setResolveBusy(true);
    setResolveError(null);
    try {
      const res = await certaintyApi.resolvePaperBlotterEntry(confirmingEntry.ledger_id, {
        resolution_result: selectedResolution,
        resolution_source: resolutionSource.trim(),
        notes: resolutionNotes.trim(),
        linked_prediction_id: linkedPredictionId.trim() || null,
      });
      const brierDetail =
        res.resolved_prediction && res.resolved_prediction.brier_score !== null
          ? ` · Linked forecast ${res.resolved_prediction.prediction_id} scored Brier ${formatScoreMetric(
              res.resolved_prediction.brier_score
            )}`
          : res.entry.resolution_result === 'VOID'
            ? ' · VOID excluded from Brier & mistake training'
            : '';
      await onRefreshCrossVenue();
      setResolveBanner(
        `Confirmed ${res.entry.ledger_id} (${res.entry.pair_id}) as ${res.entry.resolution_result} · Learning Cycle ${res.learning_cycle_id} recorded${brierDetail}.`
      );
      setConfirmingEntry(null);
    } catch (err) {
      setResolveError(
        err instanceof Error ? err.message : 'Failed to confirm paper blotter outcome.'
      );
    } finally {
      setResolveBusy(false);
    }
  };

  const handleSelectKalshiTicker = async (ticker: string) => {
    setSelectedTicker(ticker);
    setKalshiBookLoading(true);
    setKalshiBookError(null);
    try {
      const book = await certaintyApi.getKalshiBook(ticker, 15);
      setKalshiBook(book);
    } catch (err) {
      setKalshiBookError(err instanceof Error ? err.message : 'Failed to fetch Kalshi book');
      setKalshiBook(null);
    } finally {
      setKalshiBookLoading(false);
    }
  };

  const handleRegisterPair = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!polyMarketId.trim() || !kalshiTicker.trim()) {
      setPairError('Both Polymarket Market ID and Kalshi Ticker are required.');
      return;
    }
    setRegisterBusy(true);
    setPairError(null);
    try {
      const resp = await certaintyApi.registerCrossVenuePair({
        poly_market_id: polyMarketId.trim(),
        kalshi_ticker: kalshiTicker.trim(),
        resolution_source_verified: sourceVerified,
        predicate_verified: predicateVerified,
        polarity_inverted: polarityInverted,
        void_rules_compatible: voidCompatible,
        allow_early_close_variance: allowEarlyClose,
        notes: pairNotes.trim(),
      });
      setRegisterResult(resp);
      await onRefreshCrossVenue();
    } catch (err) {
      setPairError(err instanceof Error ? err.message : 'Failed to verify contract pair');
    } finally {
      setRegisterBusy(false);
    }
  };

  const handleScanCrossVenue = async () => {
    setScanBusy(true);
    setExecNotice(null);
    try {
      const resp = await certaintyApi.scanCrossVenue({
        target_shares: targetShares,
        slippage_buffer_bps: slippageBps,
      });
      setExecNotice(
        resp.count > 0
          ? `Evaluated ${resp.count} registered pair(s) live against Polymarket CLOB V2 and Kalshi Trade API v2.`
          : resp.message || 'No pairs available to scan.'
      );
      await onRefreshCrossVenue();
    } catch (err) {
      setExecNotice(err instanceof Error ? err.message : 'Cross-venue scan failed');
    } finally {
      setScanBusy(false);
    }
  };

  const handleExecutePaper = async (pairId: string) => {
    setExecBusyPair(pairId);
    setExecNotice(null);
    try {
      const res = await certaintyApi.executePaperTrade({
        pair_id: pairId,
        target_shares: targetShares,
        slippage_buffer_bps: slippageBps,
        scenario,
      });
      setLastExecution(res);
      setExecNotice(
        `Simulated Execution ${res.execution_id} (${res.status}) · Risk Gate Audit ${res.gate_result.audit_id}`
      );
      await onRefreshCrossVenue();
    } catch (err) {
      setExecNotice(err instanceof Error ? err.message : 'Paper execution request failed');
    } finally {
      setExecBusyPair(null);
    }
  };

  const summary = blotter?.summary;
  const orders = blotter?.orders ?? [];
  const fills = blotter?.fills ?? [];
  const positions = blotter?.positions ?? [];
  const pnlLedger = blotter?.pnl_ledger ?? [];

  return (
    <div className="space-y-6">
      {/* Banner making Phase 2 Paper-Only lock explicit */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl px-5 py-3 flex flex-wrap lg:flex-nowrap items-center justify-between gap-3 text-xs">
        <div className="flex flex-wrap items-center gap-2.5 min-w-0">
          <ProvenanceLabel provenance="SIMULATED" detail="Paper Trading Only · Live Execution Disabled" />
          <span className="text-slate-300">
            Audited fail-closed Risk Gate paper simulations (min 2.00% edge, $2.5k/venue cap, -$250/day loss cap).
          </span>
        </div>
        <span className="font-mono text-amber-300 shrink-0 whitespace-nowrap">NO REAL CAPITAL DEPLOYED</span>
      </section>

      {/* Section 1: Kalshi Live Binary Explorer + Complement Book */}
      <section className="grid grid-cols-1 xl:grid-cols-12 gap-6">
        <div className="xl:col-span-7 border border-slate-800 bg-[#111827] rounded-xl overflow-hidden flex flex-col">
          <div className="px-5 py-4 border-b border-slate-800 flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="text-sm font-semibold text-slate-100">
                  Kalshi Trade API v2 Binary Contracts
                </h2>
                <ProvenanceLabel provenance="LIVE" detail="Read-Only Public Feed" />
              </div>
              <p className="mt-0.5 text-xs text-slate-400">
                Single non-combo binary contracts ordered by 24h volume. Click a row to inspect YES/NO books.
              </p>
            </div>
            <button
              type="button"
              onClick={() => void onRefreshKalshi()}
              disabled={kalshiLoading}
              className="px-3 py-1.5 text-xs font-medium text-slate-200 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg transition-colors whitespace-nowrap shrink-0"
            >
              {kalshiLoading ? 'Loading...' : 'Refresh Kalshi'}
            </button>
          </div>

          {kalshiMarkets.length === 0 ? (
            <div className="p-8 text-center text-xs text-slate-400">
              {kalshiLoading
                ? 'Fetching live Kalshi Trade API v2 markets...'
                : 'No live Kalshi binary contracts loaded yet. Click Refresh Kalshi.'}
            </div>
          ) : (
            <div className="overflow-x-auto max-h-[320px] overflow-y-auto">
              <table className="w-full text-left border-collapse text-xs">
                <thead className="sticky top-0 z-10">
                  <tr className="border-b border-slate-800 bg-[#0E1522] text-slate-400 font-medium">
                    <th className="py-2.5 px-4">Ticker / Event</th>
                    <th className="py-2.5 px-3 text-right">YES Bid / Ask</th>
                    <th className="py-2.5 px-3 text-right">NO Bid / Ask</th>
                    <th className="py-2.5 px-3 text-right">Vol 24h</th>
                    <th className="py-2.5 px-3 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80">
                  {kalshiMarkets.map((mkt) => {
                    const selected = selectedTicker === mkt.ticker;
                    return (
                      <tr
                        key={mkt.ticker}
                        onClick={() => void handleSelectKalshiTicker(mkt.ticker)}
                        className={`cursor-pointer transition-colors ${
                          selected ? 'bg-sky-950/35' : 'hover:bg-slate-800/40'
                        }`}
                      >
                        <td className="py-2.5 px-4 max-w-xs">
                          <div className="font-mono font-semibold text-slate-100">{mkt.ticker}</div>
                          <div className="text-slate-400 line-clamp-1">{mkt.title}</div>
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200 whitespace-nowrap">
                          {formatPrice(mkt.yes_bid_dollars)} / {formatPrice(mkt.yes_ask_dollars)}
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200 whitespace-nowrap">
                          {formatPrice(mkt.no_bid_dollars)} / {formatPrice(mkt.no_ask_dollars)}
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-300">
                          {formatShares(mkt.volume_24h)}
                        </td>
                        <td className="py-2.5 px-3 text-right">
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              setKalshiTicker(mkt.ticker);
                            }}
                            className="px-2.5 py-1 text-[11px] font-medium text-sky-300 bg-sky-950/60 hover:bg-sky-900/70 border border-sky-800/70 rounded transition-colors whitespace-nowrap"
                          >
                            Use Ticker
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Complement-Symmetry Order Book */}
        <div className="xl:col-span-5 border border-slate-800 bg-[#111827] rounded-xl p-5 flex flex-col justify-between space-y-4">
          <div className="flex items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold text-slate-100">
                Kalshi Complement YES/NO Book
              </h3>
              <p className="text-xs text-slate-400">
                {selectedTicker
                  ? `Ticker ${selectedTicker} · YES ask = 1.00 - NO bid`
                  : 'Select a Kalshi contract to inspect bids & implied complement asks'}
              </p>
            </div>
            <ProvenanceLabel provenance={kalshiBook ? 'LIVE' : 'UNAVAILABLE'} />
          </div>

          {kalshiBookLoading ? (
            <div className="h-52 rounded-lg bg-[#0E1522] border border-slate-800 animate-pulse" />
          ) : kalshiBookError ? (
            <div className="p-4 rounded-lg border border-rose-900 bg-rose-950/30 text-xs text-rose-300">
              {kalshiBookError}
            </div>
          ) : kalshiBook ? (
            <div className="grid grid-cols-2 gap-4 text-xs">
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2 text-slate-300 font-medium">
                  <span className="whitespace-nowrap">YES Book</span>
                  <span className="font-mono tabular-nums text-[11px] text-emerald-400 whitespace-nowrap">
                    {formatPrice(kalshiBook.yes.best_bid)} / {formatPrice(kalshiBook.yes.best_ask)}
                  </span>
                </div>
                <div className="rounded-lg border border-slate-800 bg-[#0E1522] p-2.5 space-y-1 font-mono tabular-nums">
                  <div className="text-[11px] text-slate-500 flex justify-between">
                    <span>Implied Asks</span>
                    <span>Size</span>
                  </div>
                  {kalshiBook.yes.asks.slice(0, 4).map((lvl, idx) => (
                    <div key={`ka-y-${idx}`} className="flex justify-between text-rose-300">
                      <span>{formatPrice(lvl.price)}</span>
                      <span>{formatShares(lvl.size)}</span>
                    </div>
                  ))}
                  <div className="pt-1 border-t border-slate-800 text-[11px] text-slate-500 flex justify-between">
                    <span>Resting Bids</span>
                    <span>Size</span>
                  </div>
                  {kalshiBook.yes.bids.slice(0, 4).map((lvl, idx) => (
                    <div key={`kb-y-${idx}`} className="flex justify-between text-emerald-300">
                      <span>{formatPrice(lvl.price)}</span>
                      <span>{formatShares(lvl.size)}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2 text-slate-300 font-medium">
                  <span className="whitespace-nowrap">NO Book</span>
                  <span className="font-mono tabular-nums text-[11px] text-sky-400 whitespace-nowrap">
                    {formatPrice(kalshiBook.no.best_bid)} / {formatPrice(kalshiBook.no.best_ask)}
                  </span>
                </div>
                <div className="rounded-lg border border-slate-800 bg-[#0E1522] p-2.5 space-y-1 font-mono tabular-nums">
                  <div className="text-[11px] text-slate-500 flex justify-between">
                    <span>Implied Asks</span>
                    <span>Size</span>
                  </div>
                  {kalshiBook.no.asks.slice(0, 4).map((lvl, idx) => (
                    <div key={`ka-n-${idx}`} className="flex justify-between text-rose-300">
                      <span>{formatPrice(lvl.price)}</span>
                      <span>{formatShares(lvl.size)}</span>
                    </div>
                  ))}
                  <div className="pt-1 border-t border-slate-800 text-[11px] text-slate-500 flex justify-between">
                    <span>Resting Bids</span>
                    <span>Size</span>
                  </div>
                  {kalshiBook.no.bids.slice(0, 4).map((lvl, idx) => (
                    <div key={`kb-n-${idx}`} className="flex justify-between text-emerald-300">
                      <span>{formatPrice(lvl.price)}</span>
                      <span>{formatShares(lvl.size)}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="h-52 rounded-lg bg-[#0E1522] border border-slate-800/80 flex items-center justify-center text-xs text-slate-500 px-6 text-center">
              Click any Kalshi market row on the left to inspect its normalized orderbook_fp ladder.
            </div>
          )}
        </div>
      </section>

      {/* Section 2: Fail-Closed Contract Matcher & Cross-Venue Scanner */}
      <section className="grid grid-cols-1 xl:grid-cols-12 gap-6">
        <form
          onSubmit={(e) => void handleRegisterPair(e)}
          className="xl:col-span-5 border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4"
        >
          <div className="flex items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-100">
                Fail-Closed Settlement Matcher
              </h2>
              <p className="text-xs text-slate-400">
                Verifies cutoff parity, binary structure, and attestation before Risk Gate can approve a pair.
              </p>
            </div>
            <ProvenanceLabel provenance="LIVE" />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
            <label className="block space-y-1">
              <span className="text-slate-400">Polymarket Market ID</span>
              <input
                type="text"
                value={polyMarketId}
                onChange={(e) => setPolyMarketId(e.target.value)}
                placeholder="e.g. 559651"
                list="poly-market-suggestions"
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono focus:outline-none focus:ring-2 focus:ring-sky-500"
              />
              <datalist id="poly-market-suggestions">
                {polymarketOpportunities.map((o) => (
                  <option key={o.market_id} value={o.market_id}>
                    {o.question}
                  </option>
                ))}
              </datalist>
            </label>

            <label className="block space-y-1">
              <span className="text-slate-400">Kalshi Ticker</span>
              <input
                type="text"
                value={kalshiTicker}
                onChange={(e) => setKalshiTicker(e.target.value)}
                placeholder="e.g. KXFUSION-30-JAN01"
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono focus:outline-none focus:ring-2 focus:ring-sky-500"
              />
            </label>
          </div>

          <div className="space-y-2 text-xs border-t border-slate-800 pt-3">
            <label className="flex items-center gap-2.5 text-slate-300 cursor-pointer">
              <input
                type="checkbox"
                checked={predicateVerified}
                onChange={(e) => setPredicateVerified(e.target.checked)}
                className="rounded border-slate-700 bg-[#0B111B]"
              />
              <span>Identical event predicate &amp; strike verified</span>
            </label>
            <label className="flex items-center gap-2.5 text-slate-300 cursor-pointer">
              <input
                type="checkbox"
                checked={sourceVerified}
                onChange={(e) => setSourceVerified(e.target.checked)}
                className="rounded border-slate-700 bg-[#0B111B]"
              />
              <span>Authoritative settlement source / oracle verified identical</span>
            </label>
            <label className="flex items-center gap-2.5 text-slate-300 cursor-pointer">
              <input
                type="checkbox"
                checked={voidCompatible}
                onChange={(e) => setVoidCompatible(e.target.checked)}
                className="rounded border-slate-700 bg-[#0B111B]"
              />
              <span>Void, push &amp; cancellation rules compatible</span>
            </label>
            <div className="flex flex-wrap gap-4 pt-1 text-slate-400">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={polarityInverted}
                  onChange={(e) => setPolarityInverted(e.target.checked)}
                  className="rounded border-slate-700 bg-[#0B111B]"
                />
                <span>Inverted Polarity (Blocks Pair)</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={allowEarlyClose}
                  onChange={(e) => setAllowEarlyClose(e.target.checked)}
                  className="rounded border-slate-700 bg-[#0B111B]"
                />
                <span>Early-Close Reconciled</span>
              </label>
            </div>
          </div>

          <label className="block space-y-1 text-xs">
            <span className="text-slate-400">Verification Notes</span>
            <input
              type="text"
              value={pairNotes}
              onChange={(e) => setPairNotes(e.target.value)}
              placeholder="Document exact settlement source comparison..."
              className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
            />
          </label>

          {pairError ? (
            <div className="p-3 rounded-lg border border-rose-900 bg-rose-950/30 text-xs text-rose-300">
              {pairError}
            </div>
          ) : null}

          <button
            type="submit"
            disabled={registerBusy || !canManagePairs}
            title={canManagePairs ? 'Verify settlement equivalence & register pair' : 'Requires OPERATOR or OWNER role'}
            className="w-full px-4 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
          >
            {registerBusy
              ? 'Verifying Settlement Rules...'
              : !canManagePairs
                ? 'Pair Registration Locked (OPERATOR Required)'
                : 'Verify & Register Contract Pair'}
          </button>

          {registerResult ? (
            <div className="p-3.5 rounded-lg border border-slate-800 bg-[#0E1522] space-y-2 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-mono text-slate-200">{registerResult.pair.pair_id}</span>
                <span
                  className={`font-mono font-semibold ${
                    registerResult.assessment.identical_settlement_verified
                      ? 'text-emerald-400'
                      : 'text-rose-400'
                  }`}
                >
                  {registerResult.assessment.status}
                </span>
              </div>
              <div className="text-[11px] text-slate-400">
                Title similarity: {(registerResult.title_similarity * 100).toFixed(1)}% · Cutoff delta:{' '}
                {registerResult.assessment.cutoff_delta_hours !== null
                  ? `${registerResult.assessment.cutoff_delta_hours}h`
                  : 'N/A'}
              </div>
              <div className="space-y-1 pt-1">
                {registerResult.assessment.checks.map((chk) => (
                  <div key={chk.check_id} className="flex items-start justify-between gap-2 text-[11px]">
                    <span className="font-mono text-slate-300">{chk.check_id}</span>
                    <span className={chk.passed ? 'text-emerald-400' : 'text-rose-400'}>
                      {chk.passed ? 'PASS' : 'FAIL'}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </form>

        {/* Registered Pairs & Paper Execution Trigger */}
        <div className="xl:col-span-7 border border-slate-800 bg-[#111827] rounded-xl overflow-hidden flex flex-col">
          <div className="px-5 py-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="flex items-center gap-2.5">
                <h2 className="text-sm font-semibold text-slate-100">
                  Cross-Venue Scanner &amp; Paper Execution
                </h2>
                <ProvenanceLabel provenance="SIMULATED" detail="Min Edge 2.00%" />
              </div>
              <p className="mt-0.5 text-xs text-slate-400">
                Scans registered pairs across Polymarket &amp; Kalshi and dispatches Risk-Gate-approved paper simulations.
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2 text-xs">
              <label className="flex items-center gap-1.5 text-slate-400">
                <span>Shares</span>
                <input
                  type="number"
                  min={1}
                  max={500}
                  value={targetShares}
                  onChange={(e) => setTargetShares(Math.max(1, Number(e.target.value) || 25))}
                  className="w-16 px-2 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg font-mono tabular-nums text-slate-100"
                />
              </label>
              <label className="flex items-center gap-1.5 text-slate-400">
                <span>Slip bps</span>
                <input
                  type="number"
                  min={0}
                  max={200}
                  value={slippageBps}
                  onChange={(e) => setSlippageBps(Math.max(0, Number(e.target.value) || 0))}
                  className="w-14 px-2 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg font-mono tabular-nums text-slate-100"
                />
              </label>
              <label className="flex items-center gap-1.5 text-slate-400">
                <span>Sim Mode</span>
                <select
                  value={scenario}
                  onChange={(e) => setScenario(e.target.value as PaperExecutionScenario)}
                  className="px-2 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
                >
                  <option value="LIVE_BOOKS">Live Books</option>
                  <option value="PARTIAL_FILL_LEG1">Partial Leg 1 (40%)</option>
                  <option value="SECOND_LEG_PRICE_DRIFT_UNWIND">Leg 2 Drift -&gt; Unwind</option>
                  <option value="LEG2_REJECTED_UNWIND">Leg 2 Reject -&gt; Unwind</option>
                </select>
              </label>
              <button
                type="button"
                onClick={() => void handleScanCrossVenue()}
                disabled={scanBusy || !canManagePairs}
                title={canManagePairs ? 'Scan registered pairs' : 'Requires OPERATOR or OWNER role'}
                className="px-3 py-1.5 font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors whitespace-nowrap"
              >
                {scanBusy ? 'Scanning...' : 'Scan Pairs'}
              </button>
            </div>
          </div>

          {execNotice ? (
            <div className="mx-5 mt-4 px-3.5 py-2.5 rounded-lg border border-slate-700 bg-[#0E1522] text-xs text-slate-200 flex items-center justify-between gap-3">
              <span>{execNotice}</span>
              <button
                type="button"
                onClick={() => setExecNotice(null)}
                className="text-slate-400 hover:text-slate-200 shrink-0"
              >
                Dismiss
              </button>
            </div>
          ) : null}

          <div className="p-5 space-y-5 flex-1">
            {pairs.length === 0 ? (
              <div className="p-8 text-center border border-dashed border-slate-800 rounded-lg text-xs text-slate-400">
                No cross-venue contract pairs registered yet. Select a Polymarket ID and Kalshi Ticker on the left to verify settlement compatibility.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400 font-medium">
                      <th className="py-2 pr-3">Pair</th>
                      <th className="py-2 px-3">Settlement</th>
                      <th className="py-2 px-3 text-right">Cutoff Δ</th>
                      <th className="py-2 pl-3 text-right">Paper Simulation</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/80">
                    {pairs.map((p) => (
                      <tr key={p.pair_id}>
                        <td className="py-2.5 pr-3">
                          <div className="font-mono font-semibold text-slate-100">{p.pair_id}</div>
                          <div className="text-[11px] text-slate-400 font-mono">
                            Poly {p.poly_market_id} ↔ Kalshi {p.kalshi_ticker}
                          </div>
                        </td>
                        <td className="py-2.5 px-3">
                          <span
                            className={`font-mono font-semibold ${
                              p.identical_settlement_verified ? 'text-emerald-400' : 'text-rose-400'
                            }`}
                          >
                            {p.settlement_status}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-300">
                          {p.cutoff_delta_hours !== null ? `${p.cutoff_delta_hours}h` : '—'}
                        </td>
                        <td className="py-2.5 pl-3 text-right">
                          <button
                            type="button"
                            onClick={() => void handleExecutePaper(p.pair_id)}
                            disabled={execBusyPair === p.pair_id || !canExecutePaper}
                            title={canExecutePaper ? 'Execute Risk-Gate-checked paper simulation' : 'Requires OPERATOR or OWNER role'}
                            className="px-3 py-1.5 font-semibold text-xs text-amber-100 bg-amber-700/80 hover:bg-amber-600 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors whitespace-nowrap"
                          >
                            {execBusyPair === p.pair_id
                              ? 'Running...'
                              : !canExecutePaper
                                ? 'Operator Required'
                                : 'Simulate Paper Trade'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Latest Cross-Venue Scan Evaluations */}
            {crossOpportunities.length > 0 ? (
              <div className="space-y-2 pt-2 border-t border-slate-800">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium text-slate-300">
                    Recent Cross-Venue Book Evaluations
                  </span>
                  <ProvenanceLabel provenance="LIVE" />
                </div>
                <div className="overflow-x-auto max-h-56 overflow-y-auto">
                  <table className="w-full text-left border-collapse text-xs">
                    <thead>
                      <tr className="border-b border-slate-800 text-slate-400 font-medium">
                        <th className="py-2 pr-3">Direction</th>
                        <th className="py-2 px-3 text-right">Raw Cost</th>
                        <th className="py-2 px-3 text-right">Fees</th>
                        <th className="py-2 px-3 text-right">Net Edge</th>
                        <th className="py-2 pl-3">Risk Gate</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/80">
                      {crossOpportunities.slice(0, 8).map((opp) => (
                        <tr key={opp.xopp_id}>
                          <td className="py-2 pr-3">
                            <div className="font-mono text-slate-200">{opp.direction}</div>
                            <div className="text-[11px] text-slate-500 font-mono">
                              {opp.pair_id} · {opp.target_shares} sh
                            </div>
                          </td>
                          <td className="py-2 px-3 text-right font-mono tabular-nums text-slate-200">
                            {formatPrice(opp.combined_raw_cost)}
                          </td>
                          <td className="py-2 px-3 text-right font-mono tabular-nums text-amber-300">
                            {formatUsd(opp.total_fees_usd)}
                          </td>
                          <td
                            className={`py-2 px-3 text-right font-mono tabular-nums font-semibold ${
                              opp.net_edge_pct !== null && opp.net_edge_pct >= 2.0
                                ? 'text-emerald-400'
                                : 'text-rose-400'
                            }`}
                          >
                            {formatPct(opp.net_edge_pct, 3, true)}
                          </td>
                          <td className="py-2 pl-3 max-w-[240px]">
                            <div
                              className={`font-mono font-semibold ${
                                opp.risk_gate_approved ? 'text-emerald-400' : 'text-rose-400'
                              }`}
                            >
                              {opp.risk_gate_approved ? 'APPROVED' : 'DENIED'}
                            </div>
                            <div className="text-[11px] text-slate-400 truncate" title={opp.risk_gate_reason}>
                              {opp.risk_gate_reason}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : null}

            {lastExecution ? (
              <div className="p-3.5 rounded-lg border border-slate-800 bg-[#0E1522] text-xs space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono font-semibold text-slate-100">
                    Last Run: {lastExecution.execution_id}
                  </span>
                  <span className={`font-mono font-semibold ${summarizeExecutionOutcome(lastExecution.status).toneClass}`}>
                    {summarizeExecutionOutcome(lastExecution.status).label}
                  </span>
                </div>
                <div className="text-slate-400">{lastExecution.gate_result.summary}</div>
              </div>
            ) : null}
          </div>
        </div>
      </section>

      {/* Section 2b: Discovered Pair Review */}
      <PairReviewPanel
        pairs={pairs}
        canManagePairs={canManagePairs}
        onRefreshCrossVenue={onRefreshCrossVenue}
      />

      {/* Section 3: Simulated Paper Execution Blotter */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2.5">
              <h2 className="text-base font-semibold text-slate-100">
                Simulated Paper Execution Blotter
              </h2>
              <ProvenanceLabel provenance="SIMULATED" detail="DuckDB Persisted · Not Real Profit" />
            </div>
            <p className="mt-0.5 text-xs text-slate-400">
              Complete leg-by-leg order, fill, resting-bid unwind, position, and P&amp;L audit ledger.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-1 p-1 bg-[#0B111B] border border-slate-800 rounded-lg text-xs">
            {(
              [
                ['orders', `Orders (${orders.length})`],
                ['fills', `Fills & Unwinds (${fills.length})`],
                ['positions', `Positions (${positions.length})`],
                ['pnl', `P&L Ledger (${pnlLedger.length})`],
              ] as Array<[BlotterSubTab, string]>
            ).map(([tabKey, label]) => (
              <button
                key={tabKey}
                type="button"
                onClick={() => setBlotterTab(tabKey)}
                className={`px-3 py-1.5 rounded font-medium transition-colors whitespace-nowrap ${
                  blotterTab === tabKey
                    ? 'bg-slate-800 text-slate-100'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* Blotter KPI summary strip */}
        <div className="grid grid-cols-2 lg:grid-cols-5 border-b border-slate-800 divide-y lg:divide-y-0 lg:divide-x divide-slate-800 text-xs">
          <div className="p-4">
            <div className="text-slate-400">Simulated Executions</div>
            <div className="mt-1 text-lg font-mono tabular-nums font-semibold text-slate-100">
              {summary?.executions ?? 0}
            </div>
          </div>
          <div className="p-4">
            <div className="text-slate-400">Matched / Unwound Shares</div>
            <div className="mt-1 text-lg font-mono tabular-nums font-semibold text-slate-100">
              {formatShares(summary?.total_matched_shares ?? 0)} /{' '}
              <span className="text-amber-300">{formatShares(summary?.total_unwound_shares ?? 0)}</span>
            </div>
          </div>
          <div className="p-4">
            <div className="text-slate-400">Realized Paper P&amp;L</div>
            <div
              className={`mt-1 text-lg font-mono tabular-nums font-semibold ${
                (summary?.daily_simulated_pnl_usd ?? 0) < 0
                  ? 'text-rose-400'
                  : (summary?.daily_simulated_pnl_usd ?? 0) > 0
                    ? 'text-emerald-400'
                    : 'text-slate-100'
              }`}
            >
              {formatUsd(summary?.daily_simulated_pnl_usd ?? 0)}
            </div>
          </div>
          <div className="p-4">
            <div className="text-slate-400">Locked Carry (Pending Settle)</div>
            <div className="mt-1 text-lg font-mono tabular-nums font-semibold text-sky-400">
              {formatUsd(summary?.total_locked_arb_edge_usd ?? 0)}
            </div>
          </div>
          <div className="p-4">
            <div className="text-slate-400">Confirmed Outcomes</div>
            <div className="mt-1 text-lg font-mono tabular-nums font-semibold text-emerald-300">
              {summary?.resolved_entries_count ??
                pnlLedger.filter((e) => !summarizeBlotterResolution(e.resolution_result).isPending)
                  .length}{' '}
              <span className="text-sm text-slate-500">/ {summary?.executions ?? pnlLedger.length}</span>
            </div>
            <div className="text-[11px] font-mono text-slate-500">
              {summary?.pending_resolution_count ??
                pnlLedger.filter((e) => summarizeBlotterResolution(e.resolution_result).isPending)
                  .length}{' '}
              pending confirmation
            </div>
          </div>
        </div>

        {resolveBanner ? (
          <div
            role="status"
            className="px-5 py-2.5 border-b border-emerald-800/70 bg-emerald-950/30 flex items-center justify-between gap-3 text-xs text-emerald-200"
          >
            <span className="font-mono">{resolveBanner}</span>
            <button
              type="button"
              onClick={() => setResolveBanner(null)}
              className="text-emerald-300 hover:text-emerald-100 text-[11px] underline shrink-0"
            >
              Dismiss
            </button>
          </div>
        ) : null}

        <div className="overflow-x-auto max-h-[360px] overflow-y-auto">
          {blotterTab === 'orders' ? (
            orders.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-500">
                No simulated paper orders logged yet. Select a verified pair above and click Simulate Paper Trade.
              </div>
            ) : (
              <table className="w-full text-left border-collapse text-xs">
                <thead className="sticky top-0 bg-[#0E1522] border-b border-slate-800 text-slate-400">
                  <tr>
                    <th className="py-2.5 px-4">Order / Execution</th>
                    <th className="py-2.5 px-3">Leg &amp; Venue</th>
                    <th className="py-2.5 px-3 text-right">Req / Fill</th>
                    <th className="py-2.5 px-3 text-right">Limit / Avg</th>
                    <th className="py-2.5 px-3 text-right">Fees / Latency</th>
                    <th className="py-2.5 px-4">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80">
                  {orders.map((o) => (
                    <tr key={o.order_id} className="hover:bg-slate-800/30">
                      <td className="py-2.5 px-4">
                        <div className="font-mono text-slate-200">{o.order_id}</div>
                        <div className="text-[11px] font-mono text-slate-500">
                          {o.execution_id} · Audit {o.audit_id}
                        </div>
                      </td>
                      <td className="py-2.5 px-3 font-mono">
                        <span className="text-slate-200">
                          L{o.leg_number} {o.side} {o.outcome}
                        </span>
                        <div className="text-[11px] text-slate-500">{o.venue}</div>
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatShares(o.requested_shares)} / {formatShares(o.filled_shares)}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatPrice(o.limit_price)} / {formatPrice(o.avg_fill_price)}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-300">
                        {formatUsd(o.fees_usd)} · {o.latency_ms}ms
                      </td>
                      <td className="py-2.5 px-4">
                        <div className="font-mono font-semibold text-amber-300">{o.status}</div>
                        <div className="text-[11px] text-slate-400 truncate max-w-xs" title={o.reason}>
                          {o.reason}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : null}

          {blotterTab === 'fills' ? (
            fills.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-500">No simulated fills recorded yet.</div>
            ) : (
              <table className="w-full text-left border-collapse text-xs">
                <thead className="sticky top-0 bg-[#0E1522] border-b border-slate-800 text-slate-400">
                  <tr>
                    <th className="py-2.5 px-4">Fill ID</th>
                    <th className="py-2.5 px-3">Venue / Side</th>
                    <th className="py-2.5 px-3 text-right">Shares</th>
                    <th className="py-2.5 px-3 text-right">Fill Price</th>
                    <th className="py-2.5 px-3 text-right">Notional / Fee</th>
                    <th className="py-2.5 px-4">Type / Time</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80">
                  {fills.map((f) => (
                    <tr key={f.fill_id} className="hover:bg-slate-800/30">
                      <td className="py-2.5 px-4 font-mono text-slate-200">{f.fill_id}</td>
                      <td className="py-2.5 px-3 font-mono text-slate-200">
                        {f.venue} · {f.side} {f.outcome}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatShares(f.shares)}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatPrice(f.fill_price)}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-300">
                        {formatUsd(f.notional_usd)} · fee {formatUsd(f.fee_usd)}
                      </td>
                      <td className="py-2.5 px-4 font-mono">
                        <span className={f.is_unwind ? 'text-amber-300 font-semibold' : 'text-emerald-400'}>
                          {f.is_unwind ? 'UNWIND (SIMULATED)' : 'ENTRY (SIMULATED)'}
                        </span>
                        <div className="text-[11px] text-slate-500">{formatTimestampShort(f.filled_at)}</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : null}

          {blotterTab === 'positions' ? (
            positions.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-500">No simulated positions recorded yet.</div>
            ) : (
              <table className="w-full text-left border-collapse text-xs">
                <thead className="sticky top-0 bg-[#0E1522] border-b border-slate-800 text-slate-400">
                  <tr>
                    <th className="py-2.5 px-4">Position / Pair</th>
                    <th className="py-2.5 px-3">Venue / Outcome</th>
                    <th className="py-2.5 px-3 text-right">Shares</th>
                    <th className="py-2.5 px-3 text-right">Entry Price</th>
                    <th className="py-2.5 px-3 text-right">Notional USD</th>
                    <th className="py-2.5 px-4">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80">
                  {positions.map((pos) => (
                    <tr key={pos.position_id} className="hover:bg-slate-800/30">
                      <td className="py-2.5 px-4 font-mono">
                        <div className="text-slate-200">{pos.position_id}</div>
                        <div className="text-[11px] text-slate-500">{pos.pair_id}</div>
                      </td>
                      <td className="py-2.5 px-3 font-mono text-slate-200">
                        {pos.venue} · {pos.outcome}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatShares(pos.shares)}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatPrice(pos.entry_price)}
                      </td>
                      <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatUsd(pos.notional_usd)}
                      </td>
                      <td className="py-2.5 px-4 font-mono">
                        <span
                          className={
                            pos.status === 'OPEN_HEDGED'
                              ? 'text-emerald-400 font-semibold'
                              : pos.status === 'OPEN_UNHEDGED'
                                ? 'text-rose-400 font-semibold'
                                : 'text-slate-400'
                          }
                        >
                          {pos.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : null}

          {blotterTab === 'pnl' ? (
            pnlLedger.length === 0 ? (
              <div className="p-8 text-center text-xs text-slate-500">
                No simulated P&amp;L ledger entries recorded yet.
              </div>
            ) : (
              <table className="w-full text-left border-collapse text-xs">
                <thead className="sticky top-0 bg-[#0E1522] border-b border-slate-800 text-slate-400">
                  <tr>
                    <th className="py-2.5 px-4">Ledger Entry</th>
                    <th className="py-2.5 px-3">Outcome</th>
                    <th className="py-2.5 px-3 text-right">Matched / Unwound</th>
                    <th className="py-2.5 px-3 text-right">Fees</th>
                    <th className="py-2.5 px-3 text-right">Realized Paper P&amp;L</th>
                    <th className="py-2.5 px-3 text-right">Locked Carry</th>
                    <th className="py-2.5 px-4">Resolution Result</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/80">
                  {pnlLedger.map((entry) => {
                    const resBadge = summarizeBlotterResolution(entry.resolution_result);
                    const allowConfirm = canConfirmBlotterResolution(
                      entry.resolution_result,
                      canResolveOutcomes
                    );
                    return (
                      <tr key={entry.ledger_id} className="hover:bg-slate-800/30 align-top">
                        <td className="py-2.5 px-4 font-mono">
                          <div className="text-slate-200">{entry.execution_id}</div>
                          <div className="text-[11px] text-slate-500">
                            {entry.pair_id} · {formatTimestampShort(entry.recorded_at)}
                          </div>
                        </td>
                        <td className="py-2.5 px-3 font-mono">
                          <span className={summarizeExecutionOutcome(entry.outcome_status).toneClass}>
                            {summarizeExecutionOutcome(entry.outcome_status).label}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono tabular-nums text-slate-200">
                          {formatShares(entry.matched_shares)} / {formatShares(entry.unwound_shares)}
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono tabular-nums text-amber-300">
                          {formatUsd(entry.fees_usd)}
                        </td>
                        <td
                          className={`py-2.5 px-3 text-right font-mono tabular-nums font-semibold ${
                            entry.realized_pnl_usd < 0
                              ? 'text-rose-400'
                              : entry.realized_pnl_usd > 0
                                ? 'text-emerald-400'
                                : 'text-slate-200'
                          }`}
                        >
                          {formatUsd(entry.realized_pnl_usd)}
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono tabular-nums text-sky-400">
                          {formatUsd(entry.locked_arb_edge_usd)}
                        </td>
                        <td className="py-2.5 px-4">
                          <div className="flex flex-wrap items-center gap-2">
                            <span
                              className={`inline-block px-2 py-0.5 border rounded font-mono text-[11px] font-semibold ${resBadge.toneClass}`}
                            >
                              {resBadge.label}
                            </span>
                            {resBadge.isPending ? (
                              <button
                                type="button"
                                disabled={!allowConfirm}
                                title={
                                  allowConfirm
                                    ? 'Manually confirm actual market settlement outcome'
                                    : 'Operator or Owner role required to confirm market outcomes'
                                }
                                onClick={() => openConfirmModal(entry)}
                                className="px-2.5 py-1 rounded border border-sky-700/80 bg-sky-950/40 hover:bg-sky-900/60 disabled:opacity-45 disabled:cursor-not-allowed text-[11px] font-semibold text-sky-200 transition-colors"
                              >
                                Confirm Outcome
                              </button>
                            ) : null}
                          </div>
                          {!resBadge.isPending ? (
                            <div className="mt-1 space-y-0.5 text-[11px] text-slate-400 font-mono">
                              <div>
                                {entry.resolved_by ? `by ${entry.resolved_by}` : 'confirmed'}
                                {entry.learning_cycle_id ? ` · ${entry.learning_cycle_id}` : ''}
                              </div>
                              {entry.arena_model_id || entry.linked_prediction_id ? (
                                <div className="text-sky-300/90">
                                  {[
                                    entry.arena_model_id ? `model ${entry.arena_model_id}` : null,
                                    entry.linked_prediction_id
                                      ? `forecast ${entry.linked_prediction_id}`
                                      : null,
                                  ]
                                    .filter(Boolean)
                                    .join(' · ')}
                                </div>
                              ) : null}
                              {entry.resolution_source ? (
                                <div
                                  className="text-slate-400 truncate max-w-[240px]"
                                  title={entry.resolution_source}
                                >
                                  src {entry.resolution_source}
                                </div>
                              ) : null}
                              {entry.resolution_notes ? (
                                <div
                                  className="text-slate-500 font-sans truncate max-w-[240px]"
                                  title={entry.resolution_notes}
                                >
                                  {entry.resolution_notes}
                                </div>
                              ) : null}
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )
          ) : null}
        </div>
      </section>

      {/* Section 4: Read-Only Official Settlement Verification & Operator Review Queue */}
      <SettlementVerificationPanel
        openPredictions={openPredictions}
        pendingLedgerEntries={pnlLedger.filter(
          (entry) => (entry.resolution_result ?? 'PENDING') === 'PENDING'
        )}
        canFetchSettlement={canSubmitResearch}
        canApproveSettlement={canResolveOutcomes}
        onSettlementFinalized={onRefreshCrossVenue}
      />

      {/* Manual Outcome Confirmation Modal */}
      {confirmingEntry ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-outcome-title"
          className="fixed inset-0 z-50 bg-black/75 flex items-center justify-center p-4"
        >
          <div className="w-full max-w-lg border border-slate-700 bg-[#111827] rounded-xl overflow-hidden shadow-2xl">
            <div className="px-5 py-4 border-b border-slate-800 flex items-center justify-between gap-3">
              <div>
                <h3 id="confirm-outcome-title" className="text-sm font-semibold text-slate-100">
                  Confirm Actual Market Outcome
                </h3>
                <p className="text-xs text-slate-400 mt-0.5">
                  Record verified settlement reality into the Learning Core and calibration loop.
                </p>
              </div>
              <button
                type="button"
                onClick={closeConfirmModal}
                disabled={resolveBusy}
                aria-label="Close confirmation dialog"
                className="px-2 py-1 text-xs text-slate-400 hover:text-slate-100 border border-slate-700 rounded"
              >
                Esc
              </button>
            </div>

            <form onSubmit={(e) => void handleConfirmResolution(e)} className="p-5 space-y-4 text-xs">
              <div className="grid grid-cols-2 gap-3 p-3 rounded-lg bg-[#0B111B] border border-slate-800 font-mono">
                <div>
                  <div className="text-[11px] text-slate-500">Ledger / Pair</div>
                  <div className="text-slate-200">{confirmingEntry.ledger_id}</div>
                  <div className="text-[11px] text-slate-400">{confirmingEntry.pair_id}</div>
                </div>
                <div className="text-right">
                  <div className="text-[11px] text-slate-500">Simulated Paper P&amp;L / Carry</div>
                  <div className="text-slate-200 tabular-nums">
                    {formatUsd(confirmingEntry.realized_pnl_usd)} /{' '}
                    <span className="text-sky-400">
                      {formatUsd(confirmingEntry.locked_arb_edge_usd)}
                    </span>
                  </div>
                  <div className="text-[11px] text-slate-400">
                    {summarizeExecutionOutcome(confirmingEntry.outcome_status).label}
                  </div>
                </div>
              </div>

              <div>
                <div className="text-slate-300 font-medium mb-1">
                  Actual Market Resolution Result
                </div>
                <p className="text-[11px] text-slate-400 mb-2">
                  YES / NO is evaluated relative to the forecast&apos;s binary YES proposition (not paper buy/sell leg side). VOID is never scored or trained as a mistake.
                </p>
                <div className="grid grid-cols-3 gap-2">
                  {(
                    [
                      ['YES', 'Proposition YES', 'border-emerald-600 bg-emerald-950/50 text-emerald-200'],
                      ['NO', 'Proposition NO', 'border-rose-600 bg-rose-950/50 text-rose-200'],
                      ['VOID', 'Void (Unscored)', 'border-amber-600 bg-amber-950/50 text-amber-200'],
                    ] as Array<['YES' | 'NO' | 'VOID', string, string]>
                  ).map(([val, desc, activeCls]) => {
                    const active = selectedResolution === val;
                    return (
                      <button
                        key={val}
                        type="button"
                        aria-pressed={active}
                        onClick={() => setSelectedResolution(val)}
                        className={`p-2.5 rounded-lg border text-left transition-colors ${
                          active
                            ? activeCls
                            : 'border-slate-700 bg-[#0B111B] text-slate-300 hover:border-slate-500'
                        }`}
                      >
                        <div className="font-mono font-semibold text-sm">{val}</div>
                        <div className="text-[11px] opacity-80">{desc}</div>
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label htmlFor="resolve-source" className="block text-slate-300 mb-1">
                    Settlement Source (Optional)
                  </label>
                  <input
                    id="resolve-source"
                    type="text"
                    maxLength={160}
                    placeholder="e.g. polymarket_uma_oracle"
                    value={resolutionSource}
                    onChange={(e) => setResolutionSource(e.target.value)}
                    className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono text-xs"
                  />
                </div>

                <div>
                  <label htmlFor="resolve-linked-pred" className="block text-slate-300 mb-1">
                    Link Open Forecast ID (Optional)
                  </label>
                  {openPredictions.length > 0 ? (
                    <select
                      id="resolve-linked-pred"
                      value={linkedPredictionId}
                      onChange={(e) => setLinkedPredictionId(e.target.value)}
                      className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono text-xs"
                    >
                      <option value="">None (blotter learning cycle only)</option>
                      {openPredictions.map((pred) => (
                        <option key={pred.prediction_id} value={pred.prediction_id}>
                          {pred.prediction_id} · {pred.market_id} · {pred.agent_id} ({(pred.predicted_prob * 100).toFixed(0)}%)
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      id="resolve-linked-pred"
                      type="text"
                      maxLength={64}
                      placeholder="e.g. PRED-..."
                      value={linkedPredictionId}
                      onChange={(e) => setLinkedPredictionId(e.target.value)}
                      className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono text-xs"
                    />
                  )}
                </div>
              </div>

              {selectedLinkedPrediction ? (
                <div className="p-3 rounded-lg bg-[#0B111B] border border-sky-900/70 space-y-1 text-[11px]">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sky-300 font-semibold">
                      Authoritative Forecast Attribution (Read-Only)
                    </span>
                    <span className="font-mono text-slate-300">
                      {formatProbPct(selectedLinkedPrediction.predicted_prob)}
                      {selectedLinkedPrediction.predicted_direction
                        ? ` (${selectedLinkedPrediction.predicted_direction})`
                        : ''}
                    </span>
                  </div>
                  <div className="font-mono text-slate-200">
                    Model: {formatArenaAttribution(selectedLinkedPrediction)}
                  </div>
                  <div className="font-mono text-slate-400">
                    Market {selectedLinkedPrediction.market_id}
                    {selectedLinkedPrediction.timeframe
                      ? ` · Window ${selectedLinkedPrediction.timeframe}`
                      : ''}
                    {selectedLinkedPrediction.dataset_snapshot_id
                      ? ` · Snap ${selectedLinkedPrediction.dataset_snapshot_id}`
                      : ''}
                  </div>
                  <div className="text-slate-300">{selectedLinkedPrediction.question}</div>
                </div>
              ) : null}

              <div>
                <label htmlFor="resolve-notes" className="block text-slate-300 mb-1">
                  Verification Notes (Optional)
                </label>
                <textarea
                  id="resolve-notes"
                  rows={2}
                  maxLength={600}
                  placeholder="Official exchange settlement reference, timestamp, or discrepancy notes..."
                  value={resolutionNotes}
                  onChange={(e) => setResolutionNotes(e.target.value)}
                  className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 text-xs"
                />
              </div>

              <label className="flex items-start gap-2.5 p-3 rounded-lg border border-amber-800/60 bg-amber-950/20 text-amber-200 cursor-pointer">
                <input
                  type="checkbox"
                  checked={confirmChecked}
                  onChange={(e) => setConfirmChecked(e.target.checked)}
                  className="mt-0.5 accent-sky-500"
                />
                <span className="leading-relaxed">
                  I manually confirm that <span className="font-mono font-semibold">{selectedResolution}</span>{' '}
                  reflects the actual market settlement outcome. This atomic action is final, writes
                  a verified Learning Core cycle, and does not alter Risk Gate limits.
                </span>
              </label>

              {resolveError ? (
                <div
                  role="alert"
                  className="p-3 rounded-lg bg-rose-950/40 border border-rose-800 text-xs text-rose-200"
                >
                  {resolveError}
                </div>
              ) : null}

              <div className="flex items-center justify-end gap-2 pt-1">
                <button
                  type="button"
                  onClick={closeConfirmModal}
                  disabled={resolveBusy}
                  className="px-3 py-2 rounded-lg border border-slate-700 bg-slate-800 hover:bg-slate-700 text-slate-200 font-medium"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={!confirmChecked || resolveBusy}
                  className="px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-500 disabled:opacity-45 disabled:cursor-not-allowed text-white font-semibold"
                >
                  {resolveBusy ? 'Recording Outcome...' : `Confirm ${selectedResolution} Outcome`}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
}
