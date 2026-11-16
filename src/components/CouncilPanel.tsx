import { useCallback, useEffect, useState } from 'react';
import { certaintyApi } from '../api/client';
import type { CouncilDefinition, CouncilSession } from '../types/certainty';
import { formatTimestampShort } from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface CouncilPanelProps {
  canDispatchAgents: boolean;
  onRefresh: () => Promise<void>;
}

const STANCE_TONE: Record<string, string> = {
  GO: 'text-emerald-300 bg-emerald-950/40 border-emerald-900',
  NO: 'text-rose-300 bg-rose-950/40 border-rose-900',
  HOLD: 'text-amber-200 bg-amber-950/40 border-amber-900',
  ABSTAIN: 'text-slate-300 bg-slate-900/60 border-slate-700',
};

function stanceLabel(stance: string): string {
  if (stance === 'GO') return 'GO';
  if (stance === 'NO') return 'VETO';
  if (stance === 'ABSTAIN') return 'ABSTAIN';
  return 'HOLD';
}

function CouncilCard({
  council,
  lastSession,
  busy,
  onConvene,
}: {
  council: CouncilDefinition;
  lastSession: CouncilSession | null;
  busy: boolean;
  onConvene: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const verdict = lastSession?.verdict_detail;
  return (
    <div className="border border-slate-800 bg-[#0E1522] rounded-xl p-5 space-y-4 flex flex-col">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-slate-100">{council.label}</h3>
          <p className="text-[11px] text-slate-500 mt-0.5">
            {council.members.length} members · deterministic · $0 model cost
          </p>
        </div>
        <ProvenanceLabel provenance="SIMULATED" detail="Advisory Only · Cannot Trade" />
      </div>

      <ul className="space-y-1.5">
        {council.members.map((m) => (
          <li key={m.member_id} className="flex items-start justify-between gap-2 text-xs">
            <div className="min-w-0">
              <span className="text-slate-200">{m.name}</span>
              <span className="text-slate-500"> · {m.role}</span>
              {expanded ? (
                <p className="text-[11px] text-slate-400 mt-0.5">{m.description}</p>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="self-start text-[11px] text-sky-400 hover:text-sky-300"
      >
        {expanded ? 'Hide member briefs' : 'Show member briefs'}
      </button>

      {lastSession ? (
        <div className="pt-3 border-t border-slate-800 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="text-slate-400">Last session</span>
            <span className="font-mono text-slate-400">{formatTimestampShort(lastSession.convened_at)}</span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`px-2 py-0.5 rounded border text-[11px] font-semibold ${
                lastSession.verdict === 'GO' ? STANCE_TONE.GO : STANCE_TONE.HOLD
              }`}
            >
              {lastSession.verdict}
            </span>
            {verdict ? (
              <span className="text-[11px] text-slate-400 font-mono">
                {verdict.go_votes} GO · {verdict.no_votes} VETO · {verdict.abstain_votes} abstain ·
                confidence {(lastSession.confidence * 100).toFixed(0)}%
              </span>
            ) : null}
          </div>
          <ul className="space-y-1">
            {lastSession.member_stances.map((s) => (
              <li key={s.member_id} className="flex items-center justify-between gap-2 text-[11px]">
                <span className="text-slate-300">{s.name}</span>
                <span
                  className={`px-1.5 py-0.5 rounded border shrink-0 font-mono ${STANCE_TONE[s.stance] ?? STANCE_TONE.HOLD}`}
                >
                  {stanceLabel(s.stance)}
                </span>
              </li>
            ))}
          </ul>
          <p className="text-[11px] text-slate-400 leading-snug">{verdict?.rationale}</p>
        </div>
      ) : (
        <p className="text-xs text-slate-500 pt-3 border-t border-slate-800">
          No session yet — convene the council to get its first advisory verdict.
        </p>
      )}

      <button
        type="button"
        onClick={onConvene}
        disabled={busy}
        className="mt-auto px-3.5 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
      >
        {busy ? 'Convening…' : 'Convene now'}
      </button>
    </div>
  );
}

export function CouncilPanel({ canDispatchAgents, onRefresh }: CouncilPanelProps) {
  const [councils, setCouncils] = useState<CouncilDefinition[] | null>(null);
  const [sessions, setSessions] = useState<Record<string, CouncilSession | null>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSessions = useCallback(async () => {
    try {
      const res = await certaintyApi.getCouncils();
      setCouncils(res.councils);
      const next: Record<string, CouncilSession | null> = {};
      for (const c of res.councils) {
        try {
          const s = await certaintyApi.getCouncilSessions(c.domain, 1);
          next[c.domain] = s.items[0] ?? null;
        } catch {
          next[c.domain] = null;
        }
      }
      setSessions(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load councils');
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const convene = async (domain: 'STOCKS' | 'CRYPTO') => {
    setBusy(domain);
    setError(null);
    try {
      await certaintyApi.conveneCouncil(domain);
      await loadSessions();
      await onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Convening the ${domain} council failed`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section
      className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4"
      aria-label="Council deliberation teams"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex items-center gap-2.5">
            <h2 className="text-sm font-semibold text-slate-100">Council Deliberation Teams</h2>
            <ProvenanceLabel provenance="SIMULATED" detail="Advisory · Fail-Closed · Paper Only" />
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Two domain councils — Stocks and Crypto — deliberate in three rounds over the app's own
            market data. The Risk Sentinel holds a veto, and a council can only advise GO when a
            Risk-Gate-approved opportunity exists in its domain.
          </p>
        </div>
        {!canDispatchAgents ? (
          <span className="text-[11px] text-slate-500">Convening requires OPERATOR or OWNER role.</span>
        ) : null}
      </div>

      {error ? (
        <div className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200">{error}</div>
      ) : null}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {(councils ?? []).map((c) => (
          <CouncilCard
            key={c.domain}
            council={c}
            lastSession={sessions[c.domain] ?? null}
            busy={busy === c.domain}
            onConvene={() => void convene(c.domain)}
          />
        ))}
        {councils === null && !error ? (
          <div className="md:col-span-2 p-6 text-center text-xs text-slate-400">Loading councils…</div>
        ) : null}
      </div>
    </section>
  );
}
