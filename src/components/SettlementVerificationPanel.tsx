import { useCallback, useEffect, useState } from 'react';
import { certaintyApi } from '../api/client';
import type {
  PaperPnlLedgerRecord,
  PredictionRecord,
  SettlementQueueItem,
  SettlementQueueStatus,
} from '../types/certainty';
import { formatScoreMetric, formatTimestampShort } from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface SettlementVerificationPanelProps {
  openPredictions: PredictionRecord[];
  pendingLedgerEntries: PaperPnlLedgerRecord[];
  canFetchSettlement: boolean;
  canApproveSettlement: boolean;
  onSettlementFinalized: () => Promise<void>;
}

const STATUS_STYLE: Record<SettlementQueueStatus, string> = {
  PENDING_OPERATOR_REVIEW: 'text-sky-300',
  APPROVED_AND_FINALIZED: 'text-emerald-400',
  DISPUTED: 'text-rose-400',
  CONFLICTING_EVIDENCE: 'text-rose-400',
  AMBIGUOUS: 'text-amber-300',
  UNVERIFIED: 'text-amber-300',
  REJECTED: 'text-slate-400',
};

export function SettlementVerificationPanel({
  openPredictions,
  pendingLedgerEntries,
  canFetchSettlement,
  canApproveSettlement,
  onSettlementFinalized,
}: SettlementVerificationPanelProps) {
  const [items, setItems] = useState<SettlementQueueItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Read-only fetch form state
  const [venue, setVenue] = useState<'polymarket' | 'kalshi'>('polymarket');
  const [marketId, setMarketId] = useState<string>('');
  const [linkedPredictionId, setLinkedPredictionId] = useState<string>('');
  const [linkedLedgerId, setLinkedLedgerId] = useState<string>('');
  const [secondaryVenue, setSecondaryVenue] = useState<'' | 'polymarket' | 'kalshi'>('');
  const [secondaryMarketId, setSecondaryMarketId] = useState<string>('');

  // Inline approval confirmation state
  const [confirmingQueueId, setConfirmingQueueId] = useState<string | null>(null);
  const [approveChecked, setApproveChecked] = useState<boolean>(false);
  const [reviewNotes, setReviewNotes] = useState<string>('');

  const loadQueue = useCallback(async () => {
    setLoading(true);
    try {
      const res = await certaintyApi.getSettlementQueue({ limit: 50 });
      setItems(res.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load settlement queue');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadQueue();
  }, [loadQueue]);

  const handleFetchSettlement = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!marketId.trim() || busy) return;
    setBusy('fetch');
    setError(null);
    setNotice(null);
    try {
      const res = await certaintyApi.fetchOfficialSettlement({
        venue,
        market_id: marketId.trim(),
        linked_prediction_id: linkedPredictionId || null,
        linked_ledger_id: linkedLedgerId || null,
        secondary_venue: secondaryVenue || null,
        secondary_market_id: secondaryVenue && secondaryMarketId.trim() ? secondaryMarketId.trim() : null,
      });
      setNotice(
        `Queued ${res.queue_item.queue_id} (${res.queue_item.venue} ${res.queue_item.market_id}) -> ${res.queue_item.verification_status} (${res.queue_item.proposed_outcome}). Auto-finalization disabled.`
      );
      setMarketId('');
      setSecondaryMarketId('');
      await loadQueue();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Official settlement lookup failed');
    } finally {
      setBusy(null);
    }
  };

  const handleReviewAction = async (
    item: SettlementQueueItem,
    action: 'APPROVE' | 'REJECT' | 'MARK_DISPUTED'
  ) => {
    if (busy) return;
    setBusy(`${action}-${item.queue_id}`);
    setError(null);
    setNotice(null);
    try {
      const res = await certaintyApi.reviewSettlementQueueItem(item.queue_id, {
        action,
        operator_confirmed: action === 'APPROVE' ? approveChecked : false,
        notes: reviewNotes.trim() || undefined,
      });
      const brierNote =
        res.resolved_prediction && res.resolved_prediction.brier_score !== null
          ? ` · Scored Brier ${formatScoreMetric(res.resolved_prediction.brier_score, 4)}`
          : '';
      setNotice(
        `Settlement ${res.queue_item.queue_id} updated to ${res.queue_item.verification_status}${brierNote}.`
      );
      setConfirmingQueueId(null);
      setApproveChecked(false);
      setReviewNotes('');
      await loadQueue();
      if (action === 'APPROVE') {
        await onSettlementFinalized();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action.toLowerCase()} settlement`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section
      data-testid="settlement-verification-panel"
      className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden"
    >
      <div className="px-5 py-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2.5">
            <h2 className="text-base font-semibold text-slate-100">
              Official Settlement Verification Queue
            </h2>
            <ProvenanceLabel
              provenance="LIVE"
              detail="Read-Only Venue Adapters · Operator Approval Required"
            />
          </div>
          <p className="mt-0.5 text-xs text-slate-400">
            Retrieves official Polymarket Gamma &amp; Kalshi v2 settlement outcomes into an auditable review queue.
            Disputed, ambiguous, conflicting, or unverified settlements are blocked from finalization.
          </p>
        </div>

        <button
          type="button"
          onClick={() => void loadQueue()}
          disabled={loading}
          className="px-3 py-1.5 text-xs font-medium text-slate-200 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg whitespace-nowrap"
        >
          {loading ? 'Loading...' : `Refresh Queue (${items.length})`}
        </button>
      </div>

      <div className="p-5 space-y-4">
        {/* Read-only Official Settlement Lookup Form */}
        <form
          onSubmit={(e) => void handleFetchSettlement(e)}
          className="grid grid-cols-1 md:grid-cols-12 gap-3 p-3.5 rounded-lg bg-[#0E1522] border border-slate-800 text-xs items-end"
        >
          <div className="md:col-span-2">
            <label htmlFor="settle-venue-select" className="block text-slate-400 mb-1">
              Primary Venue
            </label>
            <select
              id="settle-venue-select"
              value={venue}
              onChange={(e) => setVenue(e.target.value as 'polymarket' | 'kalshi')}
              className="w-full px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            >
              <option value="polymarket">polymarket</option>
              <option value="kalshi">kalshi</option>
            </select>
          </div>

          <div className="md:col-span-3">
            <label htmlFor="settle-market-input" className="block text-slate-400 mb-1">
              Official Market ID / Ticker
            </label>
            <input
              id="settle-market-input"
              type="text"
              required
              value={marketId}
              onChange={(e) => setMarketId(e.target.value)}
              placeholder="e.g. 253591 or KX..."
              className="w-full px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            />
          </div>

          <div className="md:col-span-3">
            <label htmlFor="settle-pred-select" className="block text-slate-400 mb-1">
              Link Open Forecast (Optional)
            </label>
            <select
              id="settle-pred-select"
              value={linkedPredictionId}
              onChange={(e) => setLinkedPredictionId(e.target.value)}
              className="w-full px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            >
              <option value="">None</option>
              {openPredictions.map((p) => (
                <option key={p.prediction_id} value={p.prediction_id}>
                  {p.prediction_id} · {p.market_id}
                </option>
              ))}
            </select>
          </div>

          <div className="md:col-span-2">
            <label htmlFor="settle-ledger-select" className="block text-slate-400 mb-1">
              Link Blotter Row (Optional)
            </label>
            <select
              id="settle-ledger-select"
              value={linkedLedgerId}
              onChange={(e) => setLinkedLedgerId(e.target.value)}
              className="w-full px-2.5 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 font-mono"
            >
              <option value="">None</option>
              {pendingLedgerEntries.map((row) => (
                <option key={row.ledger_id} value={row.ledger_id}>
                  {row.ledger_id} ({row.pair_id})
                </option>
              ))}
            </select>
          </div>

          <div className="md:col-span-2">
            <button
              type="submit"
              disabled={busy !== null || !canFetchSettlement || !marketId.trim()}
              title={canFetchSettlement ? 'Fetch read-only official settlement into review queue' : 'Requires ANALYST role or higher'}
              className="w-full px-3 py-2 font-semibold bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg whitespace-nowrap"
            >
              {busy === 'fetch' ? 'Fetching...' : 'Fetch to Queue'}
            </button>
          </div>

          <div className="md:col-span-12 pt-1 border-t border-slate-800/80 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
            <span>Cross-Venue Corroboration (Optional):</span>
            <select
              aria-label="Secondary venue for conflict check"
              value={secondaryVenue}
              onChange={(e) => setSecondaryVenue(e.target.value as '' | 'polymarket' | 'kalshi')}
              className="px-2 py-1 bg-[#0B111B] border border-slate-700 rounded text-slate-200 font-mono"
            >
              <option value="">None</option>
              <option value="polymarket">polymarket</option>
              <option value="kalshi">kalshi</option>
            </select>
            {secondaryVenue ? (
              <input
                type="text"
                aria-label="Secondary market ID"
                value={secondaryMarketId}
                onChange={(e) => setSecondaryMarketId(e.target.value)}
                placeholder="Secondary market ID / ticker"
                className="px-2 py-1 bg-[#0B111B] border border-slate-700 rounded text-slate-100 font-mono w-56"
              />
            ) : null}
          </div>
        </form>

        {notice ? (
          <div
            role="status"
            className="px-3.5 py-2.5 rounded-lg border border-sky-800/70 bg-sky-950/30 text-xs text-sky-200 flex items-center justify-between gap-3"
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
            className="px-3.5 py-2.5 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200 flex items-center justify-between gap-3"
          >
            <span>{error}</span>
            <button type="button" onClick={() => setError(null)} className="text-rose-300 hover:text-white">
              Dismiss
            </button>
          </div>
        ) : null}

        {/* Review Queue Table */}
        {items.length === 0 ? (
          <div className="p-6 text-center rounded-lg bg-[#0E1522] border border-slate-800 text-xs text-slate-500">
            No official settlements queued yet. Fetch a Polymarket ID or Kalshi ticker above to retrieve read-only official evidence for operator review.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-slate-800 text-slate-400 font-medium">
                  <th className="py-2 pr-3">Queue / Market</th>
                  <th className="py-2 px-3">Proposed</th>
                  <th className="py-2 px-3">Verification Gate</th>
                  <th className="py-2 px-3">Source &amp; Timestamp</th>
                  <th className="py-2 pl-3 text-right">Operator Review</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {items.map((item) => {
                  const isFinalizable =
                    item.verification_status === 'PENDING_OPERATOR_REVIEW' &&
                    item.conflict_reasons.length === 0 &&
                    (Boolean(item.linked_prediction_id) || Boolean(item.linked_ledger_id));
                  const isTerminal =
                    item.verification_status === 'APPROVED_AND_FINALIZED' ||
                    item.verification_status === 'REJECTED';
                  const isConfirming = confirmingQueueId === item.queue_id;

                  return (
                    <tr key={item.queue_id} className="align-top">
                      <td className="py-2.5 pr-3 font-mono">
                        <div className="font-semibold text-slate-100">{item.queue_id}</div>
                        <div className="text-[11px] text-slate-400">
                          {item.venue} · {item.market_id}
                        </div>
                        <div className="text-[11px] text-sky-300/80">
                          {item.linked_prediction_id ? `Pred ${item.linked_prediction_id}` : ''}
                          {item.linked_prediction_id && item.linked_ledger_id ? ' · ' : ''}
                          {item.linked_ledger_id ? `Ledger ${item.linked_ledger_id}` : ''}
                        </div>
                      </td>
                      <td className="py-2.5 px-3 font-mono font-semibold text-slate-100">
                        {item.proposed_outcome}
                      </td>
                      <td className="py-2.5 px-3 max-w-xs">
                        <div
                          className={`font-mono font-semibold ${
                            STATUS_STYLE[item.verification_status] ?? 'text-slate-300'
                          }`}
                        >
                          {item.verification_status}
                        </div>
                        {item.conflict_reasons.length > 0 ? (
                          <div className="mt-1 text-[11px] text-rose-300/90 leading-snug">
                            {item.conflict_reasons[0]}
                          </div>
                        ) : null}
                      </td>
                      <td className="py-2.5 px-3 font-mono text-[11px] text-slate-300">
                        <div className="truncate max-w-[200px]" title={item.source_name}>
                          {item.source_name}
                        </div>
                        <div className="text-slate-500">
                          {item.settlement_timestamp
                            ? formatTimestampShort(item.settlement_timestamp)
                            : 'Unsettled'}
                        </div>
                      </td>
                      <td className="py-2.5 pl-3 text-right">
                        {isTerminal ? (
                          <div className="font-mono text-[11px] text-slate-400">
                            <div>
                              {item.reviewed_by ? `By ${item.reviewed_by}` : 'Closed'}
                            </div>
                            {item.resolution_cycle_id ? (
                              <div className="text-sky-400">{item.resolution_cycle_id}</div>
                            ) : null}
                          </div>
                        ) : isConfirming ? (
                          <div className="p-2.5 rounded-lg bg-[#0B111B] border border-sky-800 text-left space-y-2 min-w-[220px]">
                            <input
                              type="text"
                              value={reviewNotes}
                              onChange={(e) => setReviewNotes(e.target.value)}
                              placeholder="Operator verification note..."
                              className="w-full px-2 py-1 bg-[#111827] border border-slate-700 rounded text-[11px] text-slate-100"
                            />
                            <label className="flex items-start gap-1.5 text-[11px] text-amber-200 cursor-pointer">
                              <input
                                type="checkbox"
                                checked={approveChecked}
                                onChange={(e) => setApproveChecked(e.target.checked)}
                                className="mt-0.5 accent-sky-500"
                              />
                              <span>Confirm official {item.proposed_outcome} settlement</span>
                            </label>
                            <div className="flex items-center justify-end gap-1.5">
                              <button
                                type="button"
                                onClick={() => {
                                  setConfirmingQueueId(null);
                                  setApproveChecked(false);
                                }}
                                className="px-2 py-1 text-[11px] bg-slate-800 text-slate-300 rounded"
                              >
                                Cancel
                              </button>
                              <button
                                type="button"
                                disabled={!approveChecked || busy !== null}
                                onClick={() => void handleReviewAction(item, 'APPROVE')}
                                className="px-2.5 py-1 text-[11px] font-semibold bg-emerald-700 hover:bg-emerald-600 disabled:opacity-40 text-white rounded"
                              >
                                Finalize
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="flex flex-wrap justify-end gap-1.5">
                            <button
                              type="button"
                              disabled={!isFinalizable || !canApproveSettlement || busy !== null}
                              title={
                                !canApproveSettlement
                                  ? 'Requires OPERATOR or OWNER role'
                                  : !isFinalizable
                                    ? 'Blocked: only uncontested PENDING_OPERATOR_REVIEW items with a linked forecast or blotter row can be finalized'
                                    : 'Approve & finalize official settlement'
                              }
                              onClick={() => {
                                setConfirmingQueueId(item.queue_id);
                                setApproveChecked(false);
                                setReviewNotes('');
                              }}
                              className="px-2.5 py-1 text-[11px] font-semibold bg-emerald-800/80 hover:bg-emerald-700 disabled:opacity-35 disabled:cursor-not-allowed text-emerald-100 rounded"
                            >
                              Approve
                            </button>
                            <button
                              type="button"
                              disabled={!canApproveSettlement || busy !== null}
                              onClick={() => void handleReviewAction(item, 'MARK_DISPUTED')}
                              className="px-2 py-1 text-[11px] font-medium bg-amber-900/60 hover:bg-amber-800/80 disabled:opacity-40 text-amber-200 rounded"
                            >
                              Dispute
                            </button>
                            <button
                              type="button"
                              disabled={!canApproveSettlement || busy !== null}
                              onClick={() => void handleReviewAction(item, 'REJECT')}
                              className="px-2 py-1 text-[11px] font-medium bg-slate-800 hover:bg-slate-700 disabled:opacity-40 text-slate-300 rounded"
                            >
                              Reject
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
