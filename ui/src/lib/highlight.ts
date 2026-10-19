// Splits a page's text into plain and highlighted segments. The API gives each citation as [start, end) offsets in
// Python code points (priorauth.agent.verifier.locate); JavaScript strings index UTF-16 code units, so offsets are
// converted first. Concatenating the segments gives back the page text exactly, character for character.

export interface Span {
  id: string;
  start: number; // code points
  end: number;
}

export interface Segment {
  text: string;
  start: number; // UTF-16 index in the page text
  ids: string[]; // the spans covering this segment, in input order
  opens: string[]; // the spans that start at this segment
}

/** Maps a code-point offset to a UTF-16 index; returns -1 past the end of the text. */
export function utf16Index(text: string): (codePoint: number) => number {
  const starts: number[] = [];
  let i = 0;
  for (const ch of text) {
    starts.push(i);
    i += ch.length;
  }
  starts.push(i);
  return (codePoint) => (Number.isInteger(codePoint) && codePoint >= 0 && codePoint < starts.length ? starts[codePoint]! : -1);
}

export function segments(text: string, spans: readonly Span[]): Segment[] {
  const toUtf16 = utf16Index(text);
  const valid = spans
    .map((s) => ({ id: s.id, start: toUtf16(s.start), end: toUtf16(s.end) }))
    .filter((s) => s.start >= 0 && s.end > s.start);
  const cuts = new Set<number>([0, text.length]);
  for (const s of valid) {
    cuts.add(s.start);
    cuts.add(s.end);
  }
  const points = [...cuts].sort((a, b) => a - b);
  const out: Segment[] = [];
  for (let k = 0; k + 1 < points.length; k++) {
    const a = points[k]!;
    const b = points[k + 1]!;
    out.push({
      text: text.slice(a, b),
      start: a,
      ids: valid.filter((s) => s.start <= a && b <= s.end).map((s) => s.id),
      opens: valid.filter((s) => s.start === a).map((s) => s.id),
    });
  }
  return out;
}
