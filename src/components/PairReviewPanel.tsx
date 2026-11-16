import { useMemo, useState } from 'react';
import { certaintyApi } from '../api/client';
import type {
  CrossVenuePairRecord,
  PairDiscoveryResponse,
  PairReviewResponse,
  PairReviewState,
} from '../types/certainty';
import { formatTimestampShort } from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface PairReviewPanelProps {
  pairs: CrossVenuePairRecord[];
  canManagePairs: boolean;
  onRefreshCrossVenue: () => Promise<void>;
}

type ReviewFilter = PairReviewState | 'ALL';

const FILTERS: { id: ReviewFilter; label: string }[] = [
  { id: 'PENDING_REVIEW', label: 'Pending' },
  { id: 'CONFIRMED', label: 'Confirmed' },
  { id: 'REJECTED', label: 'Rejected' },
  { id: 'ALL', label: 'All' },
];

export function reviewStateOf(pair: CrossVenuePairRecord): PairReviewState {
  if (pair.review_state) return pair.review_state;
  if ((pair.notes || '').startsWith('REJECTED_BY_REVIEW')) return 'REJECTED';
  return pair.identical_settlement_verified ? 'CONFIRMED' : 'PENDING_REVIEW';
}

function stateBadgeClass(state: PairReviewState): string {
  if (state === 'CONFIRMED') return 'text-emerald-300 bg-emerald-950/40 border-emerald-900';
  if (state === 'REJECTED') return 'text-rose-300 bg-rose-950/40 border-rose-900';
  return 'text-amber-200 bg-amber-950/40 border-amber-900';
}

function stateLabel(state: PairReviewState): string {
  if (state === 'CONFIRMED') return 'Confirmed';
  if (state === 'REJECTED') return 'Rejected';
  return 'Pending review';
}

function RulesBlock({ label, text }: { label: string; text: string }) {
  return (
    <div className="space-y-1">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <p className="text-xs text-slate-200 whitespace-pre-line leading-relaxed">
        {text.trim() ? text : <span className="text-slate-500">Not provided by venue</span>}
      </p>
    </div>
  );
}

