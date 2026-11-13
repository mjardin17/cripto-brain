import { useState } from 'react';
import type { ArbitrageOpportunity, MarketBookSnapshotResponse } from '../types/certainty';
import { formatPct, formatPrice, formatUsd } from '../utils/formatters';
import { OrderBookInspector } from './OrderBookInspector';
import { ProvenanceLabel } from './ProvenanceLabel';

interface ScannerViewProps {
  opportunities: ArbitrageOpportunity[];
  selectedMarketId: string | null;
  selectedBook: MarketBookSnapshotResponse | null;
  bookLoading: boolean;
  bookError: string | null;
  scanning: boolean;
  onSelectMarket: (marketId: string) => void;
  onRunCustomScan: (params: {
    limit: number;
    target_shares: number;
    slippage_buffer_bps: number;
  }) => void;
}

export function ScannerView({
  opportunities,
  selectedMarketId,
  selectedBook,
  bookLoading,
  bookError,
  scanning,
  onSelectMarket,
  onRunCustomScan,
}: ScannerViewProps) {
  const [limit, setLimit] = useState(12);
  const [targetShares, setTargetShares] = useState(25);
  const [slippageBps, setSlippageBps] = useState(15);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onRunCustomScan({
      limit,
      target_shares: targetShares,
      slippage_buffer_bps: slippageBps,
    });
  };

  return (
    <div className="space-y-6">
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-5">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-4 border-b border-slate-800">
          <div>
            <h2 className="text-base font-semibold text-slate-100">
              Tier 1 Polymarket CLOB V2 Scanner Controls
            </h2>
            <p className="mt-0.5 text-xs text-slate-400">
              Walks live Gamma + CLOB V2 ask books for exact share size, adds category-specific taker fees and slippage buffer, and gates every pair through Risk Gate.
            </p>
          </div>
          <ProvenanceLabel provenance="LIVE" detail="Read-Only Public API" />
        </div>

        <form onSubmit={handleSubmit} className="mt-4 grid grid-cols-1 sm:grid-cols-4 gap-4 items-end">
          <div>
            <label htmlFor="scan-limit" className="block text-xs font-medium text-slate-300 mb-1.5">
              Active Markets Limit
            </label>
            <input
              id="scan-limit"
              type="number"
              min={1}
              max={30}
              value={limit}
              onChange={(e) => setLimit(Math.max(1, Math.min(30, Number(e.target.value) || 10)))}
              className="w-full px-3 py-2 text-xs font-mono tabular-nums bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
            />
          </div>

          <div>
            <label htmlFor="scan-shares" className="block text-xs font-medium text-slate-300 mb-1.5">
              Target Pair Size (Shares)
            </label>
            <input
              id="scan-shares"
              type="number"
              min={1}
              max={5000}
              step={5}
              value={targetShares}
              onChange={(e) => setTargetShares(Math.max(1, Number(e.target.value) || 25))}
              className="w-full px-3 py-2 text-xs font-mono tabular-nums bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
            />
          </div>

          <div>
            <label htmlFor="scan-slippage" className="block text-xs font-medium text-slate-300 mb-1.5">
              Leg Slippage Buffer (bps)
            </label>
            <input
              id="scan-slippage"
              type="number"
              min={0}
              max={200}
              value={slippageBps}
              onChange={(e) => setSlippageBps(Math.max(0, Number(e.target.value) || 0))}
              className="w-full px-3 py-2 text-xs font-mono tabular-nums bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 focus:outline-none focus:ring-2 focus:ring-sky-500"
            />
          </div>

          <button
            type="submit"
            disabled={scanning}
            className="px-4 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-60 rounded-lg transition-colors h-[34px]"
          >
            {scanning ? 'Scanning Live Books...' : 'Execute Research Scan'}
          </button>
        </form>
      </section>

      <OrderBookInspector bookData={selectedBook} loading={bookLoading} error={bookError} />

      <section className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-slate-800 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-100">
            DuckDB Logged Arbitrage Evaluations ({opportunities.length})
          </h3>
          <span className="text-xs text-slate-400">Click row to inspect YES/NO depth</span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="border-b border-slate-800 bg-[#0E1522] text-slate-400 font-medium">
                <th className="py-2.5 px-4">Market Question</th>
                <th className="py-2.5 px-3 text-right">Shares</th>
                <th className="py-2.5 px-3 text-right">Raw Sum</th>
                <th className="py-2.5 px-3 text-right">Taker Fees</th>
                <th className="py-2.5 px-3 text-right">Gross Edge</th>
                <th className="py-2.5 px-3 text-right">Net Edge</th>
                <th className="py-2.5 px-3 text-right">Imbalance</th>
                <th className="py-2.5 px-4">Risk Gate Decision</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/80">
              {opportunities.map((opp) => (
                <tr
                  key={opp.opp_id}
                  onClick={() => onSelectMarket(opp.market_id)}
                  className={`cursor-pointer transition-colors ${
                    selectedMarketId === opp.market_id ? 'bg-sky-950/35' : 'hover:bg-slate-800/40'
                  }`}
                >
                  <td className="py-3 px-4 max-w-sm">
                    <div className="font-medium text-slate-100 line-clamp-1">{opp.question}</div>
                    <div className="text-[11px] font-mono text-slate-500">
                      {opp.scan_id} · Mkt {opp.market_id}
                    </div>
                  </td>
                  <td className="py-3 px-3 text-right font-mono tabular-nums text-slate-300">
                    {opp.target_shares}
                  </td>
                  <td className="py-3 px-3 text-right font-mono tabular-nums text-slate-200">
                    {formatPrice(opp.combined_raw_cost)}
                  </td>
                  <td className="py-3 px-3 text-right font-mono tabular-nums text-amber-300">
                    {formatUsd(opp.taker_fees_usd, 4)}
                  </td>
                  <td className="py-3 px-3 text-right font-mono tabular-nums text-slate-300">
                    {formatPct(opp.gross_edge_pct, 3, true)}
                  </td>
                  <td
                    className={`py-3 px-3 text-right font-mono tabular-nums font-semibold ${
                      opp.net_edge_pct !== null && opp.net_edge_pct >= 1.5
                        ? 'text-emerald-400'
                        : 'text-rose-400'
                    }`}
                  >
                    {formatPct(opp.net_edge_pct, 3, true)}
                  </td>
                  <td className="py-3 px-3 text-right font-mono tabular-nums text-slate-300">
                    {opp.leg_imbalance_ratio.toFixed(2)}x
                  </td>
                  <td className="py-3 px-4 max-w-xs">
                    <span
                      className={`font-mono font-semibold ${
                        opp.risk_gate_approved ? 'text-emerald-400' : 'text-rose-400'
                      }`}
                    >
                      {opp.risk_gate_approved ? 'APPROVED' : 'DENIED'}
                    </span>
                    <div className="text-[11px] text-slate-400 truncate">{opp.risk_gate_reason}</div>
                  </td>
                </tr>
              ))}
              {opportunities.length === 0 ? (
                <tr>
                  <td colSpan={8} className="py-8 text-center text-slate-500">
                    No scanner runs logged yet. Submit parameters above to run a live scan.
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
