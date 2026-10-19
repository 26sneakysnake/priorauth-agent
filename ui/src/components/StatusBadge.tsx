import type { Status } from "@/lib/api";

const STYLE: Record<Status, string> = {
  MET: "bg-emerald-100 text-emerald-900 ring-emerald-300",
  NOT_MET: "bg-amber-100 text-amber-900 ring-amber-300",
  INSUFFICIENT: "bg-slate-100 text-slate-800 ring-slate-300",
};

export function StatusBadge({ status }: { status: Status }) {
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-semibold ring-1 ${STYLE[status]}`}>{status}</span>
  );
}
