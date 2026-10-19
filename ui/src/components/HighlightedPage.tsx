import { Fragment } from "react";

import type { PageView } from "@/lib/api";
import { type Span, segments } from "@/lib/highlight";

/** One page of the record, with every located citation marked. The text is rendered exactly as stored. */
export function HighlightedPage({ page, spans, active }: { page: PageView; spans: Span[]; active?: string | null }) {
  return (
    <section id={`page-${page.number}`} aria-label={`Page ${page.number}`} className="scroll-mt-4 rounded border border-slate-200 bg-white">
      <h3 className="border-b border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-600">
        Page {page.number}
      </h3>
      <pre data-testid={`page-text-${page.number}`} className="whitespace-pre-wrap break-words p-3 font-mono text-[13px] leading-relaxed text-slate-900">
        {segments(page.text, spans).map((seg) =>
          seg.ids.length === 0 ? (
            <Fragment key={seg.start}>{seg.text}</Fragment>
          ) : (
            <Fragment key={seg.start}>
              {seg.opens.map((id) => (
                // an empty anchor per citation, so that two citations starting at the same place both get a link
                <span key={id} id={`ev-${id}`} className="scroll-mt-24" />
              ))}
              <mark
                data-evidence={seg.ids.join(" ")}
                className={`rounded-sm px-0 ${
                  active && seg.ids.includes(active) ? "bg-yellow-300 ring-2 ring-yellow-500" : "bg-yellow-100"
                }`}
              >
                {seg.text}
              </mark>
            </Fragment>
          ),
        )}
      </pre>
    </section>
  );
}
