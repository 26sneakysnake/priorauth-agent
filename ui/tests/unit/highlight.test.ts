import { describe, expect, it } from "vitest";

import { type Span, segments, utf16Index } from "@/lib/highlight";

function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

const TEXT = "Plan of care: 2 visits per week.\n\tProgress: KOOS 40 → 61 (week 6) 👍 since 04/10/2025.";

describe("segments", () => {
  it("gives back the page text exactly, whatever the spans", () => {
    const random = rng(7);
    const length = Array.from(TEXT).length;
    for (let run = 0; run < 500; run++) {
      const spans: Span[] = Array.from({ length: Math.floor(random() * 6) }, (_, i) => {
        const a = Math.floor(random() * (length + 4)) - 2;
        const b = a + Math.floor(random() * 20) - 3;
        return { id: `s${i}`, start: a, end: b };
      });
      const parts = segments(TEXT, spans);
      expect(parts.map((p) => p.text).join("")).toBe(TEXT);
      for (const p of parts) expect(p.text.length).toBeGreaterThan(0);
    }
  });

  it("marks exactly the characters of each span, in code points", () => {
    const chars = Array.from(TEXT);
    const spans: Span[] = [
      { id: "a", start: 0, end: 12 },
      { id: "b", start: chars.indexOf("👍") - 2, end: chars.indexOf("👍") + 7 },
    ];
    const parts = segments(TEXT, spans);
    for (const span of spans) {
      const marked = parts.filter((p) => p.ids.includes(span.id)).map((p) => p.text).join("");
      expect(marked).toBe(chars.slice(span.start, span.end).join(""));
    }
    expect(parts.filter((p) => p.opens.includes("b"))).toHaveLength(1);
  });

  it("splits overlapping spans so that each character knows every span covering it", () => {
    const parts = segments("abcdefgh", [
      { id: "x", start: 1, end: 5 },
      { id: "y", start: 3, end: 7 },
    ]);
    expect(parts.map((p) => [p.text, p.ids.join(",")])).toEqual([
      ["a", ""],
      ["bc", "x"],
      ["de", "x,y"],
      ["fg", "y"],
      ["h", ""],
    ]);
  });

  it("ignores empty, inverted and out-of-range spans", () => {
    const parts = segments("abc", [
      { id: "empty", start: 1, end: 1 },
      { id: "inverted", start: 2, end: 1 },
      { id: "past", start: 2, end: 9 },
      { id: "negative", start: -1, end: 2 },
    ]);
    expect(parts).toEqual([{ text: "abc", start: 0, ids: [], opens: [] }]);
  });
});

describe("utf16Index", () => {
  it("maps code points to UTF-16 indices", () => {
    const index = utf16Index("a👍b");
    expect([0, 1, 2, 3].map(index)).toEqual([0, 1, 3, 4]);
    expect(index(4)).toBe(-1);
  });
});
