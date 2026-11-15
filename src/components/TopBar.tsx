import { KeyRound, LogOut, RefreshCw } from 'lucide-react';
import type { AuthenticatedUserRecord } from '../types/certainty';
import { describeUserRole } from '../utils/formatters';
import type { WorkspaceTab } from './SidebarNav';

interface TopBarProps {
  activeTab: WorkspaceTab;
  scanning: boolean;
  killSwitchActive: boolean;
  user: AuthenticatedUserRecord;
  onRunScan: () => void;
  onRefresh: () => void;
  onOpenChangePassword: () => void;
  onLogout: () => void;
}

const TAB_TITLES: Record<WorkspaceTab, string> = {
  command: 'Workspace / Command Center',
  scanner: 'Workspace / Tier 1 Polymarket Scanner & Order Book',
  cross_venue: 'Workspace / Kalshi Cross-Venue & Paper Execution Simulator',
  intelligence: 'Workspace / Intelligence, Calibration & Agent Command Center',
  risk: 'Workspace / Deterministic Risk Gate & Audit Trail',
  architecture: 'Workspace / Persistent Memory & Phase 0 Audit',
  access: 'Workspace / Access Control, Users & Security Audit',
};

export function TopBar({
  activeTab,
  scanning,
  killSwitchActive,
  user,
  onRunScan,
  onRefresh,
  onOpenChangePassword,
  onLogout,
}: TopBarProps) {
  const roleBadge = describeUserRole(user.role);
  const canScan = user.permissions.can_run_scans;

  return (
    <header className="border-b border-slate-800 bg-[#0E1522]/90 px-4 sm:px-6 py-3.5 sm:py-4 flex flex-wrap items-center justify-between gap-3 sm:gap-4">
      <div className="min-w-0">
        <div className="text-sm font-medium text-slate-300 truncate">{TAB_TITLES[activeTab]}</div>
      </div>

      <div className="flex flex-wrap items-center gap-2 sm:gap-2.5 max-w-full">
        <span
          className={`hidden xl:inline text-xs font-mono font-medium whitespace-nowrap ${
            killSwitchActive ? 'text-rose-400' : 'text-slate-400'
          }`}
        >
          {killSwitchActive ? 'KILL SWITCH ACTIVE' : 'Live Trading: Disabled'}
        </span>

        <div
          className="inline-flex items-center gap-2 px-2.5 py-1.5 rounded-lg border border-slate-800 bg-[#0B111B] text-xs"
          data-testid="topbar-user-badge"
        >
          <span className="font-mono text-slate-200">{user.username}</span>
          <span className={`px-1.5 py-0.5 rounded border text-[10px] font-mono font-semibold ${roleBadge.toneClass}`}>
            {roleBadge.label}
          </span>
        </div>

        <button
          type="button"
          onClick={onOpenChangePassword}
          aria-label="Change Password"
          className="px-2.5 py-2 text-xs font-medium text-slate-300 bg-slate-800/70 hover:bg-slate-800 border border-slate-700 rounded-lg transition-colors whitespace-nowrap shrink-0 inline-flex items-center gap-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500"
        >
          <KeyRound className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">Password</span>
        </button>

        <button
          type="button"
          onClick={onRefresh}
          aria-label="Refresh dashboard data"
          className="px-2.5 py-2 text-xs font-medium text-slate-300 bg-slate-800/70 hover:bg-slate-800 border border-slate-700 rounded-lg transition-colors whitespace-nowrap shrink-0 inline-flex items-center gap-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          <span>Refresh</span>
        </button>

        <button
          type="button"
          onClick={onRunScan}
          disabled={scanning || !canScan}
          title={canScan ? 'Run live Polymarket CLOB scan' : 'Requires OPERATOR or OWNER role'}
          className="px-3.5 sm:px-4 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors whitespace-nowrap shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
        >
          {scanning ? 'Scanning CLOB V2...' : 'Scan Polymarket CLOB'}
        </button>

        <button
          type="button"
          onClick={onLogout}
          aria-label="Sign Out"
          className="px-2.5 py-2 text-xs font-medium text-slate-300 bg-slate-800/70 hover:bg-rose-950/70 hover:text-rose-200 hover:border-rose-800 border border-slate-700 rounded-lg transition-colors whitespace-nowrap shrink-0 inline-flex items-center gap-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-500"
        >
          <LogOut className="w-3.5 h-3.5" />
          <span>Sign Out</span>
        </button>
      </div>
    </header>
  );
}