export function PairReviewPanel({ pairs, canManagePairs, onRefreshCrossVenue }: PairReviewPanelProps) {
  const [filter, setFilter] = useState<ReviewFilter>('PENDING_REVIEW');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [review, setReview] = useState<PairReviewResponse | null>(null);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);

  const [predicateOk, setPredicateOk] = useState(false);
  const [sourceOk, setSourceOk] = useState(false);
  const [voidOk, setVoidOk] = useState(false);
  const [earlyCloseOk, setEarlyCloseOk] = useState(false);
  const [reviewNote, setReviewNote] = useState('');
  const [actionBusy, setActionBusy] = useState<'confirm' | 'reject' | null>(null);
  const [actionMessage, setActionMessage] = useState<{ tone: 'ok' | 'warn' | 'error'; text: string } | null>(null);

  const [discoverBusy, setDiscoverBusy] = useState(false);
  const [discoverResult, setDiscoverResult] = useState<PairDiscoveryResponse | null>(null);
  const [discoverError, setDiscoverError] = useState<string | null>(null);

  const counts = useMemo(() => {
    const c = { PENDING_REVIEW: 0, CONFIRMED: 0, REJECTED: 0, ALL: pairs.length };
    for (const p of pairs) c[reviewStateOf(p)] += 1;
    return c;
  }, [pairs]);

  const visiblePairs = useMemo(
    () => (filter === 'ALL' ? pairs : pairs.filter((p) => reviewStateOf(p) === filter)),
    [pairs, filter]
  );

  const resetAttestations = (pair: CrossVenuePairRecord | null) => {
    setPredicateOk(Boolean(pair?.predicate_verified));
    setSourceOk(Boolean(pair?.resolution_source_verified));
    setVoidOk(Boolean(pair?.void_rules_compatible));
    setEarlyCloseOk(Boolean(pair?.allow_early_close_variance));
    setReviewNote('');
  };

  const openReview = async (pair: CrossVenuePairRecord) => {
    setSelectedId(pair.pair_id);
    setReview(null);
    setReviewError(null);
    setActionMessage(null);
    resetAttestations(pair);
    setReviewLoading(true);
    try {
      setReview(await certaintyApi.getCrossVenuePairReview(pair.pair_id));
    } catch (err) {
      setReviewError(err instanceof Error ? err.message : 'Failed to load live settlement rules');
    } finally {
      setReviewLoading(false);
    }
  };

  const handleDiscover = async () => {
    setDiscoverBusy(true);
    setDiscoverError(null);
    try {
      const res = await certaintyApi.discoverCrossVenuePairs({ persist: true });
      setDiscoverResult(res);
      setFilter('PENDING_REVIEW');
      await onRefreshCrossVenue();
    } catch (err) {
      setDiscoverError(err instanceof Error ? err.message : 'Pair discovery failed');
    } finally {
      setDiscoverBusy(false);
    }
  };

  const allAttested = predicateOk && sourceOk && voidOk && earlyCloseOk;
  const noteOk = reviewNote.trim().length >= 3;
  const selectedPair = review?.pair ?? pairs.find((p) => p.pair_id === selectedId) ?? null;

  const handleConfirm = async () => {
    if (!selectedPair || !allAttested || !noteOk) return;
    setActionBusy('confirm');
    setActionMessage(null);
    try {
      const res = await certaintyApi.registerCrossVenuePair({
        pair_id: selectedPair.pair_id,
        poly_market_id: selectedPair.poly_market_id,
        kalshi_ticker: selectedPair.kalshi_ticker,
        resolution_source_verified: sourceOk,
        predicate_verified: predicateOk,
        polarity_inverted: false,
        void_rules_compatible: voidOk,
        allow_early_close_variance: earlyCloseOk,
        notes: `HUMAN_REVIEWED: ${reviewNote.trim()}`,
      });
      if (res.assessment.identical_settlement_verified) {
        setActionMessage({ tone: 'ok', text: 'Pair confirmed. The scanner will now evaluate it first, and the Risk Gate can consider it.' });
      } else {
        setActionMessage({
          tone: 'warn',
          text: `Saved, but the matcher still blocks this pair: ${res.assessment.mismatch_reasons.join('; ') || res.assessment.status}`,
        });
      }
      await onRefreshCrossVenue();
      setReview(await certaintyApi.getCrossVenuePairReview(selectedPair.pair_id));
    } catch (err) {
      setActionMessage({ tone: 'error', text: err instanceof Error ? err.message : 'Confirmation failed' });
    } finally {
      setActionBusy(null);
    }
  };

  const handleReject = async () => {
    if (!selectedPair || !noteOk) return;
    setActionBusy('reject');
    setActionMessage(null);
    try {
      await certaintyApi.rejectCrossVenuePair(selectedPair.pair_id, reviewNote.trim());
      setActionMessage({ tone: 'ok', text: 'Pair rejected. It will be skipped by the scanner and not re-added by discovery.' });
      resetAttestations(null);
      await onRefreshCrossVenue();
      setReview(await certaintyApi.getCrossVenuePairReview(selectedPair.pair_id));
    } catch (err) {
      setActionMessage({ tone: 'error', text: err instanceof Error ? err.message : 'Rejection failed' });
    } finally {
      setActionBusy(null);
    }
  };

  const poly = review?.polymarket ?? null;
  const kalshi = review?.kalshi ?? null;
  const currentState = review ? review.review_state : selectedPair ? reviewStateOf(selectedPair) : null;

  return (
    <section className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden" aria-label="Discovered pair review">
      <div className="px-5 py-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <h2 className="text-sm font-semibold text-slate-100">Discovered Pair Review</h2>
            <ProvenanceLabel provenance="LIVE" />
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Compare each venue's settlement rules side by side. Only pairs you confirm can pass the Risk Gate.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void handleDiscover()}
          disabled={discoverBusy || !canManagePairs}
          title={canManagePairs ? 'Find candidate pairs across Polymarket and Kalshi' : 'Requires OPERATOR or OWNER role'}
          className="px-3.5 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors whitespace-nowrap"
        >
          {discoverBusy ? 'Discovering pairs…' : !canManagePairs ? 'Operator Required' : 'Discover pairs'}
        </button>
      </div>

      {discoverError ? (
        <div className="mx-5 mt-4 p-3 rounded-lg border border-rose-900 bg-rose-950/30 text-xs text-rose-300">{discoverError}</div>
      ) : null}
      {discoverResult ? (
        <div className="mx-5 mt-4 p-3 rounded-lg border border-slate-800 bg-[#0E1522] text-xs text-slate-300 flex flex-wrap items-center justify-between gap-2">
          <span>
            Scanned {discoverResult.polymarket_markets_scanned.toLocaleString()} Polymarket and{' '}
            {discoverResult.kalshi_markets_scanned.toLocaleString()} Kalshi markets · {discoverResult.candidates_found} candidates ·{' '}
            {discoverResult.registered_count} newly added for review
            {discoverResult.warnings.length ? ` · ${discoverResult.warnings.join('; ')}` : ''}
          </span>
          <button type="button" onClick={() => setDiscoverResult(null)} className="text-slate-400 hover:text-slate-200">
            Dismiss
          </button>
        </div>
      ) : null}

      <div className="grid grid-cols-1 xl:grid-cols-12">
        {/* Queue */}
        <div className="xl:col-span-4 border-b xl:border-b-0 xl:border-r border-slate-800 p-4 space-y-3">
          <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Review filter">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                role="tab"
                aria-selected={filter === f.id}
                onClick={() => setFilter(f.id)}
                className={`px-2.5 py-1 rounded-md text-[11px] font-medium border transition-colors ${
                  filter === f.id
                    ? 'bg-slate-700 border-slate-600 text-slate-100'
                    : 'bg-transparent border-slate-800 text-slate-400 hover:text-slate-200'
                }`}
              >
                {f.label} <span className="tabular-nums text-slate-400">{counts[f.id]}</span>
              </button>
            ))}
          </div>

          {visiblePairs.length === 0 ? (
            <div className="p-6 text-center border border-dashed border-slate-800 rounded-lg text-xs text-slate-400">
              {pairs.length === 0
                ? 'No pairs yet. Use Discover pairs to find candidates across both venues.'
                : 'No pairs in this group.'}
            </div>
          ) : (
            <ul className="space-y-1.5 max-h-[34rem] overflow-y-auto pr-1">
              {visiblePairs.map((p) => {
                const state = reviewStateOf(p);
                const active = p.pair_id === selectedId;
                return (
                  <li key={p.pair_id}>
                    <button
                      type="button"
                      onClick={() => void openReview(p)}
                      className={`w-full text-left p-2.5 rounded-lg border transition-colors ${
                        active ? 'border-sky-700 bg-sky-950/30' : 'border-slate-800 hover:border-slate-700 bg-[#0E1522]'
                      }`}
                    >
                      <div className="text-xs text-slate-100 leading-snug">{p.poly_question}</div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px]">
                        <span className={`px-1.5 py-0.5 rounded border ${stateBadgeClass(state)}`}>{stateLabel(state)}</span>
                        <span className="font-mono text-slate-400">{p.kalshi_ticker}</span>
                        {p.cutoff_delta_hours !== null ? (
                          <span className="font-mono text-slate-500">Δ {p.cutoff_delta_hours}h</span>
                        ) : null}
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Detail */}
        <div className="xl:col-span-8 p-5 space-y-4 min-w-0">
          {!selectedId ? (
            <div className="p-10 text-center border border-dashed border-slate-800 rounded-lg text-xs text-slate-400">
              Select a pair to compare its settlement rules.
            </div>
          ) : reviewLoading ? (
            <div className="p-10 text-center text-xs text-slate-400">Loading live settlement rules from both venues…</div>
          ) : reviewError ? (
            <div className="p-3 rounded-lg border border-rose-900 bg-rose-950/30 text-xs text-rose-300">{reviewError}</div>
          ) : review ? (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="font-mono text-xs text-slate-300 break-all">{review.pair.pair_id}</div>
                {currentState ? (
                  <span className={`px-2 py-0.5 rounded border text-[11px] ${stateBadgeClass(currentState)}`}>
                    {stateLabel(currentState)}
                  </span>
                ) : null}
              </div>

              {review.errors.length ? (
                <div className="p-3 rounded-lg border border-amber-900 bg-amber-950/30 text-xs text-amber-200">
                  {review.errors.join(' · ')}
                </div>
              ) : null}

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div className="p-4 rounded-lg border border-slate-800 bg-[#0E1522] space-y-3 min-w-0">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-violet-300">Polymarket</span>
                    {poly?.url ? (
                      <a href={poly.url} target="_blank" rel="noreferrer" className="text-[11px] text-sky-400 hover:underline">
                        Open market
                      </a>
                    ) : null}
                  </div>
                  {poly ? (
                    <>
                      <div className="text-sm text-slate-100 leading-snug">{poly.question}</div>
                      <RulesBlock label="Resolution rules" text={poly.rules_text} />
                      <RulesBlock label="Resolution source" text={poly.resolution_source} />
                      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                        <dt className="text-slate-500">Ends</dt>
                        <dd className="text-slate-200 font-mono">{formatTimestampShort(poly.end_date)}</dd>
                        <dt className="text-slate-500">Multi-outcome group</dt>
                        <dd className="text-slate-200">{poly.neg_risk ? 'Yes' : 'No'}</dd>
                      </dl>
                    </>
                  ) : (
                    <div className="text-xs text-slate-500">Unavailable</div>
                  )}
                </div>

                <div className="p-4 rounded-lg border border-slate-800 bg-[#0E1522] space-y-3 min-w-0">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-semibold text-teal-300">Kalshi</span>
                    <div className="flex gap-3">
                      {kalshi?.contract_url ? (
                        <a href={kalshi.contract_url} target="_blank" rel="noreferrer" className="text-[11px] text-sky-400 hover:underline">
                          Contract terms
                        </a>
                      ) : null}
                      {kalshi?.url ? (
                        <a href={kalshi.url} target="_blank" rel="noreferrer" className="text-[11px] text-sky-400 hover:underline">
                          Open market
                        </a>
                      ) : null}
                    </div>
                  </div>
                  {kalshi ? (
                    <>
                      <div className="text-sm text-slate-100 leading-snug">{kalshi.title}</div>
                      <RulesBlock label="Primary rules" text={kalshi.rules_primary} />
                      {kalshi.rules_secondary.trim() ? <RulesBlock label="Secondary rules" text={kalshi.rules_secondary} /> : null}
                      <div className="space-y-1">
                        <div className="text-[11px] uppercase tracking-wide text-slate-500">Settlement sources</div>
                        {kalshi.settlement_sources.length ? (
                          <ul className="text-xs text-slate-200 space-y-0.5">
                            {kalshi.settlement_sources.map((s) => (
                              <li key={`${s.name}-${s.url}`}>
                                {s.url ? (
                                  <a href={s.url} target="_blank" rel="noreferrer" className="text-sky-400 hover:underline">
                                    {s.name || s.url}
                                  </a>
                                ) : (
                                  s.name
                                )}
                              </li>
                            ))}
                          </ul>
                        ) : (
                          <p className="text-xs text-slate-500">Not provided by venue</p>
                        )}
                      </div>
                      {kalshi.can_close_early ? (
                        <RulesBlock label="Early close condition" text={kalshi.early_close_condition} />
                      ) : null}
                      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                        <dt className="text-slate-500">Closes</dt>
                        <dd className="text-slate-200 font-mono">{formatTimestampShort(kalshi.close_time)}</dd>
                        <dt className="text-slate-500">Expires</dt>
                        <dd className="text-slate-200 font-mono">{formatTimestampShort(kalshi.expiration_time)}</dd>
                        <dt className="text-slate-500">Can close early</dt>
                        <dd className={kalshi.can_close_early ? 'text-amber-300' : 'text-slate-200'}>
                          {kalshi.can_close_early ? 'Yes' : 'No'}
                        </dd>
                      </dl>
                    </>
                  ) : (
                    <div className="text-xs text-slate-500">Unavailable</div>
                  )}
                </div>
              </div>

              {review.assessment ? (
                <div className="p-4 rounded-lg border border-slate-800 bg-[#0E1522] space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="font-medium text-slate-300">Settlement matcher checks</span>
                    <span className="font-mono text-slate-400">
                      Closing-time gap:{' '}
                      {review.assessment.cutoff_delta_hours !== null ? `${review.assessment.cutoff_delta_hours}h` : 'N/A'} ·{' '}
                      <span className={review.assessment.identical_settlement_verified ? 'text-emerald-400' : 'text-rose-400'}>
                        {review.assessment.status}
                      </span>
                    </span>
                  </div>
                  <ul className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1">
                    {review.assessment.checks.map((chk) => (
                      <li key={chk.check_id} className="flex items-start justify-between gap-2 text-[11px]">
                        <span className="text-slate-300" title={chk.detail}>{chk.detail}</span>
                        <span className={`font-mono shrink-0 ${chk.passed ? 'text-emerald-400' : 'text-rose-400'}`}>
                          {chk.passed ? 'PASS' : 'FAIL'}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              <div className="p-4 rounded-lg border border-slate-800 bg-[#0E1522] space-y-3">
                <div className="text-xs font-medium text-slate-300">Your review</div>
                <fieldset className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs text-slate-300" disabled={!canManagePairs}>
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input type="checkbox" checked={predicateOk} onChange={(e) => setPredicateOk(e.target.checked)} className="mt-0.5 rounded border-slate-700 bg-[#0B111B]" />
                    <span>Both markets resolve YES on the same event</span>
                  </label>
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input type="checkbox" checked={sourceOk} onChange={(e) => setSourceOk(e.target.checked)} className="mt-0.5 rounded border-slate-700 bg-[#0B111B]" />
                    <span>Resolution sources are equivalent</span>
                  </label>
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input type="checkbox" checked={voidOk} onChange={(e) => setVoidOk(e.target.checked)} className="mt-0.5 rounded border-slate-700 bg-[#0B111B]" />
                    <span>Void and cancellation rules are compatible</span>
                  </label>
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input type="checkbox" checked={earlyCloseOk} onChange={(e) => setEarlyCloseOk(e.target.checked)} className="mt-0.5 rounded border-slate-700 bg-[#0B111B]" />
                    <span>Early-close behavior is acceptable</span>
                  </label>
                </fieldset>
                <label className="block space-y-1 text-xs">
                  <span className="text-slate-400">Review note (required)</span>
                  <input
                    type="text"
                    value={reviewNote}
                    onChange={(e) => setReviewNote(e.target.value)}
                    disabled={!canManagePairs}
                    maxLength={400}
                    placeholder="e.g. Both resolve on official Democratic Party nomination; same window"
                    className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-sky-500"
                  />
                </label>

                {actionMessage ? (
                  <div
                    className={`p-3 rounded-lg border text-xs ${
                      actionMessage.tone === 'ok'
                        ? 'border-emerald-900 bg-emerald-950/30 text-emerald-300'
                        : actionMessage.tone === 'warn'
                          ? 'border-amber-900 bg-amber-950/30 text-amber-200'
                          : 'border-rose-900 bg-rose-950/30 text-rose-300'
                    }`}
                  >
                    {actionMessage.text}
                  </div>
                ) : null}

                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void handleConfirm()}
                    disabled={!canManagePairs || !allAttested || !noteOk || actionBusy !== null}
                    title={!allAttested ? 'Tick all four checks to confirm' : !noteOk ? 'Add a review note' : 'Confirm settlement equivalence'}
                    className="px-4 py-2 text-xs font-semibold text-white bg-emerald-700 hover:bg-emerald-600 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
                  >
                    {actionBusy === 'confirm' ? 'Confirming…' : 'Confirm pair'}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleReject()}
                    disabled={!canManagePairs || !noteOk || actionBusy !== null || currentState === 'REJECTED'}
                    title={!noteOk ? 'Add a review note explaining the rejection' : 'Reject this pair'}
                    className="px-4 py-2 text-xs font-semibold text-rose-100 bg-rose-800/80 hover:bg-rose-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
                  >
                    {actionBusy === 'reject' ? 'Rejecting…' : 'Reject pair'}
                  </button>
                  {!canManagePairs ? (
                    <span className="self-center text-[11px] text-slate-500">Reviewing requires OPERATOR or OWNER role.</span>
                  ) : null}
                </div>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </section>
  );
}
