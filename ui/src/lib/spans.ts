// Every located citation of a decision, grouped by page, with the ids the criterion cards link to.
import { evidenceId } from "@/components/CriterionCard";

import type { CriterionView, EvidenceView } from "./api";
import type { Span } from "./highlight";

export interface Cited {
  criteria: CriterionView[];
  injection_evidence: EvidenceView[];
}

export function pageSpans(decision: Cited): Map<number, Span[]> {
  const spans = new Map<number, Span[]>();
  const add = (page: number, span: Span) => spans.set(page, [...(spans.get(page) ?? []), span]);
  for (const c of decision.criteria) {
    c.evidence.forEach((ev, i) => {
      if (ev.start !== null && ev.end !== null) add(ev.page, { id: evidenceId(c.criterion_id, i), start: ev.start, end: ev.end });
    });
  }
  decision.injection_evidence.forEach((ev, i) => {
    if (ev.start !== null && ev.end !== null) add(ev.page, { id: `injection-${i}`, start: ev.start, end: ev.end });
  });
  return spans;
}
