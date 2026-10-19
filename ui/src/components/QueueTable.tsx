import Link from "next/link";

import type { QueueItem } from "@/lib/api";
import { formatAge, shortId } from "@/lib/format";

const STATUS_LABEL: Record<string, string> = { request_info: "info requested", flag_error: "error flagged" };

export function QueueTable({ items }: { items: QueueItem[] }) {
  if (items.length === 0) return <p className="text-slate-600">No pended case.</p>;
  return (
    <table className="w-full border-collapse text-left text-sm">
      <thead>
        <tr className="border-b border-slate-300 text-xs uppercase tracking-wide text-slate-500">
          <th className="py-2 pr-3">Case</th>
          <th className="py-2 pr-3">Procedure</th>
          <th className="py-2 pr-3">Age</th>
          <th className="py-2 pr-3">Why it is pended</th>
          <th className="py-2">Review</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.case_id} data-testid="queue-row" className="border-b border-slate-200 align-top">
            <td className="py-2 pr-3">
              <Link href={`/cases/${item.case_id}`} className="font-mono text-blue-700 underline underline-offset-2">
                {item.submitter_case_id ?? shortId(item.case_id)}
              </Link>
            </td>
            <td className="py-2 pr-3 font-mono text-xs">{item.procedure_code}</td>
            <td className="py-2 pr-3 whitespace-nowrap">{formatAge(item.age_seconds)}</td>
            <td className="py-2 pr-3">
              <ul className="space-y-0.5">
                {item.reasons.map((r, i) => (
                  <li key={i}>
                    <span className="font-semibold">{r.code}</span>
                    {r.criterion_id ? <span className="ml-1 font-mono text-xs text-slate-600">{r.criterion_id}</span> : null}
                  </li>
                ))}
              </ul>
            </td>
            <td className="py-2 text-slate-700">{item.review_status ? STATUS_LABEL[item.review_status] : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
