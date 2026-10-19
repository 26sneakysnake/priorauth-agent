"use client";

import { useState } from "react";

import type { CriterionView, EvidenceView, PageView, ReasonView } from "@/lib/api";
import { pageSpans } from "@/lib/spans";

import { CriterionCard } from "./CriterionCard";
import { HighlightedPage } from "./HighlightedPage";

export interface Decided {
  outcome: string;
  reasons: ReasonView[];
  criteria: CriterionView[];
  pages: PageView[];
  injection_detected: boolean;
  injection_evidence: EvidenceView[];
}

/** A decision with its reasons, its criteria and, page by page, the cited text highlighted. */
export function DecisionView({ decided }: { decided: Decided }) {
  const [active, setActive] = useState<string | null>(null);
  const spans = pageSpans(decided);
  const cited = decided.pages.filter((p) => (spans.get(p.number) ?? []).length > 0);
  const others = decided.pages.filter((p) => (spans.get(p.number) ?? []).length === 0);
  const approve = decided.outcome === "APPROVE";
  return (
    <div className="space-y-3" data-testid="decision">
      <p className="text-sm">
        Decision:{" "}
        <span
          data-testid="decision-outcome"
          className={`rounded px-2 py-0.5 font-semibold ${approve ? "bg-emerald-100 text-emerald-900" : "bg-amber-100 text-amber-900"}`}
        >
          {decided.outcome}
        </span>{" "}
        {approve ? "every criterion is met with a verified citation" : "sent to a human reviewer (never denied)"}
      </p>
      {decided.injection_detected ? (
        <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-800">
          The record contains text that tries to instruct an automated reviewer: flagged, never approved.
        </p>
      ) : null}
      {decided.reasons.length > 0 ? (
        <section aria-label="Why it is pended" className="rounded border border-amber-200 bg-amber-50 p-3 text-sm">
          <h3 className="mb-1 font-semibold">Why it is pended</h3>
          <ul className="list-disc pl-5" data-testid="pend-reasons">
            {decided.reasons.map((r, i) => (
              <li key={i}>
                <strong>{r.code}</strong> {r.criterion_id ? <span className="font-mono text-xs">{r.criterion_id}</span> : null}
                {r.detail ? <span className="text-slate-700"> — {r.detail}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-3" aria-label="Cited pages">
          {cited.map((page) => (
            <HighlightedPage key={page.number} page={page} spans={spans.get(page.number) ?? []} active={active} />
          ))}
          {others.length > 0 ? (
            <details className="rounded border border-slate-200 bg-white p-2 text-sm">
              <summary className="cursor-pointer text-slate-700">{others.length} page(s) without a citation</summary>
              <div className="mt-2 space-y-3">
                {others.map((page) => (
                  <HighlightedPage key={page.number} page={page} spans={[]} />
                ))}
              </div>
            </details>
          ) : null}
        </div>
        <div className="space-y-3" aria-label="Criteria">
          {decided.criteria.map((c) => (
            <CriterionCard key={c.criterion_id} criterion={c} onSelect={setActive} />
          ))}
        </div>
      </div>
    </div>
  );
}
