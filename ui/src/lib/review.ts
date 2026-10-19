// What each review action needs before it can be sent; the API checks the same rules (priorauth.api.review.ReviewIn).
import type { QueueItem, ReviewIn, Status } from "./api";

export type Action = ReviewIn["action"];

export interface ReviewForm {
  action: Action;
  reason: string;
  criterionId: string;
  correctedStatus: Status | "";
  comment: string;
}

export function reviewBody(form: ReviewForm): { body: ReviewIn } | { error: string } {
  switch (form.action) {
    case "approve":
      return { body: { action: "approve", comment: form.comment.trim() || null } };
    case "request_info":
      if (!form.reason.trim()) return { error: "Say what information is missing." };
      return { body: { action: "request_info", reason: form.reason.trim() } };
    case "flag_error":
      if (!form.criterionId) return { error: "Choose the criterion the agent got wrong." };
      if (!form.correctedStatus) return { error: "Choose the correct status." };
      if (!form.comment.trim()) return { error: "Explain the error in a comment." };
      return {
        body: {
          action: "flag_error",
          criterion_id: form.criterionId,
          corrected_status: form.correctedStatus,
          comment: form.comment.trim(),
        },
      };
  }
}

export function filterQueue(items: readonly QueueItem[], procedure: string): QueueItem[] {
  return procedure ? items.filter((i) => i.procedure_code === procedure) : [...items];
}

export function procedures(items: readonly QueueItem[]): string[] {
  return [...new Set(items.map((i) => i.procedure_code))].sort();
}
