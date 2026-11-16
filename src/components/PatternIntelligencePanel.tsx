import { useEffect, useState } from 'react';
import { certaintyApi } from '../api/client';
import type { EventReactionRecord, PatternPrediction } from '../types/certainty';
import { formatTimestampShort } from '../utils/formatters';
import { ProvenanceLabel } from './ProvenanceLabel';

interface PatternIntelligencePanelProps {
  canDispatchAgents: boolean;
}

const DIRECTION_TONE: Record<string, string> = {
  UP: 'text-emerald-300',
  DOWN: 'text-rose-300',
  FLAT: 'text-slate-400',
};

const DOMAINS: Array<'STOCKS' | 'CRYPTO'> = ['CRYPTO', 'STOCKS'];

export function PatternIntelligencePanel({ canDispatchAgents }: PatternIntelligencePanelProps) {
  const [reactions, setReactions] = useState<EventReactionRecord[]>([]);
  const [analysis, setAnalysis] = useState<PatternPrediction | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const importCurated = async () => {
    setBusy('curated');
    setError(null);
    setMessage(null);
    try {
      const res = await certaintyApi.importCuratedHistory();
      setMessage(
        `Imported ${res.imported} curated real events (${res.skipped_duplicates} already present)` +
          (res.purged_test_analogs ? ` and removed ${res.purged_test_analogs} test analogs.` : '.')
      );
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Curated import failed');
    } finally {
      setBusy(null);
    }
  };

  // Record form
  const [domain, setDomain] = useState<'STOCKS' | 'CRYPTO'>('CRYPTO');
  const [eventType, setEventType] = useState('NATURAL_DISASTER');
  const [eventLabel, setEventLabel] = useState('');
  const [occurredAt, setOccurredAt] = useState('');
  const [direction, setDirection] = useState('DOWN');
  const [magnitude, setMagnitude] = useState('');
  const [narrative, setNarrative] = useState('');

  const load = async () => {
    try {
      const res = await certaintyApi.getEventReactions();
      setReactions(res.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load recorded events');
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const record = async () => {
    setBusy('record');
    setError(null);
    setMessage(null);
    try {
      await certaintyApi.recordEventReaction({
        domain,
        event_type: eventType,
        event_label: eventLabel || `${eventType} event`,
        occurred_at: occurredAt || new Date().toISOString(),
        reaction_direction: direction,
        narrative,
        reaction_magnitude_pct: magnitude === '' ? null : Number(magnitude),
      });
      setMessage('Historical event reaction recorded — it now feeds pattern predictions.');
      setEventLabel('');
      setNarrative('');
      setMagnitude('');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Recording failed');
    } finally {
      setBusy(null);
    }
  };

  const predict = async (targetDomain: 'STOCKS' | 'CRYPTO', targetEventType: string) => {
    setBusy('predict');
    setError(null);
    setMessage(null);
    try {
      const res = await certaintyApi.predictEventPattern(targetDomain, targetEventType);
      setAnalysis(res);
      if (!res.predictable) {
        setMessage(res.reason ?? 'No analogs recorded yet for this event type.');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Pattern prediction failed');
    } finally {
      setBusy(null);
    }
  };

  const canRecord =
    Boolean(eventType.trim()) &&
    Boolean(narrative.trim()) &&
    narrative.trim().length >= 10 &&
    canDispatchAgents;

  return (
    <section
      className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4"
      aria-label="Event pattern intelligence"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex items-center gap-2.5">
            <h2 className="text-sm font-semibold text-slate-100">Event Pattern Intelligence</h2>
            <ProvenanceLabel provenance="SIMULATED" detail="Advisory · Brier-Scored Feedback Loop" />
            <button
              type="button"
              onClick={() => void importCurated()}
              disabled={busy !== null || !canDispatchAgents}
              title={canDispatchAgents ? 'Import the curated set of real documented events' : 'Requires OPERATOR or OWNER role'}
              className="px-2.5 py-1 rounded-md text-[11px] font-medium border border-slate-700 text-slate-300 hover:border-sky-600 hover:text-sky-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {busy === 'curated' ? 'Importing…' : 'Import real events'}
            </button>
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Record how markets reacted to past events (disasters, shocks, outages). When a similar
            event approaches, the analog base rate drives a prediction that is tracked and scored, so
            each resolved event makes the next call measurably better.
          </p>
        </div>
      </div>

      {error ? (
        <div className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200">{error}</div>
      ) : null}
      {message ? (
        <div className="px-4 py-3 rounded-lg border border-sky-800/70 bg-sky-950/30 text-xs text-sky-200">{message}</div>
      ) : null}

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {/* Record a historical event reaction */}
        <div className="space-y-3">
          <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wide">
            Record historical reaction
          </h3>
          <div className="grid grid-cols-2 gap-3">
            <label className="space-y-1 text-[11px] text-slate-400">
              Domain
              <select
                value={domain}
                onChange={(e) => setDomain(e.target.value as 'STOCKS' | 'CRYPTO')}
                disabled={!canDispatchAgents}
                className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-xs text-slate-100"
              >
                {DOMAINS.map((d) => (
                  <option key={d} value={d}>{d}</option>
                ))}
              </select>
            </label>
            <label className="space-y-1 text-[11px] text-slate-400">
              Event type
              <input
                type="text"
                value={eventType}
                onChange={(e) => setEventType(e.target.value.toUpperCase())}
                disabled={!canDispatchAgents}
                className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-xs text-slate-100 font-mono"
              />
            </label>
            <label className="space-y-1 text-[11px] text-slate-400">
              Event label
              <input
                type="text"
                value={eventLabel}
                onChange={(e) => setEventLabel(e.target.value)}
                disabled={!canDispatchAgents}
                placeholder="Hurricane Cat-5 landfall"
                className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-xs text-slate-100"
              />
            </label>
            <label className="space-y-1 text-[11px] text-slate-400">
              Occurred at
              <input
                type="date"
                value={occurredAt}
                onChange={(e) => setOccurredAt(e.target.value)}
                disabled={!canDispatchAgents}
                className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-xs text-slate-100 font-mono"
              />
            </label>
            <label className="space-y-1 text-[11px] text-slate-400">
              Direction
              <select
                value={direction}
                onChange={(e) => setDirection(e.target.value)}
                disabled={!canDispatchAgents}
                className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-xs text-slate-100"
              >
                <option value="DOWN">DOWN</option>
                <option value="UP">UP</option>
                <option value="FLAT">FLAT</option>
              </select>
            </label>
            <label className="space-y-1 text-[11px] text-slate-400">
              Magnitude %
              <input
                type="number"
                step="0.1"
                value={magnitude}
                onChange={(e) => setMagnitude(e.target.value)}
                disabled={!canDispatchAgents}
                placeholder="-6.5"
                className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-xs text-slate-100 font-mono"
              />
            </label>
          </div>
          <label className="block space-y-1 text-[11px] text-slate-400">
            Narrative (what happened, how markets reacted)
            <textarea
              value={narrative}
              onChange={(e) => setNarrative(e.target.value)}
              disabled={!canDispatchAgents}
              rows={2}
              maxLength={2000}
              className="w-full px-2.5 py-1.5 bg-[#0B111B] border border-slate-700 rounded-lg text-xs text-slate-100"
            />
          </label>
          <button
            type="button"
            onClick={() => void record()}
            disabled={!canRecord || busy !== null}
            title={canDispatchAgents ? 'Record this historical reaction' : 'Requires OPERATOR or OWNER role'}
            className="px-3.5 py-2 text-xs font-semibold text-white bg-sky-600 hover:bg-sky-500 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
          >
            {busy === 'record' ? 'Recording…' : 'Record event reaction'}
          </button>

          {reactions.length ? (
            <div className="pt-3 border-t border-slate-800 space-y-1.5 max-h-56 overflow-y-auto pr-1">
              <div className="text-[11px] text-slate-500 uppercase tracking-wide">
                Recorded analogs ({reactions.length})
              </div>
              {reactions.slice(0, 12).map((r) => (
                <div key={r.reaction_id} className="flex items-center justify-between gap-2 text-[11px]">
                  <span className="text-slate-300 truncate">
                    <span className={`font-mono ${DIRECTION_TONE[r.reaction_direction] ?? ''}`}>
                      {r.reaction_direction}
                    </span>{' '}
                    {r.reaction_magnitude_pct !== null ? `${r.reaction_magnitude_pct}%` : ''} ·{' '}
                    {r.event_type}
                  </span>
                  <span className="text-slate-500 font-mono shrink-0">{formatTimestampShort(r.occurred_at)}</span>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        {/* Upcoming event prediction */}
        <div className="space-y-3">
          <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wide">
            Predict reaction to an upcoming event
          </h3>
          <div className="flex flex-wrap gap-2">
            {DOMAINS.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => void predict(d, eventType)}
                disabled={busy !== null || !canDispatchAgents}
                title={canDispatchAgents ? `Run analog-based prediction for ${d}` : 'Requires ANALYST role or higher'}
                className="px-3 py-1.5 text-[11px] font-semibold rounded-lg border border-slate-700 text-slate-200 hover:border-sky-600 hover:text-sky-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {busy === 'predict' ? 'Analyzing…' : `Predict ${d} for ${eventType || 'event'}`}
              </button>
            ))}
          </div>

          {analysis ? (
            <div className="p-4 rounded-lg border border-slate-800 bg-[#0E1522] space-y-3">
              {analysis.predictable && analysis.prediction ? (
                <>
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="text-slate-400">
                      {analysis.domain} · {analysis.analog_count} analogs
                    </span>
                    <span className={`font-mono font-semibold ${DIRECTION_TONE[analysis.prediction.direction] ?? ''}`}>
                      {analysis.prediction.direction}{' '}
                      {analysis.prediction.expected_magnitude_pct != null
                        ? `${analysis.prediction.expected_magnitude_pct}%`
                        : ''}
                    </span>
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div className="p-2 rounded-lg bg-[#0B111B]">
                      <div className="text-[10px] text-slate-500 uppercase">Probability</div>
                      <div className="text-sm font-mono text-slate-100">
                        {(analysis.prediction.probability * 100).toFixed(0)}%
                      </div>
                    </div>
                    <div className="p-2 rounded-lg bg-[#0B111B]">
                      <div className="text-[10px] text-slate-500 uppercase">Base rate</div>
                      <div className="text-sm font-mono text-slate-100">
                        {analysis.base_rate != null ? `${(analysis.base_rate * 100).toFixed(0)}%` : '—'}
                      </div>
                    </div>
                    <div className="p-2 rounded-lg bg-[#0B111B]">
                      <div className="text-[10px] text-slate-500 uppercase">Confidence</div>
                      <div className="text-sm font-mono text-slate-100">
                        {analysis.confidence != null ? `${(analysis.confidence * 100).toFixed(0)}%` : '—'}
                      </div>
                    </div>
                  </div>
                  {analysis.insufficient_sample ? (
                    <p className="text-[11px] text-amber-300">
                      Small analog set — record more historical reactions to sharpen this call.
                    </p>
                  ) : null}
                  <p className="text-[11px] text-slate-400 leading-snug">{analysis.prediction.note}</p>
                  {analysis.recorded_prediction_id ? (
                    <p className="text-[11px] text-emerald-300 font-mono">
                      Tracked as prediction {analysis.recorded_prediction_id} — resolve it after the
                      event so Brier scoring improves the next call.
                    </p>
                  ) : null}
                </>
              ) : (
                <p className="text-xs text-slate-400">
                  {analysis.reason ?? 'Not enough analogs to make a prediction.'}
                </p>
              )}
            </div>
          ) : (
            <div className="p-4 rounded-lg border border-dashed border-slate-800 text-xs text-slate-500">
              Run a prediction to see the analog-based call for an upcoming event.
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
