import { RefreshCw } from 'lucide-react';
import type { WorkspaceTab } from './SidebarNav';

interface TopBarProps {
  activeTab: WorkspaceTab;
  scanning: boolean;
  killSwitchActive: boolean;
  onRunScan: () => void;
  onRefresh: () => void;
}

const TAB_TITLES: Record<WorkspaceTab, string> = {
  command: 'Workspace / Command Center',
  scanner: 'Workspace / Tier 1 Polymarket Scanner & Order Book',
  risk: 'Workspace / Deterministic Risk Gate & Audit Trail',
  architecture: 'Workspace / Persistent Memory & Phase 0 Audit',
};

export function TopBar({
  activeTab,
  scanning,
  killSwitchActive,
  onRunScan,
  onRefresh,
}: TopBarProps) {
  return (
    <header className="border-b border-slate-800 bg-[#0E1522]/90 px-4 sm:px-6 py-3.5 sm:py-4 flex flex-wrap items-center justify-between gap-3 sm:gap-4">
      <div className="min-w-0">
        <div className="text-sm font-medium text-slate-300 truncate">{TAB_TITLES[activeTab]}</div>
      </div>

      <div className="flex flex-wrap items-center gap-2.5 sm:gap-3 shrink-0">
        <span
          className={`hidden sm:inline text-xs font-mono font-medium whitespace-nowrap ${
            killSwitchActive ? 'text-rose-400' : 'text-slate-400'
          }`}
        >
          {killSwitchActive ? 'KILL SWITCH ACTIVE' : 'Live Trading: Disabled'}
        </span>

        <button
          type="button"
          onClick={onRefresh}
          aria-label="Refresh dashboard data"
          className="px-3 py-2 text-xs font-medium text-slate-300 bg-slate-800/70 hover:bg-slate-800 border border-slate-700 rounded-lg transition-colors whitespace-nowrap shrink-0 inline-flex items-center gap-1.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          <span>Refresh</span>
        </button>

        <button
          type="button"
          onClick={onRunScan}
          disabled={scanning}
          className="px-3.5 sm:px-4 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-60 disabled:cursor-not-allowed rounded-lg transition-colors whitespace-nowrap shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
        >
          {scanning ? 'Scanning CLOB V2...' : 'Scan Polymarket CLOB'}
        </button>
      </div>
    </header>
  );
}
