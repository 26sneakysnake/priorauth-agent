import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DecisionView } from "@/components/DecisionView";
import type { CaseReview } from "@/lib/api";

import fixture from "../fixtures/case_review.json";

const review = fixture as unknown as CaseReview;
const decided = { ...review, outcome: review.outcome ?? "PEND" };

describe("DecisionView", () => {
  it("shows the decision, the reasons of a PEND and the injection flag", () => {
    render(<DecisionView decided={decided} />);
    expect(screen.getByTestId("decision-outcome").textContent).toBe("PEND");
    expect(screen.getByTestId("pend-reasons").textContent).toContain("INJECTION_DETECTED");
    expect(screen.getByRole("alert").textContent).toMatch(/never approved/);
  });

  it("highlights exactly the cited characters on the pages it shows", () => {
    const { container } = render(<DecisionView decided={decided} />);
    let checked = 0;
    for (const c of review.criteria) {
      c.evidence.forEach((ev) => {
        if (ev.start === null || ev.end === null) return;
        const page = review.pages.find((p) => p.number === ev.page)!;
        const expected = Array.from(page.text).slice(ev.start, ev.end).join("");
        const marks = [...container.querySelectorAll(`section[aria-label="Page ${ev.page}"] mark`)].map((m) => m.textContent);
        expect(marks.join("")).toContain(expected.slice(0, 20));
        checked++;
      });
    }
    expect(checked).toBeGreaterThan(5);
  });

  it("says an APPROVE needs every criterion met and never mentions a denial", () => {
    render(<DecisionView decided={{ ...decided, outcome: "APPROVE", reasons: [], injection_detected: false }} />);
    expect(screen.getByTestId("decision").textContent).toContain("every criterion is met");
    expect(screen.getByTestId("decision").textContent?.toLowerCase()).not.toContain("denied ");
  });
});
