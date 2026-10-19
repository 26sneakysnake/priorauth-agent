import type { CriterionView } from "@/lib/api";

import { StatusBadge } from "./StatusBadge";

export function evidenceId(criterionId: string, index: number): string {
  return `${criterionId.replace(/[^A-Za-z0-9_-]/g, "_")}-${index}`;
}

export function CriterionCard({ criterion, onSelect }: { criterion: CriterionView; onSelect: (id: string) => void }) {
  return (
    <article aria-label={criterion.title} className="rounded border border-slate-200 bg-white p-3">
      <header className="flex items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold text-slate-900">{criterion.title}</h3>
          <p className="font-mono text-xs text-slate-500">{criterion.criterion_id}</p>
        </div>
        <StatusBadge status={criterion.status} />
      </header>
      <p className="mt-2 text-sm text-slate-700">{criterion.requirement}</p>
      <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
        <div>
          <dt className="inline">Agent claimed: </dt>
          <dd className="inline font-medium">{criterion.claimed_status ?? "nothing"}</dd>
        </div>
        <div>
          <dt className="inline">Evidence verified: </dt>
          <dd className="inline font-medium">{criterion.verified ? "yes" : "no"}</dd>
        </div>
        {criterion.conflict ? <dd className="font-medium text-amber-800">Record contradicts itself</dd> : null}
      </dl>
      {criterion.note ? <p className="mt-2 text-sm italic text-slate-600">“{criterion.note}”</p> : null}
      <ul className="mt-2 space-y-1">
        {criterion.evidence.map((ev, i) => {
          const id = evidenceId(criterion.criterion_id, i);
          const located = ev.start !== null && ev.end !== null;
          return (
            <li key={id} className="text-sm">
              {located ? (
                <a
                  href={`#ev-${id}`}
                  onClick={() => onSelect(id)}
                  className="mr-2 font-medium text-blue-700 underline underline-offset-2"
                >
                  page {ev.page}
                </a>
              ) : (
                <span className="mr-2 font-medium text-slate-500">page {ev.page}</span>
              )}
              <q className="text-slate-800">{ev.quote}</q>
              {!ev.valid ? <span className="ml-2 text-xs font-semibold text-red-700">not verified</span> : null}
              {ev.valid && !located ? <span className="ml-2 text-xs text-slate-500">not located</span> : null}
            </li>
          );
        })}
        {criterion.evidence.length === 0 ? <li className="text-sm text-slate-500">No citation.</li> : null}
      </ul>
    </article>
  );
}
