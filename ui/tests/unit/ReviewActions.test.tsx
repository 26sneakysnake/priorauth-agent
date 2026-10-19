import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ReviewActions } from "@/components/ReviewActions";
import type { CaseReview } from "@/lib/api";

import fixture from "../fixtures/case_review.json";

const criteria = (fixture as unknown as CaseReview).criteria;

describe("ReviewActions", () => {
  it("does not send a request for information without a reason", async () => {
    const onSubmit = vi.fn(async () => null);
    render(<ReviewActions criteria={criteria} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByLabelText("Request more information"));
    await userEvent.click(screen.getByRole("button", { name: "Request more information" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toMatch(/missing/);
    await userEvent.type(screen.getByLabelText("What is missing"), "Latest therapy note");
    await userEvent.click(screen.getByRole("button", { name: "Request more information" }));
    expect(onSubmit).toHaveBeenCalledWith({ action: "request_info", reason: "Latest therapy note" });
  });

  it("flags an error on one criterion with its corrected status", async () => {
    const onSubmit = vi.fn(async () => null);
    render(<ReviewActions criteria={criteria} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByLabelText("Flag an agent error"));
    const target = criteria[1]!;
    await userEvent.selectOptions(screen.getByLabelText("Criterion"), target.criterion_id);
    await userEvent.selectOptions(screen.getByLabelText("Correct status"), "NOT_MET");
    await userEvent.type(screen.getByLabelText("Comment"), "The quote is about the other knee");
    await userEvent.click(screen.getByRole("button", { name: "Flag an agent error" }));
    expect(onSubmit).toHaveBeenCalledWith({
      action: "flag_error",
      criterion_id: target.criterion_id,
      corrected_status: "NOT_MET",
      comment: "The quote is about the other knee",
    });
    expect(screen.getByRole("status").textContent).toMatch(/recorded/);
  });

  it("shows the API's error instead of a confirmation", async () => {
    render(<ReviewActions criteria={criteria} onSubmit={async () => "only a finished, pended case can be reviewed"} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByRole("status").textContent).toBe("only a finished, pended case can be reviewed");
  });

  it("explains why a case cannot be reviewed", () => {
    render(<ReviewActions criteria={criteria} onSubmit={async () => null} disabled="A reviewer approved this case." />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("A reviewer approved this case.")).toBeDefined();
  });
});
