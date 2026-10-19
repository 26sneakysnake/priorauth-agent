// Every located citation of a case, grouped by page, with the ids the criterion cards link to.
import { evidenceId } from "@/components/CriterionCard";

import type { CaseReview } from "./api";
import type { Span } from "./highlight";

export function pageSpans(review: CaseReview): Map<number, Span[]> {
  const spans = new Map<number, Span[]>();
  const add = (page: number, span: Span) => spans.set(page, [...(spans.get(page) ?? []), span]);
  for (const c of review.criteria) {
    c.evidence.forEach((ev, i) => {
      if (ev.start !== null && ev.end !== null) add(ev.page, { id: evidenceId(c.criterion_id, i), start: ev.start, end: ev.end });
    });
  }
  review.injection_evidence.forEach((ev, i) => {
    if (ev.start !== null && ev.end !== null) add(ev.page, { id: `injection-${i}`, start: ev.start, end: ev.end });
  });
  return spans;
}
