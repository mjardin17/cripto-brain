import type { HistoricalEdgePoint } from '../types/certainty';
import { formatPct, formatTimestampShort } from '../utils/formatters';

interface HistoricalEdgeChartProps {
  series: HistoricalEdgePoint[];
}

export function HistoricalEdgeChart({ series }: HistoricalEdgeChartProps) {
  if (!series || series.length === 0) {
    return (
      <div className="h-44 flex items-center justify-center text-xs text-slate-500 border border-dashed border-slate-800 rounded-lg">
        No DuckDB scan history recorded yet. Run a Polymarket scan to log historical net edge series.
      </div>
    );
  }

  const values = series.map((s) => s.best_net_edge_pct ?? 0);
  const minVal = Math.min(-3, ...values, 1.5) - 0.5;
  const maxVal = Math.max(2.5, ...values, 1.5) + 0.5;
  const span = Math.max(1, maxVal - minVal);

  const width = 560;
  const height = 160;
  const padX = 28;
  const padY = 18;
  const innerW = width - padX * 2;
  const innerH = height - padY * 2;

  const yFor = (v: number) => padY + ((maxVal - v) / span) * innerH;
  const xFor = (i: number) =>
    series.length === 1 ? padX + innerW / 2 : padX + (i / (series.length - 1)) * innerW;

  const hurdleY = yFor(1.5);
  const zeroY = yFor(0);

  const polylinePoints = series
    .map((pt, idx) => `${xFor(idx).toFixed(1)},${yFor(pt.best_net_edge_pct ?? 0).toFixed(1)}`)
    .join(' ');

  const latest = series[series.length - 1];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
        <span>Best Observed Net Edge per DuckDB Scan vs. 1.50% Risk Gate Hurdle</span>
        <span className="font-mono tabular-nums text-slate-300">
          Latest: {formatPct(latest.best_net_edge_pct, 3, true)} ({latest.markets_evaluated} mkts)
        </span>
      </div>

      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full h-44 bg-[#0B111B] rounded-lg border border-slate-800/80"
        role="img"
        aria-label="Historical DuckDB best net arbitrage edge chart"
      >
        {/* 0% reference */}
        <line
          x1={padX}
          y1={zeroY}
          x2={width - padX}
          y2={zeroY}
          stroke="#334155"
          strokeWidth="1"
          strokeDasharray="3 3"
        />
        <text x={padX} y={zeroY - 4} fill="#64748b" fontSize="9" fontFamily="JetBrains Mono">
          0.0%
        </text>

        {/* +1.5% Risk Gate threshold */}
        <line
          x1={padX}
          y1={hurdleY}
          x2={width - padX}
          y2={hurdleY}
          stroke="#16a34a"
          strokeWidth="1"
          strokeDasharray="4 3"
        />
        <text x={width - padX - 88} y={hurdleY - 4} fill="#4ade80" fontSize="9" fontFamily="JetBrains Mono">
          +1.50% Gate Hurdle
        </text>

        {series.length > 1 ? (
          <polyline
            fill="none"
            stroke="#38bdf8"
            strokeWidth="2"
            points={polylinePoints}
          />
        ) : null}

        {series.map((pt, idx) => {
          const val = pt.best_net_edge_pct ?? 0;
          const cx = xFor(idx);
          const cy = yFor(val);
          const approved = val >= 1.5;
          return (
            <circle
              key={pt.scan_id}
              cx={cx}
              cy={cy}
              r="3.5"
              fill={approved ? '#22c55e' : '#38bdf8'}
              stroke="#0b111b"
              strokeWidth="1.5"
            >
              <title>{`${formatTimestampShort(pt.scan_time)} · Best: ${formatPct(val, 3, true)}`}</title>
            </circle>
          );
        })}
      </svg>
    </div>
  );
}
