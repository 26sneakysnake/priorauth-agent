"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { CriterionCard } from "@/components/CriterionCard";
import { HighlightedPage } from "@/components/HighlightedPage";
import { ReviewActions } from "@/components/ReviewActions";
import { type CaseReview, type ReviewIn, api, errorText } from "@/lib/api";
import { pageSpans } from "@/lib/spans";

export default function CasePage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [review, setReview] = useState<CaseReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState<string | null>(null);

  const load = useCallback(async () => {
    const { data, error: failure, response } = await api.GET("/cases/{case_id}/review", {
      params: { path: { case_id: id } },
    });
    if (response.status === 401) router.replace(`/login?next=/cases/${id}`);
    else if (data) setReview(data);
    else setError(errorText(failure));
  }, [id, router]);

  useEffect(() => {
    void load();
  }, [load]);

  async function submit(body: ReviewIn): Promise<string | null> {
    const { error: failure, response } = await api.POST("/cases/{case_id}/reviews", {
      params: { path: { case_id: id } },
      body,
    });
    if (!response.ok) return errorText(failure);
    await load();
    return null;
  }

  if (error) return <p role="alert" className="text-red-700">{error}</p>;
  if (!review) return <p className="text-slate-600">Loading…</p>;
  const spans = pageSpans(review);
  const approved = review.reviews.some((r) => r.action === "approve");
  const disabled =
    review.outcome !== "PEND"
      ? "The engine approved this case: there is nothing to review."
      : approved
        ? "A reviewer approved this case."
        : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold">
          {review.submitter_case_id ?? review.case_id} · {review.procedure_name}
        </h1>
        <Link href="/queue" className="text-sm text-blue-700 underline underline-offset-2">
          Back to the queue
        </Link>
      </div>
      <p className="text-sm text-slate-600">
        Engine decision: <strong>{review.outcome ?? "pending"}</strong>. Evidence extracted by{" "}
        <span className="font-mono">{review.extracted_by ?? "unknown"}</span>
        {review.error ? <span> (error: {review.error})</span> : null}.
      </p>
      {review.injection_detected ? (
        <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-800">
          The record contains text that tries to instruct an automated reviewer.
        </p>
      ) : null}
      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-4" aria-label="Record pages">
          {review.pages.map((page) => (
            <HighlightedPage key={page.number} page={page} spans={spans.get(page.number) ?? []} active={active} />
          ))}
        </div>
        <div className="space-y-4">
          <section aria-label="Reasons" className="rounded border border-slate-200 bg-white p-3 text-sm">
            <h2 className="mb-1 font-semibold">Why it is pended</h2>
            <ul className="list-disc pl-5">
              {review.reasons.map((r, i) => (
                <li key={i}>
                  <strong>{r.code}</strong> {r.criterion_id ? <span className="font-mono text-xs">{r.criterion_id}</span> : null}
                  {r.detail ? <span className="text-slate-600"> — {r.detail}</span> : null}
                </li>
              ))}
              {review.reasons.length === 0 ? <li>None.</li> : null}
            </ul>
          </section>
          <section aria-label="Criteria" className="space-y-3">
            {review.criteria.map((c) => (
              <CriterionCard key={c.criterion_id} criterion={c} onSelect={setActive} />
            ))}
          </section>
          <ReviewActions criteria={review.criteria} disabled={disabled} onSubmit={submit} />
          <section aria-label="Review history" className="rounded border border-slate-200 bg-white p-3 text-sm">
            <h2 className="mb-1 font-semibold">Reviews</h2>
            <ul className="space-y-1">
              {review.reviews.map((r) => (
                <li key={r.id} data-testid="review-entry">
                  <strong>{r.action}</strong>
                  {r.criterion_id ? ` · ${r.criterion_id} → ${r.corrected_status}` : ""}
                  {r.comment ? <span className="text-slate-600"> — {r.comment}</span> : null}
                </li>
              ))}
              {review.reviews.length === 0 ? <li className="text-slate-500">No review yet.</li> : null}
            </ul>
          </section>
        </div>
      </div>
    </div>
  );
}
