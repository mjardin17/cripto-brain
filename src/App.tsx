import { useCallback, useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { ApiError, certaintyApi, registerAuthHandlers } from './api/client';
import { AdminAccessView } from './components/AdminAccessView';
import { AuthGateView } from './components/AuthGateView';
import { CommandCenterView } from './components/CommandCenterView';
import { CrossVenuePaperView } from './components/CrossVenuePaperView';
import { IntelligenceCenterView } from './components/IntelligenceCenterView';
import { MarketCouncilsView } from './components/MarketCouncilsView';
import { MemoryArchitectureView } from './components/MemoryArchitectureView';
import { RiskGateAuditView } from './components/RiskGateAuditView';
import { ScannerView } from './components/ScannerView';
import { SidebarNav, type WorkspaceTab } from './components/SidebarNav';
import { TopBar } from './components/TopBar';
import type {
  AuthStatusResponse,
  AuthenticatedUserRecord,
  CrossVenueOpportunityRecord,
  CrossVenuePairRecord,
  DashboardPayload,
  HealthPayload,
  IntelligenceCommandCenterPayload,
  KalshiMarketSummary,
  MarketBookSnapshotResponse,
  MarketIntelligenceOverview,
  PaperBlotterResponse,
} from './types/certainty';
import { canAccessWorkspaceTab, type RiskDryRunFormState } from './utils/formatters';

export default function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatusResponse | null>(null);
  const [authUser, setAuthUser] = useState<AuthenticatedUserRecord | null>(null);
  const [authChecking, setAuthChecking] = useState(true);
  const [sessionNotice, setSessionNotice] = useState<string | null>(null);
  const [forbiddenNotice, setForbiddenNotice] = useState<string | null>(null);

  // Change password modal state
  const [showChangePassword, setShowChangePassword] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmNewPassword, setConfirmNewPassword] = useState('');
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordSuccess, setPasswordSuccess] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<WorkspaceTab>('command');
  const [dashboard, setDashboard] = useState<DashboardPayload | null>(null);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [loading, setLoading] = useState(false);
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
  const [intelligenceData, setIntelligenceData] =
    useState<IntelligenceCommandCenterPayload | null>(null);
  const [intelligenceLoading, setIntelligenceLoading] = useState(false);
  const [marketIntelData, setMarketIntelData] = useState<MarketIntelligenceOverview | null>(null);
  const [marketIntelLoading, setMarketIntelLoading] = useState(false);

  const resetWorkspaceState = useCallback(() => {
    setDashboard(null);
    setHealth(null);
    setSelectedMarketId(null);
    setSelectedBook(null);
    setKalshiMarkets([]);
    setCrossPairs([]);
    setCrossOpps([]);
    setPaperBlotter(null);
    setIntelligenceData(null);
    setMarketIntelData(null);
    setActiveTab('command');
  }, []);

  const refreshAuthStatus = useCallback(async () => {
    setError(null);
    const maxAttempts = 4;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        const status = await certaintyApi.getAuthStatus();
        setAuthStatus(status);
        setAuthUser(status.user);
        setError(null);
        if (!status.user) {
          resetWorkspaceState();
        }
        setAuthChecking(false);
        return;
      } catch (err) {
        const transient =
          err instanceof ApiError
            ? err.status === 502 || err.status === 503 || err.status === 504
            : true;
        if (transient && attempt < maxAttempts) {
          await new Promise((resolve) => setTimeout(resolve, 900));
          continue;
        }
        setError(err instanceof Error ? err.message : 'Unable to reach authentication service');
      }
    }
    setAuthChecking(false);
  }, [resetWorkspaceState]);

  useEffect(() => {
    registerAuthHandlers({
      onUnauthorized: (detail) => {
        setAuthUser(null);
        resetWorkspaceState();
        setSessionNotice(
          detail && detail !== 'Authentication required'
            ? `Session ended: ${detail}`
            : 'Your session expired or was revoked. Sign in again to continue.'
        );
        void refreshAuthStatus();
      },
      onForbidden: (detail) => {
        setForbiddenNotice(`Access Denied (403): ${detail}`);
      },
    });
    return () => registerAuthHandlers({});
  }, [refreshAuthStatus, resetWorkspaceState]);

  useEffect(() => {
    void refreshAuthStatus();
  }, [refreshAuthStatus]);

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

  const loadIntelligenceCenter = useCallback(async () => {
    setIntelligenceLoading(true);
    try {
      const res = await certaintyApi.getIntelligenceCommandCenter();
      setIntelligenceData(res);
    } catch {
      // Surfaced via command-center loader state
    } finally {
      setIntelligenceLoading(false);
    }
  }, []);

  const loadMarketIntel = useCallback(async () => {
    setMarketIntelLoading(true);
    try {
      const res = await certaintyApi.getMarketIntelligenceOverview('24H');
      setMarketIntelData(res);
    } catch {
      // Surfaced via MarketCouncilsView action/error handlers
    } finally {
      setMarketIntelLoading(false);
    }
  }, []);

  const loadAll = useCallback(
    async (probeVenues = true) => {
      try {
        setLoading(true);
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

  // Trigger protected workspace fetches only after session is verified
  useEffect(() => {
    if (!authUser) return;
    void loadAll(true);
    void loadKalshiMarkets();
    void loadIntelligenceCenter();
    void loadMarketIntel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authUser?.user_id]);

  // Keep activeTab valid if user role changes
  useEffect(() => {
    if (authUser && !canAccessWorkspaceTab(authUser.role, activeTab)) {
      setActiveTab('command');
    }
  }, [activeTab, authUser]);

  const handleAuthenticated = (user: AuthenticatedUserRecord) => {
    setAuthUser(user);
    setSessionNotice(null);
    setForbiddenNotice(null);
    setAuthStatus((prev) =>
      prev
        ? { ...prev, authenticated: true, needs_owner_bootstrap: false, user }
        : prev
    );
  };

  const handleLogout = async () => {
    try {
      await certaintyApi.logout();
    } catch {
      // Even if cookie already expired, clear local view state
    }
    setAuthUser(null);
    resetWorkspaceState();
    setSessionNotice('Signed out cleanly. Session revoked server-side.');
    await refreshAuthStatus();
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPasswordError(null);
    setPasswordSuccess(null);
    if (newPassword !== confirmNewPassword) {
      setPasswordError('New password and confirmation do not match.');
      return;
    }
    setPasswordBusy(true);
    try {
      const res = await certaintyApi.changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setAuthUser(res.user);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmNewPassword('');
      setPasswordSuccess('Password updated and active session rotated.');
    } catch (err) {
      setPasswordError(err instanceof Error ? err.message : 'Failed to update password');
    } finally {
      setPasswordBusy(false);
    }
  };

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
    await certaintyApi.toggleKillSwitch({ active, reason });
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

  if (authChecking) {
    return (
      <div className="min-h-screen bg-[#0B111B] text-slate-300 flex items-center justify-center p-6 text-xs font-mono">
        Verifying server session...
      </div>
    );
  }

  if (!authUser) {
    if (!authStatus && error) {
      return (
        <div className="min-h-screen bg-[#0B111B] text-slate-100 flex items-center justify-center p-6">
          <div className="w-full max-w-md rounded-2xl border border-rose-900/70 bg-[#111827] p-6 space-y-4">
            <div className="text-sm font-semibold text-rose-300">
              FastAPI Security Service Unavailable
            </div>
            <p className="text-xs text-slate-300 leading-relaxed">{error}</p>
            <button
              type="button"
              onClick={() => {
                setAuthChecking(true);
                void refreshAuthStatus();
              }}
              className="px-4 py-2 text-xs font-semibold bg-sky-600 hover:bg-sky-500 text-white rounded-lg transition-colors"
            >
              Retry Connection
            </button>
          </div>
        </div>
      );
    }
    return (
      <AuthGateView
        authStatus={
          authStatus ?? {
            authenticated: false,
            needs_owner_bootstrap: false,
            env_mode: 'development',
            cookie_secure: false,
            tls_required: false,
            csrf_header_name: 'X-CSRF-Token',
            user: null,
          }
        }
        sessionNotice={sessionNotice || error}
        onAuthenticated={handleAuthenticated}
        onRefreshStatus={refreshAuthStatus}
      />
    );
  }

  const killSwitchActive = dashboard?.risk_gate.kill_switch.active ?? false;

  return (
    <div className="min-h-screen bg-[#0B111B] text-slate-100 flex flex-col lg:flex-row">
      <SidebarNav
        activeTab={activeTab}
        onSelectTab={setActiveTab}
        killSwitchActive={killSwitchActive}
        auditsCount={dashboard?.risk_gate.total_audits ?? 0}
        opportunitiesCount={dashboard?.historical_opportunities_count ?? 0}
        user={authUser}
      />

      <div className="flex-1 min-w-0 flex flex-col">
        <TopBar
          activeTab={activeTab}
          scanning={scanning}
          killSwitchActive={killSwitchActive}
          user={authUser}
          onRunScan={() => void handleRunScan()}
          onRefresh={() => {
            void loadAll(true);
            void loadIntelligenceCenter();
            void loadMarketIntel();
          }}
          onOpenChangePassword={() => {
            setPasswordError(null);
            setPasswordSuccess(null);
            setShowChangePassword(true);
          }}
          onLogout={() => void handleLogout()}
        />

        <main className="flex-1 p-6 max-w-[1440px] w-full mx-auto space-y-5">
          {authUser.must_rotate_password ? (
            <div
              role="status"
              className="px-4 py-3 rounded-lg border border-amber-700/70 bg-amber-950/30 text-xs text-amber-200 flex flex-wrap items-center justify-between gap-3"
            >
              <span>
                Your account is flagged for password rotation. Update your password to clear this notice.
              </span>
              <button
                type="button"
                onClick={() => {
                  setPasswordError(null);
                  setPasswordSuccess(null);
                  setShowChangePassword(true);
                }}
                className="px-3 py-1 font-semibold bg-amber-700 hover:bg-amber-600 text-white rounded"
              >
                Rotate Password Now
              </button>
            </div>
          ) : null}

          {forbiddenNotice ? (
            <div
              role="alert"
              className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200 flex items-center justify-between gap-4"
            >
              <span>{forbiddenNotice}</span>
              <button
                type="button"
                onClick={() => setForbiddenNotice(null)}
                className="text-rose-300 hover:text-white shrink-0"
              >
                Dismiss
              </button>
            </div>
          ) : null}

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

          {activeTab === 'access' && authUser.role === 'OWNER' ? (
            <AdminAccessView currentUser={authUser} />
          ) : loading && !dashboard ? (
            <div className="space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
                {[1, 2, 3, 4].map((idx) => (
                  <div
                    key={idx}
                    className="h-28 rounded-xl bg-[#111827] border border-slate-800 animate-pulse"
                  />
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
                  canRunScans={authUser.permissions.can_run_scans}
                  onSelectMarket={(id) => void loadSelectedBook(id)}
                  onRunScan={() => void handleRunScan()}
                  onNavigateTab={(tab) => setActiveTab(tab)}
                />
              ) : null}

              {activeTab === 'councils' ? (
                <MarketCouncilsView
                  data={marketIntelData}
                  loading={marketIntelLoading}
                  permissions={authUser.permissions}
                  onRefresh={async () => {
                    await Promise.all([loadMarketIntel(), loadIntelligenceCenter(), loadAll(false)]);
                  }}
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
                  canRunScans={authUser.permissions.can_run_scans}
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
                  permissions={authUser.permissions}
                  onRefreshKalshi={loadKalshiMarkets}
                  onRefreshCrossVenue={async () => {
                    await Promise.all([loadCrossVenueAndBlotter(), loadAll(false)]);
                  }}
                />
              ) : null}

              {activeTab === 'intelligence' ? (
                <IntelligenceCenterView
                  data={intelligenceData}
                  loading={intelligenceLoading}
                  permissions={authUser.permissions}
                  onRefresh={async () => {
                    await Promise.all([loadIntelligenceCenter(), loadAll(false)]);
                  }}
                />
              ) : null}

              {activeTab === 'risk' ? (
                <RiskGateAuditView
                  policy={dashboard.risk_gate.policy}
                  killSwitch={dashboard.risk_gate.kill_switch}
                  audits={dashboard.recent_audits}
                  permissions={authUser.permissions}
                  onToggleKillSwitch={handleToggleKillSwitch}
                  onEvaluateDryRun={handleEvaluateDryRun}
                />
              ) : null}

              {activeTab === 'architecture' ? (
                <MemoryArchitectureView
                  memories={dashboard.recent_memories}
                  modules={dashboard.modules}
                  permissions={authUser.permissions}
                  onCreateMemory={handleCreateMemory}
                />
              ) : null}
            </>
          ) : null}
        </main>
      </div>

      {/* Change Password Modal */}
      {showChangePassword ? (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="change-password-title"
          className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4"
        >
          <div className="w-full max-w-md border border-slate-700 bg-[#111827] rounded-xl p-5 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-800 pb-3">
              <div>
                <h2 id="change-password-title" className="text-sm font-semibold text-slate-100">
                  Change Account Password
                </h2>
                <p className="text-xs text-slate-400">
                  Principal: <span className="font-mono text-slate-200">{authUser.username}</span> ({authUser.role})
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowChangePassword(false)}
                aria-label="Close password modal"
                className="text-slate-400 hover:text-slate-200"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {passwordError ? (
              <div role="alert" className="p-3 rounded-lg bg-rose-950/40 border border-rose-800 text-xs text-rose-200">
                {passwordError}
              </div>
            ) : null}

            {passwordSuccess ? (
              <div role="status" className="p-3 rounded-lg bg-emerald-950/40 border border-emerald-800 text-xs text-emerald-200">
                {passwordSuccess}
              </div>
            ) : null}

            <form onSubmit={(e) => void handleChangePassword(e)} className="space-y-3 text-xs">
              <div>
                <label htmlFor="cp-current" className="block text-slate-300 mb-1">
                  Current Password
                </label>
                <input
                  id="cp-current"
                  type="password"
                  required
                  autoComplete="current-password"
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
                />
              </div>

              <div>
                <label htmlFor="cp-new" className="block text-slate-300 mb-1">
                  New Password (min 12 chars, 3 char classes)
                </label>
                <input
                  id="cp-new"
                  type="password"
                  required
                  minLength={12}
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
                />
              </div>

              <div>
                <label htmlFor="cp-confirm" className="block text-slate-300 mb-1">
                  Confirm New Password
                </label>
                <input
                  id="cp-confirm"
                  type="password"
                  required
                  minLength={12}
                  autoComplete="new-password"
                  value={confirmNewPassword}
                  onChange={(e) => setConfirmNewPassword(e.target.value)}
                  className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
                />
              </div>

              <div className="flex items-center justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowChangePassword(false)}
                  className="px-3 py-2 text-xs font-medium text-slate-300 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-lg"
                >
                  Close
                </button>
                <button
                  type="submit"
                  disabled={passwordBusy}
                  className="px-4 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-60 rounded-lg"
                >
                  {passwordBusy ? 'Updating...' : 'Update Password'}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
}
