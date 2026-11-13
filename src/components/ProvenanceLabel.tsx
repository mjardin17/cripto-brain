import type { DataProvenance } from '../types/certainty';
import { describeProvenance } from '../utils/formatters';

interface ProvenanceLabelProps {
  provenance: DataProvenance;
  detail?: string;
}

export function ProvenanceLabel({ provenance, detail }: ProvenanceLabelProps) {
  const { label, toneClass } = describeProvenance(provenance);
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-mono tracking-tight">
      <span className={`font-semibold ${toneClass}`}>{label}</span>
      {detail ? (
        <>
          <span className="text-slate-600" aria-hidden="true">
            ·
          </span>
          <span className="text-slate-400 font-sans">{detail}</span>
        </>
      ) : null}
    </span>
  );
}
