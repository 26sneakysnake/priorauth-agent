import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HighlightedPage } from "@/components/HighlightedPage";
import type { CaseReview } from "@/lib/api";
import { pageSpans } from "@/lib/spans";

import fixture from "../fixtures/case_review.json";

const review = fixture as unknown as CaseReview;

function markedText(container: HTMLElement, id: string): string {
  return [...container.querySelectorAll("mark")]
    .filter((m) => (m.dataset.evidence ?? "").split(" ").includes(id))
    .map((m) => m.textContent)
    .join("");
}

describe("HighlightedPage on a real API response (tests/fixtures/case_review.json)", () => {
  const spans = pageSpans(review);

  it("renders every page's text exactly as stored", () => {
    for (const page of review.pages) {
      const { getByTestId, unmount } = render(<HighlightedPage page={page} spans={spans.get(page.number) ?? []} />);
      expect(getByTestId(`page-text-${page.number}`).textContent).toBe(page.text);
      unmount();
    }
  });

  it("highlights exactly the characters [start, end) of every located citation", () => {
    let checked = 0;
    for (const page of review.pages) {
      const { container, unmount } = render(<HighlightedPage page={page} spans={spans.get(page.number) ?? []} />);
      for (const span of spans.get(page.number) ?? []) {
        expect(markedText(container, span.id)).toBe(Array.from(page.text).slice(span.start, span.end).join(""));
        expect(container.querySelector(`#ev-${span.id}`)).not.toBeNull();
        checked++;
      }
      unmount();
    }
    const located = review.criteria.flatMap((c) => c.evidence).filter((e) => e.start !== null);
    expect(checked).toBe(located.length + review.injection_evidence.filter((e) => e.start !== null).length);
    expect(checked).toBeGreaterThan(5);
  });

  it("puts the injection quote on its page too", () => {
    expect(review.injection_detected).toBe(true);
    const page = review.injection_evidence[0]!.page;
    expect((spans.get(page) ?? []).some((s) => s.id === "injection-0")).toBe(true);
  });
});
