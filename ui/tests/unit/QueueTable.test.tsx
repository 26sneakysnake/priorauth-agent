import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { QueueTable } from "@/components/QueueTable";
import type { QueueItem } from "@/lib/api";

const item: QueueItem = {
  case_id: "4f0c2d7e-0000-4000-8000-000000000001",
  submitter_case_id: "dev-042",
  procedure_code: "MRI_LUMBAR",
  created_at: "2026-10-19T09:00:00",
  finished_at: "2026-10-19T09:00:01",
  age_seconds: 7300,
  reasons: [{ code: "CONFLICT", criterion_id: "MRI_LUMBAR.red_flags", detail: "" }],
  review_status: "request_info",
};

describe("QueueTable", () => {
  it("shows reasons, age and review status, and links to the case", () => {
    render(<QueueTable items={[item]} />);
    const link = screen.getByRole("link", { name: "dev-042" });
    expect(link.getAttribute("href")).toBe(`/cases/${item.case_id}`);
    expect(screen.getByText("CONFLICT")).toBeDefined();
    expect(screen.getByText("MRI_LUMBAR.red_flags")).toBeDefined();
    expect(screen.getByText("2 h")).toBeDefined();
    expect(screen.getByText("info requested")).toBeDefined();
  });

  it("says when the queue is empty", () => {
    render(<QueueTable items={[]} />);
    expect(screen.getByText("No pended case.")).toBeDefined();
  });
});
