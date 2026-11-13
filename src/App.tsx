import { useCallback, useEffect, useState } from 'react';
import { certaintyApi } from './api/client';
import { CommandCenterView } from './components/CommandCenterView';
import { CrossVenuePaperView } from './components/CrossVenuePaperView';
import { MemoryArchitectureView } from './components/MemoryArchitectureView';
import { RiskGateAuditView } from './components/RiskGateAuditView';
import { ScannerView } from './components/ScannerView';
import { SidebarNav, type WorkspaceTab } from './components/SidebarNav';
import { TopBar } from './components/TopBar';
import type {
  CrossVenueOpportunityRecord,
  CrossVenuePairRecord,
  DashboardPayload,
  HealthPayload,
  KalshiMarketSummary,
  MarketBookSnapshotResponse,
  PaperBlotterResponse,
} from './types/certainty';
import type { RiskDryRunFormState } from './utils/formatters';

export default function App() {
  const [activeTab, setActiveTab] = useState<WorkspaceTab>('command');
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(null);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanNotice, setScanNotice] = useState<string | null>(null);

  const [selectedMarketId, setSelectedMarketId] = useState<string | null>(null);
  const [selectedBook, setSelectedBook] = useState<MarketBookSnapshotResponse | null>(null);
  const [bookLoading, setBookLoading] = useState(false);
  const [bookError, setBookError] = useState<string | null>(null);

  const [kalshiMarkets, setKalshiMarkets] = useState<KalshiMarketSummary[]>([]);
  const [kalshiLoading, setKalshiLoading] = useState(false);
  const [crossPairs, setCrossPairs] = useState<CrossVenuePairRecord[]>([]);
  const [crossOpps, setCrossOpps] = useState<CrossVenueOpportunityRecord[]>([]);
  const [paperBlotter, setPaperBlotter] = useState<PaperBlotterResponse | null>(null);

  const loadSelectedBook = useCallback(async (marketId: string) => {
    setSelectedMarketId(marketId);
    setBookLoading(true);
    setBookError(null);
    try {
      const bookResp = await certaintyApi.getMarketBook(marketId);
      setSelectedBook(bookResp);
    } catch (err) {
      setBookError(err instanceof Error ? err.message : 'Failed to load market book');
      setSelectedBook(null);
    } finally {
      setBookLoading(false);
    }
  }, []);

  const loadCrossVenueAndBlotter = useCallback(async () => {
    try {
      const [pairsRes, oppsRes, blotterRes] = await Promise.all([
        certaintyApi.getCrossVenuePairs(100),
        certaintyApi.getCrossVenueOpportunities(100),
        certaintyApi.getPaperBlotter(100),
      ]);
      setCrossPairs(pairsRes.items);
      setCrossOpps(oppsRes.items);
      setPaperBlotter(blotterRes);
    } catch {
      // Errors surfaced by main loader or interactive actions
    }
  }, []);

  const loadKalshiMarkets = useCallback(async () => {
    setKalshiLoading(true);
    try {
      const res = await certaintyApi.getKalshiMarkets(15);
      setKalshiMarkets(res.markets);
    } catch {
      // Surface smoothly without blocking core dashboard
    } finally {
      setKalshiLoading(false);
    }
  }, []);

  const loadAll = useCallback(
    async (probeVenues = true) => {
      try {
        setError(null);
        const dashPromise = certaintyApi.getDashboard();
        const shouldFetchHealth = probeVenues || !health;
        const [dashData, healthData] = await Promise.all([
          dashPromise,
          shouldFetchHealth ? certaintyApi.getHealth(true) : Promise.resolve(null),
          loadCrossVenueAndBlotter(),
        ]);
        setDashboard(dashData);
        if (healthData) {
          setHealth(healthData);
        }

        if (dashData.latest_opportunities.length > 0) {
          const targetId =
            selectedMarketId &&
            dashData.latest_opportunities.some((o) => o.market_id === selectedMarketId)
              ? selectedMarketId
              : dashData.latest_opportunities[0].market_id;
          await loadSelectedBook(targetId);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to connect to backend');
      } finally {
        setLoading(false);
      }
    },
    [health, loadCrossVenueAndBlotter, loadSelectedBook, selectedMarketId]
  );

  useEffect(() => {
    void loadAll(true);
    void loadKalshiMarkets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleRunScan = async (params?: {
    limit: number;
    target_shares: number;
    slippage_buffer_bps: number;
  }) => {
    setScanning(true);
    setScanNotice(null);
    try {
      const result = await certaintyApi.runPolymarketScan(
        params ?? { limit: 12, target_shares: 25, slippage_buffer_bps: 15 }
      );
      if (result.status === 'ERROR') {
        setScanNotice(result.error || 'Polymarket scan returned an error');
      } else {
        setScanNotice(
          `Scan ${result.scan_id} completed in ${result.duration_ms}ms: ${result.markets_scanned} binary markets evaluated (${result.approved_count ?? 0} Risk Gate approved).`
        );
      }
      await loadAll(false);
    } catch (err) {
      setScanNotice(err instanceof Error ? err.message : 'Scan request failed');
    } finally {
      setScanning(false);
    }
  };

  const handleToggleKillSwitch = async (active: boolean, reason: string) => {
    await certaintyApi.toggleKillSwitch({ active, reason, actor: 'operator' });
    await loadAll(false);
  };

  const handleEvaluateDryRun = async (payload: RiskDryRunFormState) => {
    const res = await certaintyApi.evaluateRiskGate(payload);
    await loadAll(false);
    return res;
  };

  const handleCreateMemory = async (payload: {
    category: string;
    title: string;
    summary: string;
    status: string;
  }) => {
    await certaintyApi.createMemory(payload);
    await loadAll(false);
  };

  const killSwitchActive = dashboard?.risk_gate.kill_switch.active ?? false;

  return (
    <div className="min-h-screen bg-[#0B111B] text-slate-100 flex flex-col lg:flex-row">
      <SidebarNav
        activeTab={activeTab}
        onSelectTab={setActiveTab}
        killSwitchActive={killSwitchActive}
        auditsCount={dashboard?.risk_gate.total_audits ?? 0}
        opportunitiesCount={dashboard?.historical_opportunities_count ?? 0}
      />

      <div className="flex-1 min-w-0 flex flex-col">
        <TopBar
          activeTab={activeTab}
          scanning={scanning}
          killSwitchActive={killSwitchActive}
          onRunScan={() => void handleRunScan()}
          onRefresh={() => void loadAll(true)}
        />

        <main className="flex-1 p-6 max-w-[1440px] w-full mx-auto space-y-5">
          {scanNotice ? (
            <div
              role="status"
              className="px-4 py-3 rounded-lg border border-slate-700 bg-[#111827] text-xs text-slate-200 flex items-center justify-between gap-4"
            >
              <span>{scanNotice}</span>
              <button
                type="button"
                onClick={() => setScanNotice(null)}
                className="text-slate-400 hover:text-slate-200"
              >
                Dismiss
              </button>
            </div>
          ) : null}

          {error ? (
            <div className="p-5 rounded-xl border border-rose-900 bg-rose-950/30 text-sm text-rose-200 space-y-2">
              <div className="font-semibold">Backend Connection Unavailable</div>
              <div className="text-xs text-rose-300">{error}</div>
              <button
                type="button"
                onClick={() => void loadAll(true)}
                className="px-3 py-1.5 text-xs font-medium bg-rose-700 hover:bg-rose-600 text-white rounded-lg"
              >
                Retry Connection
              </button>
            </div>
          ) : null}

          {loading && !dashboard ? (
            <div className="space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
                {[1, 2, 3, 4].map((idx) => (
                  <div key={idx} className="h-28 rounded-xl bg-[#111827] border border-slate-800 animate-pulse" />
                ))}
              </div>
              <div className="h-72 rounded-xl bg-[#111827] border border-slate-800 animate-pulse" />
            </div>
          ) : dashboard ? (
            <>
              {activeTab === 'command' ? (
                <CommandCenterView
                  dashboard={dashboard}
                  health={health}
                  selectedMarketId={selectedMarketId}
                  selectedBook={selectedBook}
                  bookLoading={bookLoading}
                  bookError={bookError}
                  scanning={scanning}
                  onSelectMarket={(id) => void loadSelectedBook(id)}
                  onRunScan={() => void handleRunScan()}
                  onNavigateTab={(tab) => setActiveTab(tab)}
                />
              ) : null}

              {activeTab === 'scanner' ? (
                <ScannerView
                  opportunities={
                    dashboard.latest_opportunities.length > 0
                      ? dashboard.latest_opportunities
                      : dashboard.recent_all_opportunities
                  }
                  selectedMarketId={selectedMarketId}
                  selectedBook={selectedBook}
                  bookLoading={bookLoading}
                  bookError={bookError}
                  scanning={scanning}
                  onSelectMarket={(id) => void loadSelectedBook(id)}
                  onRunCustomScan={(params) => void handleRunScan(params)}
                />
              ) : null}

              {activeTab === 'cross_venue' ? (
                <CrossVenuePaperView
                  polymarketOpportunities={
                    dashboard.latest_opportunities.length > 0
                      ? dashboard.latest_opportunities
                      : dashboard.recent_all_opportunities
                  }
                  kalshiMarkets={kalshiMarkets}
                  kalshiLoading={kalshiLoading}
                  pairs={crossPairs}
                  crossOpportunities={crossOpps}
                  blotter={paperBlotter}
                  onRefreshKalshi={loadKalshiMarkets}
                  onRefreshCrossVenue={async () => {
                    await Promise.all([loadCrossVenueAndBlotter(), loadAll(false)]);
                  }}
                />
              ) : null}

              {activeTab === 'risk' ? (
                <RiskGateAuditView
                  policy={dashboard.risk_gate.policy}
                  killSwitch={dashboard.risk_gate.kill_switch}
                  audits={dashboard.recent_audits}
                  onToggleKillSwitch={handleToggleKillSwitch}
                  onEvaluateDryRun={handleEvaluateDryRun}
                />
              ) : null}

              {activeTab === 'architecture' ? (
                <MemoryArchitectureView
                  memories={dashboard.recent_memories}
                  modules={dashboard.modules}
                  onCreateMemory={handleCreateMemory}
                />
              ) : null}
            </>
          ) : null}
        </main>
      </div>
    </div>
  );
}
