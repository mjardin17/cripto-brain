import {
  Activity,
  ArrowLeftRight,
  BookOpen,
  BrainCircuit,
  Coins,
  KeyRound,
  Layers,
  ShieldAlert,
  TrendingUp,
} from 'lucide-react';
import type { AuthenticatedUserRecord } from '../types/certainty';
import { canAccessWorkspaceTab, describeUserRole } from '../utils/formatters';

export type WorkspaceTab =
  | 'command'
  | 'councils'
  | 'scanner'
  | 'cross_venue'
  | 'solana'
  | 'intelligence'
  | 'risk'
  | 'architecture'
  | 'access';

interface SidebarNavProps {
  activeTab: WorkspaceTab;
  onSelectTab: (tab: WorkspaceTab) => void;
  killSwitchActive: boolean;
  auditsCount: number;
  opportunitiesCount: number;
  user: AuthenticatedUserRecord;
}

const NAV_ITEMS: Array<{
  id: WorkspaceTab;
  label: string;
  subtitle: string;
  Icon: typeof Activity;
}> = [
  {
    id: 'command',
    label: 'Command Center',
    subtitle: 'Portfolio, exposure & overview',
    Icon: Activity,
  },
  {
    id: 'councils',
    label: 'AI Councils & Drivers',
    subtitle: 'Crypto/Stocks councils & WHY/WHEN',
    Icon: TrendingUp,
  },
  {
    id: 'scanner',
    label: 'Scanner & Depth',
    subtitle: 'Polymarket CLOB V2 books',
    Icon: Layers,
  },
  {
    id: 'cross_venue',
    label: 'Cross-Venue & Paper',
    subtitle: 'Kalshi matcher & simulator',
    Icon: ArrowLeftRight,
  },
  {
    id: 'solana',
    label: 'Solana Research Desk',
    subtitle: '6-agent DEX & paper ledger',
    Icon: Coins,
  },
  {
    id: 'intelligence',
    label: 'Intelligence & Agents',
    subtitle: 'Supervisor, calibration & routing',
    Icon: BrainCircuit,
  },
  {
    id: 'risk',
    label: 'Risk Gate & Audit',
    subtitle: 'Fail-closed policy & kill switch',
    Icon: ShieldAlert,
  },
  {
    id: 'architecture',
    label: 'Memory & Audit Docs',
    subtitle: 'Phase 0 audit & Tier 2/3 stubs',
    Icon: BookOpen,
  },
  {
    id: 'access',
    label: 'Access & Security',
    subtitle: 'Users, RBAC, sessions & audit',
    Icon: KeyRound,
  },
];

export function SidebarNav({
  activeTab,
  onSelectTab,
  killSwitchActive,
  auditsCount,
  opportunitiesCount,
  user,
}: SidebarNavProps) {
  const visibleItems = NAV_ITEMS.filter((item) => canAccessWorkspaceTab(user.role, item.id));
  const roleBadge = describeUserRole(user.role);

  return (
    <aside className="w-full lg:w-64 shrink-0 border-b lg:border-b-0 lg:border-r border-slate-800 bg-[#0E1522] flex flex-col justify-between">
      <div className="p-4 lg:p-5">
        <div className="pb-3 lg:pb-5 border-b border-slate-800/80 flex items-center justify-between lg:block">
          <div>
            <div className="text-base font-semibold tracking-tight text-slate-50">
              The Certainty Stack
            </div>
            <div className="mt-0.5 lg:mt-1 text-xs text-slate-400">
              Phase 4A · Authenticated Research
            </div>
          </div>
          <span
            className={`lg:hidden text-xs font-mono font-semibold ${
              killSwitchActive ? 'text-rose-400' : 'text-emerald-400'
            }`}
          >
            {killSwitchActive ? 'HALTED' : 'FAIL-CLOSED'}
          </span>
        </div>

        <nav
          className="mt-3 lg:mt-5 flex lg:flex-col gap-1.5 overflow-x-auto lg:overflow-visible pb-1 lg:pb-0"
          aria-label="Primary workspace navigation"
        >
          {visibleItems.map(({ id, label, subtitle, Icon }) => {
            const active = activeTab === id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => onSelectTab(id)}
                aria-current={active ? 'page' : undefined}
                className={`shrink-0 lg:w-full text-left px-3 py-2 lg:py-2.5 rounded-lg transition-colors flex items-center lg:items-start gap-2 lg:gap-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500 ${
                  active
                    ? 'bg-slate-800/90 text-slate-50 border border-slate-700'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/40 border border-transparent'
                }`}
              >
                <Icon className={`w-4 h-4 lg:mt-0.5 shrink-0 ${active ? 'text-sky-400' : 'text-slate-500'}`} />
                <div className="min-w-0">
                  <div className="text-xs lg:text-sm font-medium whitespace-nowrap truncate">{label}</div>
                  <div className="hidden lg:block text-xs text-slate-500 truncate">{subtitle}</div>
                </div>
              </button>
            );
          })}
        </nav>
      </div>

      <div className="hidden lg:block p-5 border-t border-slate-800/80 space-y-3 text-xs">
        <div className="p-2.5 rounded-lg bg-[#0B111B] border border-slate-800 flex items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="font-mono font-semibold text-slate-100 truncate">{user.username}</div>
            <div className="text-[11px] text-slate-500 truncate">Session {user.session_id}</div>
          </div>
          <span className={`px-2 py-0.5 rounded border text-[10px] font-mono font-semibold shrink-0 ${roleBadge.toneClass}`}>
            {roleBadge.label}
          </span>
        </div>
        <div className="flex items-center justify-between text-slate-400">
          <span>Execution Lock</span>
          <span className="font-mono text-slate-200">READ-ONLY</span>
        </div>
        <div className="flex items-center justify-between text-slate-400">
          <span>Risk Gate</span>
          <span
            className={`font-mono font-semibold ${
              killSwitchActive ? 'text-rose-400' : 'text-emerald-400'
            }`}
          >
            {killSwitchActive ? 'HALTED' : 'FAIL-CLOSED'}
          </span>
        </div>
        <div className="flex items-center justify-between text-slate-400">
          <span>DuckDB Opps</span>
          <span className="font-mono tabular-nums text-slate-200">{opportunitiesCount}</span>
        </div>
        <div className="flex items-center justify-between text-slate-400">
          <span>Audit Records</span>
          <span className="font-mono tabular-nums text-slate-200">{auditsCount}</span>
        </div>
      </div>
    </aside>
  );
}
