"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { QueueTable } from "@/components/QueueTable";
import { type QueueItem, api, errorText } from "@/lib/api";
import { filterQueue, procedures } from "@/lib/review";

export default function QueuePage() {
  const router = useRouter();
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [procedure, setProcedure] = useState("");

  useEffect(() => {
    let live = true;
    api.GET("/review/queue").then(({ data, error: failure, response }) => {
      if (!live) return;
      if (response.status === 401) router.replace("/login?next=/queue");
      else if (data) setItems(data);
      else setError(errorText(failure));
    });
    return () => {
      live = false;
    };
  }, [router]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Pended cases</h1>
        <label className="text-sm">
          Procedure{" "}
          <select
            value={procedure}
            onChange={(e) => setProcedure(e.target.value)}
            className="ml-1 rounded border border-slate-300 p-1"
          >
            <option value="">All</option>
            {procedures(items ?? []).map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error ? <p role="alert" className="text-red-700">{error}</p> : null}
      {items === null && !error ? <p className="text-slate-600">Loading…</p> : null}
      {items ? <QueueTable items={filterQueue(items, procedure)} /> : null}
    </div>
  );
}
