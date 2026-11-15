import { useMemo, useState } from 'react';
import type {
  DashboardPayload,
  HealthPayload,
  MarketBookSnapshotResponse,
} from '../types/certainty';
import {
  filterOpportunities,
  formatPct,
  formatPrice,
  formatTimestampShort,
  formatUsd,
  type OpportunityFilter,
} from '../utils/formatters';
import { HistoricalEdgeChart } from './HistoricalEdgeChart';
import { OrderBookInspector } from './OrderBookInspector';
import { ProvenanceLabel } from './ProvenanceLabel';

interface CommandCenterViewProps {
  dashboard: DashboardPayload;
  health: HealthPayload | null;
  selectedMarketId: string | null;
  selectedBook: MarketBookSnapshotResponse | null;
  bookLoading: boolean;
  bookError: string | null;
  scanning: boolean;
  onSelectMarket: (marketId: string) => void;
  onRunScan: () => void;
  onNavigateTab: (tab: 'risk' | 'scanner' | 'cross_venue' | 'architecture') => void;
  canRunScans?: boolean;
}

export function CommandCenterView({
  dashboard,
  health,
  selectedMarketId,
  selectedBook,
  bookLoading,
  bookError,
  scanning,
  onSelectMarket,
  onRunScan,
  onNavigateTab,
  canRunScans = true,
}: CommandCenterViewProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [filterMode, setFilterMode] = useState<OpportunityFilter>('ALL');

  const opportunities = dashboard.latest_opportunities;
  const filteredOpps = useMemo(
    () => filterOpportunities(opportunities, searchQuery, filterMode),
    [opportunities, searchQuery, filterMode]
  );

  const approvedCount = opportunities.filter((o) => o.risk_gate_approved).length;
  const polyHealth = health?.venues.polymarket;
  const kalshiHealth = health?.venues.kalshi;
  const killActive = dashboard.risk_gate.kill_switch.active;
  const dailyPnl = dashboard.portfolio.daily_simulated_pnl_usd;

  return (
    <div className="space-y-6">
      {/* Top 4 KPI Strip */}
      <section aria-label="Portfolio and Risk Gate overview" className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        <div className="border border-slate-800 bg-[#111827] rounded-xl p-5 flex flex-col justify-between">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-slate-400">Paper Capital Allocated</span>
            <ProvenanceLabel provenance="SIMULATED" />
          </div>
          <div className="mt-3 text-2xl font-semibold font-mono tabular-nums text-slate-50">
            {formatUsd(dashboard.portfolio.paper_capital_allocated_usd)}
          </div>
          <div className="mt-2 text-xs text-slate-400">
            Tier 1 paper pool · Live execution disabled
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-5 flex flex-col justify-between">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-slate-400">Daily Simulated P&amp;L</span>
            <ProvenanceLabel provenance="SIMULATED" />
          </div>
          <div
            className={`mt-3 text-2xl font-semibold font-mono tabular-nums ${
              dailyPnl < 0 ? 'text-rose-400' : dailyPnl > 0 ? 'text-emerald-400' : 'text-slate-100'
            }`}
          >
            {formatUsd(dailyPnl)}
          </div>
          <div className="mt-2 text-xs text-slate-400 flex items-center justify-between gap-2">
            <span>Simulated unwind P&amp;L · Cap -$250/day</span>
            <button
              type="button"
              onClick={() => onNavigateTab('cross_venue')}
              className="text-sky-400 hover:text-sky-300 underline-offset-2 hover:underline shrink-0"
            >
              Blotter
            </button>
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-5 flex flex-col justify-between">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-slate-400">Active Simulated Exposure</span>
            <ProvenanceLabel provenance="SIMULATED" />
          </div>
          <div className="mt-3 text-2xl font-semibold font-mono tabular-nums text-slate-50">
            {formatUsd(dashboard.portfolio.deployed_exposure_usd)}
          </div>
          <div className="mt-2 text-xs text-slate-400 font-mono tabular-nums">
            Polymarket + Kalshi · $2.5k cap/venue
          </div>
        </div>

        <div className="border border-slate-800 bg-[#111827] rounded-xl p-5 flex flex-col justify-between">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-slate-400">Risk Gate Posture</span>
            <ProvenanceLabel provenance="LIVE" />
          </div>
          <div
            className={`mt-3 text-2xl font-semibold font-mono ${
              killActive ? 'text-rose-400' : 'text-emerald-400'
            }`}
          >
            {killActive ? 'HALTED' : 'FAIL-CLOSED'}
          </div>
          <div className="mt-2 text-xs text-slate-400 flex items-center justify-between">
            <span>Min Intra Edge {dashboard.risk_gate.policy.min_intra_venue_edge_pct.toFixed(2)}%</span>
            <button
              type="button"
              onClick={() => onNavigateTab('risk')}
              className="text-sky-400 hover:text-sky-300 underline-offset-2 hover:underline"
            >
              Manage
            </button>
          </div>
        </div>
      </section>

      {/* Market Opportunities Table */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2.5">
              <h2 className="text-base font-semibold text-slate-100">
                Polymarket Tier 1 Binary Arbitrage Scanner
              </h2>
              <ProvenanceLabel provenance="LIVE" detail="Gamma + CLOB V2" />
            </div>
            <p className="mt-0.5 text-xs text-slate-400">
              Executable YES + NO ask VWAP, taker fees, and slippage buffer compared against $1.00 guaranteed payout.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <label htmlFor="cc-opp-search" className="sr-only">
              Filter scanned markets
            </label>
            <input
              id="cc-opp-search"
              type="search"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search market or rule..."
              className="px-3 py-1.5 text-xs bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-sky-500"
            />
            <div className="flex items-center gap-1 p-1 bg-[#0B111B] border border-slate-800 rounded-lg">
              {(['ALL', 'APPROVED', 'DENIED'] as OpportunityFilter[]).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setFilterMode(mode)}
                  className={`px-2.5 py-1 text-xs font-medium rounded transition-colors whitespace-nowrap ${
                    filterMode === mode
                      ? 'bg-slate-800 text-slate-100'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  {mode === 'ALL'
                    ? `All (${opportunities.length})`
                    : mode === 'APPROVED'
                      ? `Approved (${approvedCount})`
                      : `Denied (${opportunities.length - approvedCount})`}
                </button>
              ))}
            </div>
          </div>
        </div>

        {opportunities.length === 0 ? (
          <div className="p-10 text-center space-y-3">
            <h3 className="text-sm font-semibold text-slate-200">No Polymarket Scan Snapshot Yet</h3>
            <p className="text-xs text-slate-400 max-w-md mx-auto">
              Run a live read-only Polymarket scan to ingest Gamma binary markets, sort CLOB V2 order books,
              evaluate net arbitrage edges, and persist DuckDB audit records.
            </p>
            <button
              type="button"
              onClick={onRunScan}
              disabled={scanning || !canRunScans}
              title={canRunScans ? 'Scan Polymarket CLOB' : 'Requires OPERATOR or OWNER role'}
              className="px-4 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
            >
              {scanning ? 'Scanning CLOB V2...' : 'Scan Polymarket CLOB'}
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto max-h-[340px] overflow-y-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead className="sticky top-0 z-10">
                <tr className="border-b border-slate-800 bg-[#0E1522] text-slate-400 font-medium">
                  <th className="py-2.5 px-4">Market</th>
                  <th className="py-2.5 px-3 text-right">YES VWAP</th>
                  <th className="py-2.5 px-3 text-right">NO VWAP</th>
                  <th className="py-2.5 px-3 text-right">Fees + Slip</th>
                  <th className="py-2.5 px-3 text-right">All-In Cost</th>
                  <th className="py-2.5 px-3 text-right">Net Edge</th>
                  <th className="py-2.5 px-3 text-right">Depth Part.</th>
                  <th className="py-2.5 px-4">Risk Gate Verdict</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {filteredOpps.map((opp) => {
                  const isSelected = selectedMarketId === opp.market_id;
                  const totalFrictions =
                    opp.target_shares > 0
                      ? (opp.taker_fees_usd + opp.slippage_buffer_usd) / opp.target_shares
                      : 0;
                  return (
                    <tr
                      key={opp.opp_id}
                      onClick={() => onSelectMarket(opp.market_id)}
                      className={`cursor-pointer transition-colors ${
                        isSelected ? 'bg-sky-950/35' : 'hover:bg-slate-800/40'
                      }`}
                    >
                      <td className="py-3 px-4 max-w-xs">
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            onSelectMarket(opp.market_id);
                          }}
                          className="text-left font-medium text-slate-100 hover:text-sky-300 transition-colors line-clamp-1"
                        >
                          {opp.question}
                        </button>
                        <div className="mt-0.5 text-[11px] font-mono text-slate-500">
                          ID {opp.market_id} · {opp.target_shares} sh · {opp.data_age_ms}ms age
                        </div>
                      </td>
                      <td className="py-3 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatPrice(opp.yes_vwap)}
                      </td>
                      <td className="py-3 px-3 text-right font-mono tabular-nums text-slate-200">
                        {formatPrice(opp.no_vwap)}
                      </td>
                      <td className="py-3 px-3 text-right font-mono tabular-nums text-amber-300">
                        {formatPrice(totalFrictions)}
                      </td>
                      <td className="py-3 px-3 text-right font-mono tabular-nums text-slate-100">
                        {formatPrice(opp.total_executable_cost)}
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
                        {formatPct(opp.liquidity_participation_pct, 2)}
                      </td>
                      <td className="py-3 px-4 max-w-xs">
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
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Order Book Depth Inspector */}
      <OrderBookInspector bookData={selectedBook} loading={bookLoading} error={bookError} />

      {/* Middle 2-Column Analytics: Historical DuckDB Edge + Venue Exposure & System Health */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-100">
              DuckDB Historical Net Edge Series
            </h2>
            <ProvenanceLabel provenance="LIVE" detail={`${dashboard.historical_snapshots_count} snapshots`} />
          </div>
          <HistoricalEdgeChart series={dashboard.historical_edge_series} />
        </section>

        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-100">
              Venue Exposure &amp; System Health
            </h2>
            <ProvenanceLabel provenance="LIVE" />
          </div>

          <div className="divide-y divide-slate-800 text-xs">
            {dashboard.portfolio.venue_exposures.map((v) => (
              <div key={v.venue} className="py-2.5 flex items-center justify-between gap-4">
                <div>
                  <div className="font-medium text-slate-200">{v.venue}</div>
                  <div className="text-slate-400 font-mono text-[11px]">
                    {v.mode} · {v.provenance}
                  </div>
                </div>
                <div className="text-right font-mono tabular-nums">
                  <div className="text-slate-100">
                    {formatUsd(v.current_exposure_usd)} / {formatUsd(v.max_cap_usd)}
                  </div>
                  <div className="text-slate-500 text-[11px]">
                    Loss Cap {formatUsd(v.daily_loss_limit_usd)}
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className="pt-3 border-t border-slate-800 grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
              <div className="text-slate-400">Polymarket Gamma</div>
              <div
                className={`mt-1 font-mono font-semibold ${
                  polyHealth?.gamma_reachable ? 'text-emerald-400' : 'text-amber-400'
                }`}
              >
                {polyHealth?.gamma_reachable === true
                  ? 'REACHABLE'
                  : polyHealth?.gamma_reachable === false
                    ? 'UNREACHABLE'
                    : 'UNPROBED'}
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
              <div className="text-slate-400">Polymarket CLOB V2</div>
              <div
                className={`mt-1 font-mono font-semibold ${
                  polyHealth?.clob_reachable ? 'text-emerald-400' : 'text-amber-400'
                }`}
              >
                {polyHealth?.clob_reachable === true
                  ? `REACHABLE (${polyHealth.probe_latency_ms ?? 0}ms)`
                  : polyHealth?.clob_reachable === false
                    ? 'UNREACHABLE'
                    : 'UNPROBED'}
              </div>
            </div>
            <div className="p-3 rounded-lg bg-[#0E1522] border border-slate-800/80">
              <div className="text-slate-400">Kalshi Trade API v2</div>
              <div
                className={`mt-1 font-mono font-semibold ${
                  kalshiHealth?.reachable ? 'text-emerald-400' : 'text-amber-400'
                }`}
              >
                {kalshiHealth?.reachable === true
                  ? `REACHABLE (${kalshiHealth.probe_latency_ms ?? 0}ms)`
                  : kalshiHealth?.reachable === false
                    ? 'UNREACHABLE'
                    : 'UNPROBED'}
              </div>
            </div>
          </div>
        </section>
      </div>

      {/* Bottom 2-Column: Strategy Matrix & Recent Audit/Agent Activity */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">Strategy &amp; Module Registry</h2>
            <button
              type="button"
              onClick={() => onNavigateTab('architecture')}
              className="text-xs text-sky-400 hover:text-sky-300"
            >
              Architecture Docs
            </button>
          </div>
          <div className="divide-y divide-slate-800 text-xs">
            {dashboard.modules.map((mod) => {
              const active = mod.status.startsWith('ACTIVE');
              return (
                <div key={mod.module_id} className="py-2.5 flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="font-medium text-slate-200">{mod.name}</div>
                    <div className="text-slate-400 mt-0.5 line-clamp-2">{mod.description}</div>
                  </div>
                  <div className="text-right shrink-0 font-mono">
                    <div className={active ? 'text-emerald-400 font-semibold' : 'text-slate-500'}>
                      {mod.status}
                    </div>
                    <div className="text-[11px] text-slate-500">{mod.phase}</div>
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">
              Recent Risk Gate Decisions &amp; Agent Memory
            </h2>
            <button
              type="button"
              onClick={() => onNavigateTab('risk')}
              className="text-xs text-sky-400 hover:text-sky-300"
            >
              Full Audit Trail
            </button>
          </div>

          <div className="divide-y divide-slate-800 text-xs">
            {dashboard.recent_audits.slice(0, 4).map((audit) => (
              <div key={audit.audit_id} className="py-2.5 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-mono text-slate-200 truncate">
                    {audit.audit_id} · Mkt {audit.market_id}
                  </div>
                  <div className="text-slate-400 truncate mt-0.5">{audit.summary}</div>
                </div>
                <div className="text-right shrink-0">
                  <div
                    className={`font-mono font-semibold ${
                      audit.decision === 'APPROVED' ? 'text-emerald-400' : 'text-rose-400'
                    }`}
                  >
                    {audit.decision}
                  </div>
                  <div className="text-[11px] text-slate-500 font-mono tabular-nums">
                    {formatTimestampShort(audit.decided_at)}
                  </div>
                </div>
              </div>
            ))}
            {dashboard.recent_audits.length === 0 ? (
              <div className="py-6 text-center text-slate-500">
                No Risk Gate evaluations logged yet. Run a scan or dry-run evaluation.
              </div>
            ) : null}
          </div>
        </section>
      </div>
    </div>
  );
}
