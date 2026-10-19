import { describe, expect, it } from "vitest";

import type { QueueItem } from "@/lib/api";
import { isExpired } from "@/lib/auth";
import { formatAge } from "@/lib/format";
import { filterQueue, procedures, reviewBody } from "@/lib/review";

const empty = { reason: "", criterionId: "", correctedStatus: "" as const, comment: "" };

describe("reviewBody", () => {
  it("approves with an optional comment", () => {
    expect(reviewBody({ ...empty, action: "approve" })).toEqual({ body: { action: "approve", comment: null } });
  });

  it("needs a reason to request information", () => {
    expect(reviewBody({ ...empty, action: "request_info", reason: "  " })).toHaveProperty("error");
    expect(reviewBody({ ...empty, action: "request_info", reason: " lab report " })).toEqual({
      body: { action: "request_info", reason: "lab report" },
    });
  });

  it("needs a criterion, a corrected status and a comment to flag an error", () => {
    const base = { ...empty, action: "flag_error" as const };
    expect(reviewBody(base)).toHaveProperty("error");
    expect(reviewBody({ ...base, criterionId: "A.b" })).toHaveProperty("error");
    expect(reviewBody({ ...base, criterionId: "A.b", correctedStatus: "MET" })).toHaveProperty("error");
    expect(reviewBody({ ...base, criterionId: "A.b", correctedStatus: "MET", comment: "wrong page" })).toEqual({
      body: { action: "flag_error", criterion_id: "A.b", corrected_status: "MET", comment: "wrong page" },
    });
  });
});

describe("queue helpers", () => {
  const item = (procedure_code: string): QueueItem => ({
    case_id: procedure_code,
    submitter_case_id: null,
    procedure_code,
    created_at: "2026-01-01T00:00:00",
    finished_at: null,
    age_seconds: 0,
    reasons: [],
    review_status: null,
  });
  const items = [item("MRI_LUMBAR"), item("PT_EXTENSION"), item("MRI_LUMBAR")];

  it("filters by procedure", () => {
    expect(filterQueue(items, "MRI_LUMBAR")).toHaveLength(2);
    expect(filterQueue(items, "")).toHaveLength(3);
    expect(procedures(items)).toEqual(["MRI_LUMBAR", "PT_EXTENSION"]);
  });

  it("formats ages", () => {
    expect([30, 90, 7200, 200000].map(formatAge)).toEqual(["< 1 min", "1 min", "2 h", "2 d"]);
  });
});

describe("isExpired", () => {
  const token = (exp: number) => `x.${btoa(JSON.stringify({ exp })).replace(/=+$/, "")}.y`;
  it("reads the exp claim", () => {
    expect(isExpired(token(2000), 1000)).toBe(false);
    expect(isExpired(token(1000), 2000)).toBe(true);
    expect(isExpired("garbage")).toBe(true);
  });
});
