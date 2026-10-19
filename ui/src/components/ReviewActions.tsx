"use client";

import { useState } from "react";

import { type CriterionView, type ReviewIn, STATUSES, type Status } from "@/lib/api";
import { type Action, reviewBody } from "@/lib/review";

const LABEL: Record<Action, string> = {
  approve: "Approve",
  request_info: "Request more information",
  flag_error: "Flag an agent error",
};

export function ReviewActions({
  criteria,
  disabled,
  onSubmit,
}: {
  criteria: CriterionView[];
  disabled?: string | null;
  onSubmit: (body: ReviewIn) => Promise<string | null>;
}) {
  const [action, setAction] = useState<Action>("approve");
  const [reason, setReason] = useState("");
  const [criterionId, setCriterionId] = useState("");
  const [correctedStatus, setCorrectedStatus] = useState<Status | "">("");
  const [comment, setComment] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (disabled) return <p className="rounded bg-slate-100 p-3 text-sm text-slate-700">{disabled}</p>;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const result = reviewBody({ action, reason, criterionId, correctedStatus, comment });
    if ("error" in result) {
      setMessage(result.error);
      return;
    }
    setBusy(true);
    const error = await onSubmit(result.body);
    setBusy(false);
    setMessage(error ?? `${LABEL[action]}: recorded.`);
    if (!error) {
      setReason("");
      setComment("");
      setCriterionId("");
      setCorrectedStatus("");
    }
  }

  return (
    <form onSubmit={submit} aria-label="Review" className="space-y-3 rounded border border-slate-200 bg-white p-3">
      <fieldset className="flex flex-wrap gap-3">
        <legend className="mb-1 text-sm font-semibold">Action</legend>
        {(Object.keys(LABEL) as Action[]).map((a) => (
          <label key={a} className="flex items-center gap-1 text-sm">
            <input type="radio" name="action" value={a} checked={action === a} onChange={() => setAction(a)} />
            {LABEL[a]}
          </label>
        ))}
      </fieldset>
      {action === "request_info" ? (
        <label className="block text-sm">
          What is missing
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={2000}
            rows={3}
            className="mt-1 block w-full rounded border border-slate-300 p-2"
          />
        </label>
      ) : null}
      {action === "flag_error" ? (
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="block text-sm">
            Criterion
            <select
              value={criterionId}
              onChange={(e) => setCriterionId(e.target.value)}
              className="mt-1 block w-full rounded border border-slate-300 p-1"
            >
              <option value="">Choose…</option>
              {criteria.map((c) => (
                <option key={c.criterion_id} value={c.criterion_id}>
                  {c.title}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-sm">
            Correct status
            <select
              value={correctedStatus}
              onChange={(e) => setCorrectedStatus(e.target.value as Status | "")}
              className="mt-1 block w-full rounded border border-slate-300 p-1"
            >
              <option value="">Choose…</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        </div>
      ) : null}
      {action !== "request_info" ? (
        <label className="block text-sm">
          Comment{action === "approve" ? " (optional)" : ""}
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            maxLength={2000}
            rows={3}
            className="mt-1 block w-full rounded border border-slate-300 p-2"
          />
        </label>
      ) : null}
      <button
        type="submit"
        disabled={busy}
        className="rounded bg-slate-900 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
      >
        {LABEL[action]}
      </button>
      {message ? (
        <p role="status" className="text-sm text-slate-700">
          {message}
        </p>
      ) : null}
    </form>
  );
}
