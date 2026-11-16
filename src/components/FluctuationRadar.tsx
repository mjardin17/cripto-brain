import { useEffect, useState } from 'react';
import { certaintyApi } from '../api/client';
import type {
  FluctuationAnalysis,
  FluctuationForecastResponse,
} from '../types/certainty';
import { ProvenanceLabel } from './ProvenanceLabel';

interface FluctuationRadarProps {
  canDispatchAgents: boolean;
}

const DOMAINS: Array<'STOCKS' | 'CRYPTO'> = ['CRYPTO', 'STOCKS'];

export function FluctuationRadar({ canDispatchAgents }: FluctuationRadarProps) {
  const [domain, setDomain] = useState<'STOCKS' | 'CRYPTO'>('CRYPTO');
  const [analysis, setAnalysis] = useState<FluctuationAnalysis | null>(null);
  const [forecast, setForecast] = useState<FluctuationForecastResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setBusy(true);
      setError(null);
      try {
        const [a, f] = await Promise.all([
          certaintyApi.analyzeFluctuations(domain),
          certaintyApi.forecastFluctuations(domain, 30),
        ]);
        if (!cancelled) {
          setAnalysis(a);
          setForecast(f);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Fluctuation analysis failed');
      } finally {
        if (!cancelled) setBusy(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [domain]);

  return (
    <section
      className="border border-slate-800 bg-[#111827] rounded-xl p-5 space-y-4"
      aria-label="Fluctuation radar"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <h2 className="text-sm font-semibold text-slate-100">Fluctuation Radar</h2>
            <ProvenanceLabel provenance="SIMULATED" detail="Attribution · Timing · Advisory" />
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            What is moving the market, and when the next fluctuation is due. Attribution comes from
            recorded event history; timing comes from the rhythm between past occurrences of each
            cause.
          </p>
        </div>
        <div className="flex gap-1.5">
          {DOMAINS.map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDomain(d)}
              disabled={busy}
              className={`px-2.5 py-1 rounded-md text-[11px] font-medium border transition-colors ${
                domain === d
                  ? 'bg-slate-700 border-slate-600 text-slate-100'
                  : 'bg-transparent border-slate-800 text-slate-400 hover:text-slate-200'
              }`}
            >
              {d}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <div className="px-4 py-3 rounded-lg border border-rose-800/70 bg-rose-950/30 text-xs text-rose-200">{error}</div>
      ) : null}

      {busy && !analysis ? (
        <div className="p-6 text-center text-xs text-slate-400">Analyzing recorded history…</div>
      ) : null}

      {analysis ? (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
          {/* Drivers */}
          <div className="space-y-3">
            <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wide">
              What is moving {domain}
            </h3>
            {analysis.drivers.length === 0 ? (
              <div className="p-4 rounded-lg border border-dashed border-slate-800 text-xs text-slate-500">
                {analysis.reason ?? 'No recorded causes yet — record event reactions in the Pattern Intelligence panel.'}
              </div>
            ) : (
              <>
                {analysis.volatility_note ? (
                  <p className="text-xs text-slate-400">{analysis.volatility_note}</p>
                ) : null}
                <ul className="space-y-2">
                  {analysis.drivers.map((drv) => (
                    <li key={drv.event_type} className="space-y-1">
                      <div className="flex items-center justify-between gap-2 text-xs">
                        <span className="text-slate-200 font-mono">{drv.event_type}</span>
                        <span className="text-slate-400 font-mono shrink-0">
                          {drv.dominant_direction}{' '}
                          {drv.median_magnitude_pct !== null ? `${drv.median_magnitude_pct}%` : ''} ·{' '}
                          {(drv.impact_share * 100).toFixed(0)}% of impact
                        </span>
                      </div>
                      <div className="h-1.5 rounded-full bg-slate-900 overflow-hidden">
                        <div
                          className={`h-full rounded-full ${
                            drv.dominant_direction === 'DOWN' ? 'bg-rose-500' : 'bg-emerald-500'
                          }`}
                          style={{ width: `${Math.max(4, drv.impact_share * 100)}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>

          {/* Timing forecasts */}
          <div className="space-y-3">
            <h3 className="text-xs font-semibold text-slate-300 uppercase tracking-wide">
              Next fluctuation watch
            </h3>
            {forecast && forecast.forecasts.length > 0 ? (
              <ul className="space-y-2">
                {forecast.forecasts.slice(0, 4).map((f) => (
                  <li
                    key={f.event_type}
                    className="p-3 rounded-lg border border-slate-800 bg-[#0E1522] space-y-1.5"
                  >
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <span className="text-slate-200 font-mono">{f.event_type}</span>
                      {f.insufficient_history ? (
                        <span className="text-[11px] text-slate-500">needs more history</span>
                      ) : (
                        <span
                          className={`font-mono text-[11px] ${
                            f.expected_direction === 'DOWN' ? 'text-rose-300' : 'text-emerald-300'
                          }`}
                        >
                          {f.expected_direction}{' '}
                          {f.expected_magnitude_pct != null ? `${f.expected_magnitude_pct}%` : ''}
                        </span>
                      )}
                    </div>
                    {f.insufficient_history ? (
                      <p className="text-[11px] text-slate-500">{f.reason}</p>
                    ) : (
                      <>
                        <div className="text-[11px] text-slate-400 font-mono">
                          Expected around {String(f.next_expected_at).slice(0, 10)} ·{' '}
                          {f.days_until_expected != null && f.days_until_expected >= 0
                            ? `in ${Math.max(0, Math.round(f.days_until_expected))} days`
                            : f.days_until_expected != null
                              ? `${Math.abs(Math.round(f.days_until_expected))} days overdue`
                              : ''}
                        </div>
                        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-400">
                          <span>
                            P(within 30d){' '}
                            <span className="font-mono text-slate-200">
                              {f.probability_within_horizon != null
                                ? `${(f.probability_within_horizon * 100).toFixed(0)}%`
                                : '—'}
                            </span>
                          </span>
                          <span>
                            Rhythm{' '}
                            <span className="font-mono text-slate-200">
                              {f.median_interval_days != null ? `${f.median_interval_days}d` : '—'}
                            </span>
                          </span>
                          <span>
                            Confidence{' '}
                            <span className="font-mono text-slate-200">
                              {f.confidence != null ? `${(f.confidence * 100).toFixed(0)}%` : '—'}
                            </span>
                          </span>
                        </div>
                      </>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="p-4 rounded-lg border border-dashed border-slate-800 text-xs text-slate-500">
                {forecast?.reason ?? 'Not enough recorded history to forecast timing yet.'}
              </div>
            )}
            {!canDispatchAgents ? (
              <p className="text-[11px] text-slate-500">
                Recording event history requires OPERATOR or OWNER role.
              </p>
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}
