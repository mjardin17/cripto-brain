import type { MarketBookSnapshotResponse } from '../types/certainty';
import { formatPct, formatPrice, formatShares, formatTimestampShort, formatUsd } from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface OrderBookInspectorProps {
  bookData: MarketBookSnapshotResponse | null;
  loading: boolean;
  error: string | null;
}

export function OrderBookInspector({ bookData, loading, error }: OrderBookInspectorProps) {
  if (loading) {
    return (
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
        <div className="h-5 w-64 bg-slate-800 rounded animate-pulse" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="h-56 bg-slate-800/60 rounded-lg animate-pulse" />
          <div className="h-56 bg-slate-800/60 rounded-lg animate-pulse" />
        </div>
      </section>
    );
  }

  if (error) {
    return (
      <section className="border border-rose-900/60 bg-rose-950/20 rounded-xl p-5">
        <h3 className="text-sm font-semibold text-rose-300">Order Book Snapshot Unavailable</h3>
        <p className="mt-1 text-xs text-rose-200/80">{error}</p>
      </section>
    );
  }

  if (!bookData) {
    return (
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-6 text-center">
        <h3 className="text-sm font-semibold text-slate-200">Order Book Depth Inspector</h3>
        <p className="mt-1 text-xs text-slate-400">
          Select any scanned Polymarket market row above to inspect sorted CLOB V2 YES/NO ask ladders,
          VWAP fill cost, taker fees, and Risk Gate math.
        </p>
      </section>
    );
  }

  const { snapshot } = bookData;
  const { yes, no, evaluation } = snapshot.book_payload;

  return (
    <section
      className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-5"
      data-testid="order-book-inspector"
    >
      <div className="flex flex-wrap items-start justify-between gap-4 pb-4 border-b border-slate-800">
        <div className="min-w-0 max-w-3xl">
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
            <ProvenanceLabel provenance="LIVE" detail={`CLOB V2 · Market ${snapshot.market_id}`} />
            <span aria-hidden="true">·</span>
            <span className="font-mono tabular-nums">{formatTimestampShort(snapshot.ingested_at)}</span>
            <span aria-hidden="true">·</span>
            <span>{snapshot.neg_risk ? 'NegRisk Market' : 'Standard Binary'}</span>
          </div>
          <h3 className="mt-1.5 text-base font-semibold text-slate-100">{snapshot.question}</h3>
        </div>

        <div className="text-right font-mono tabular-nums shrink-0">
          <div className="text-xs text-slate-400">Net Arbitrage Edge ({formatShares(evaluation.target_shares)} sh)</div>
          <div
            className={`text-lg font-semibold ${
              evaluation.net_edge_pct !== null && evaluation.net_edge_pct >= 1.5
                ? 'text-emerald-400'
                : 'text-rose-400'
            }`}
          >
            {formatPct(evaluation.net_edge_pct, 3, true)}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 text-xs">
        <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
          <div className="text-slate-400">YES VWAP</div>
          <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
            {formatPrice(evaluation.yes_vwap)}
          </div>
        </div>
        <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
          <div className="text-slate-400">NO VWAP</div>
          <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
            {formatPrice(evaluation.no_vwap)}
          </div>
        </div>
        <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
          <div className="text-slate-400">Raw Combined</div>
          <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
            {formatPrice(evaluation.combined_raw_cost)}
          </div>
        </div>
        <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
          <div className="text-slate-400">Taker Fees ({evaluation.fee_type})</div>
          <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-amber-300">
            {formatUsd(evaluation.taker_fees_usd, 4)}
          </div>
        </div>
        <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
          <div className="text-slate-400">Slippage Buffer</div>
          <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-200">
            {formatUsd(evaluation.slippage_buffer_usd, 4)}
          </div>
        </div>
        <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
          <div className="text-slate-400">All-In Cost / $1 Payout</div>
          <div className="mt-1 font-mono tabular-nums text-sm font-semibold text-slate-100">
            {formatPrice(evaluation.total_executable_cost_per_pair)}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* YES Token Book */}
        <div className="border border-slate-800 rounded-lg overflow-hidden bg-[#0E1522]">
          <div className="px-4 py-2.5 border-b border-slate-800 flex items-center justify-between text-xs">
            <span className="font-semibold text-slate-200">YES Outcome Order Book</span>
            <span className="font-mono tabular-nums text-slate-400">
              Ask Depth: {formatShares(snapshot.yes_ask_depth_shares)} sh · Tick ${yes.tick_size}
            </span>
          </div>
          <div className="grid grid-cols-2 divide-x divide-slate-800 text-xs">
            <div>
              <div className="px-3 py-1.5 bg-slate-900/60 text-slate-400 flex justify-between font-medium">
                <span>Best Bids</span>
                <span>Size</span>
              </div>
              <div className="divide-y divide-slate-800/60">
                {yes.bids.slice(0, 8).map((lvl, idx) => (
                  <div key={`yb-${idx}`} className="px-3 py-1.5 flex justify-between font-mono tabular-nums">
                    <span className="text-emerald-400">{lvl.price.toFixed(3)}</span>
                    <span className="text-slate-300">{formatShares(lvl.size)}</span>
                  </div>
                ))}
                {yes.bids.length === 0 ? (
                  <div className="px-3 py-3 text-slate-500">No resting bids</div>
                ) : null}
              </div>
            </div>
            <div>
              <div className="px-3 py-1.5 bg-slate-900/60 text-slate-400 flex justify-between font-medium">
                <span>Best Asks (Buy Leg)</span>
                <span>Size</span>
              </div>
              <div className="divide-y divide-slate-800/60">
                {yes.asks.slice(0, 8).map((lvl, idx) => (
                  <div key={`ya-${idx}`} className="px-3 py-1.5 flex justify-between font-mono tabular-nums">
                    <span className="text-rose-400">{lvl.price.toFixed(3)}</span>
                    <span className="text-slate-300">{formatShares(lvl.size)}</span>
                  </div>
                ))}
                {yes.asks.length === 0 ? (
                  <div className="px-3 py-3 text-slate-500">No resting asks</div>
                ) : null}
              </div>
            </div>
          </div>
        </div>

        {/* NO Token Book */}
        <div className="border border-slate-800 rounded-lg overflow-hidden bg-[#0E1522]">
          <div className="px-4 py-2.5 border-b border-slate-800 flex items-center justify-between text-xs">
            <span className="font-semibold text-slate-200">NO Outcome Order Book</span>
            <span className="font-mono tabular-nums text-slate-400">
              Ask Depth: {formatShares(snapshot.no_ask_depth_shares)} sh · Tick ${no.tick_size}
            </span>
          </div>
          <div className="grid grid-cols-2 divide-x divide-slate-800 text-xs">
            <div>
              <div className="px-3 py-1.5 bg-slate-900/60 text-slate-400 flex justify-between font-medium">
                <span>Best Bids</span>
                <span>Size</span>
              </div>
              <div className="divide-y divide-slate-800/60">
                {no.bids.slice(0, 8).map((lvl, idx) => (
                  <div key={`nb-${idx}`} className="px-3 py-1.5 flex justify-between font-mono tabular-nums">
                    <span className="text-emerald-400">{lvl.price.toFixed(3)}</span>
                    <span className="text-slate-300">{formatShares(lvl.size)}</span>
                  </div>
                ))}
                {no.bids.length === 0 ? (
                  <div className="px-3 py-3 text-slate-500">No resting bids</div>
                ) : null}
              </div>
            </div>
            <div>
              <div className="px-3 py-1.5 bg-slate-900/60 text-slate-400 flex justify-between font-medium">
                <span>Best Asks (Buy Leg)</span>
                <span>Size</span>
              </div>
              <div className="divide-y divide-slate-800/60">
                {no.asks.slice(0, 8).map((lvl, idx) => (
                  <div key={`na-${idx}`} className="px-3 py-1.5 flex justify-between font-mono tabular-nums">
                    <span className="text-rose-400">{lvl.price.toFixed(3)}</span>
                    <span className="text-slate-300">{formatShares(lvl.size)}</span>
                  </div>
                ))}
                {no.asks.length === 0 ? (
                  <div className="px-3 py-3 text-slate-500">No resting asks</div>
                ) : null}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
