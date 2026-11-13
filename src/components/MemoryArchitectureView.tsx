import { useState } from 'react';
import type { AgentMemoryEntry, ModuleRegistryItem } from '../types/certainty';
import { formatTimestampShort } from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface MemoryArchitectureViewProps {
  memories: AgentMemoryEntry[];
  modules: ModuleRegistryItem[];
  onCreateMemory: (payload: {
    category: string;
    title: string;
    summary: string;
    status: string;
  }) => Promise<void>;
}

const PHASE0_FINDINGS = [
  {
    item: 'Polymarket Gamma API (gamma-api.polymarket.com)',
    finding:
      'Verified public HTTP 200 read-only access. outcomes and clobTokenIds arrive as JSON-encoded strings; feeSchedule exposes rate, exponent, takerOnly, and rebateRate.',
    status: 'VERIFIED',
  },
  {
    item: 'Polymarket CLOB V2 API (clob.polymarket.com/book)',
    finding:
      'Default Python urllib User-Agent receives HTTP 403; httpx with explicit User-Agent succeeds. Live bids arrive sorted ASC and asks arrive sorted DESC, requiring explicit sorting before VWAP.',
    status: 'VERIFIED',
  },
  {
    item: 'Kalshi Trade API v2 (api.elections.kalshi.com)',
    finding:
      'Verified reachable for Phase 2 planning. Cross-venue paper execution and matching remain disabled until Phase 1 passes acceptance and Phase 2 is approved.',
    status: 'PHASE 2',
  },
  {
    item: 'Execution & Signing Isolation',
    finding:
      'Zero wallet private keys, mnemonics, or order-submission clients in frontend or backend. py-clob-client V1 omitted.',
    status: 'ENFORCED',
  },
];

export function MemoryArchitectureView({
  memories,
  modules,
  onCreateMemory,
}: MemoryArchitectureViewProps) {
  const [category, setCategory] = useState('VERIFIED_LESSON');
  const [title, setTitle] = useState('');
  const [summary, setSummary] = useState('');
  const [saving, setSaving] = useState(false);

  const handleSaveMemory = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || !summary.trim()) return;
    setSaving(true);
    try {
      await onCreateMemory({
        category,
        title: title.trim(),
        summary: summary.trim(),
        status: 'RECORDED',
      });
      setTitle('');
      setSummary('');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Phase 0 Dependency & API Audit */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-slate-100">
              Phase 0 Architecture &amp; Dependency Audit
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              Verified API behaviors, dependency constraints, and security boundaries documented in docs/PHASE0_AUDIT.md.
            </p>
          </div>
          <ProvenanceLabel provenance="LIVE" />
        </div>

        <div className="divide-y divide-slate-800 text-xs">
          {PHASE0_FINDINGS.map((row) => (
            <div key={row.item} className="py-3 flex flex-col sm:flex-row sm:items-start justify-between gap-3">
              <div className="max-w-3xl">
                <div className="font-medium text-slate-100">{row.item}</div>
                <div className="mt-1 text-slate-400 leading-relaxed">{row.finding}</div>
              </div>
              <span className="font-mono font-semibold text-emerald-400 shrink-0">{row.status}</span>
            </div>
          ))}
        </div>
      </section>

      {/* Inactive Future Modules Blueprint */}
      <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">
              Phase 2, Tier 2 &amp; Tier 3 Module Boundaries
            </h2>
            <p className="text-xs text-slate-400 mt-0.5">
              All Kalshi, paper execution, crypto perpetuals, and AI research modules remain strictly inactive until Phase 1 approval.
            </p>
          </div>
          <ProvenanceLabel provenance="UNAVAILABLE" />
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
          {modules.map((m) => (
            <div key={m.module_id} className="p-4 rounded-lg bg-[#0E1522] border border-slate-800 space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-slate-100">{m.name}</span>
                <span
                  className={`font-mono font-semibold ${
                    m.status.startsWith('ACTIVE') ? 'text-emerald-400' : 'text-slate-500'
                  }`}
                >
                  {m.status}
                </span>
              </div>
              <div className="text-slate-400">{m.description}</div>
              <div className="pt-1 font-mono text-[11px] text-slate-500">
                {m.phase} · {m.execution_capability}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Persistent Memory & Escalation Store */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <section className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4">
          <h2 className="text-sm font-semibold text-slate-100">
            Record Verified Lesson or Blocker
          </h2>
          <form onSubmit={handleSaveMemory} className="space-y-3 text-xs">
            <div>
              <label htmlFor="mem-cat" className="block text-slate-300 mb-1">Category</label>
              <select
                id="mem-cat"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100"
              >
                <option value="VERIFIED_LESSON">VERIFIED_LESSON</option>
                <option value="MARKET_DATA_ERROR">MARKET_DATA_ERROR</option>
                <option value="BLOCKER_ESCALATION">BLOCKER_ESCALATION</option>
                <option value="CALIBRATION_NOTE">CALIBRATION_NOTE</option>
              </select>
            </div>
            <div>
              <label htmlFor="mem-title" className="block text-slate-300 mb-1">Title</label>
              <input
                id="mem-title"
                type="text"
                required
                minLength={3}
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g., NegRisk condition pairing rule"
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 placeholder:text-slate-500"
              />
            </div>
            <div>
              <label htmlFor="mem-summary" className="block text-slate-300 mb-1">Summary</label>
              <textarea
                id="mem-summary"
                rows={3}
                required
                minLength={5}
                value={summary}
                onChange={(e) => setSummary(e.target.value)}
                placeholder="Document verified observation, evidence, or safe mitigation..."
                className="w-full px-3 py-2 bg-[#0B111B] border border-slate-700 rounded-lg text-slate-100 placeholder:text-slate-500"
              />
            </div>
            <button
              type="submit"
              disabled={saving}
              className="w-full px-4 py-2 font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-60 rounded-lg transition-colors"
            >
              {saving ? 'Saving to DuckDB...' : 'Persist to Agent Memory'}
            </button>
          </form>
        </section>

        <section className="lg:col-span-2 border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-100">
              Provider-Independent Persistent Memory ({memories.length})
            </h2>
            <ProvenanceLabel provenance="LIVE" detail="DuckDB Persisted" />
          </div>
          <div className="divide-y divide-slate-800 text-xs">
            {memories.map((mem) => (
              <div key={mem.memory_id} className="py-3 space-y-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold text-slate-100">{mem.title}</span>
                  <span className="font-mono text-[11px] text-slate-400">
                    {mem.category} · {mem.status}
                  </span>
                </div>
                <p className="text-slate-400 leading-relaxed">{mem.summary}</p>
                <div className="font-mono text-[11px] text-slate-500">
                  {mem.memory_id} · {formatTimestampShort(mem.created_at)}
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
