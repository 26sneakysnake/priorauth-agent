"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { type Decided, DecisionView } from "@/components/DecisionView";
import { type DemoInfo, type ExampleView, api, errorText } from "@/lib/api";
import { getToken } from "@/lib/auth";

type RunState = { status: "running" } | { status: "done"; caseId: string; decided: Decided } | { status: "error"; message: string };

const PROCEDURES = ["MRI_LUMBAR", "PT_EXTENSION", "BIOLOGIC_RA"];

async function waitForDecision(caseId: string): Promise<Decided> {
  for (let i = 0; i < 300; i++) {
    const { data } = await api.GET("/cases/{case_id}", { params: { path: { case_id: caseId } } });
    if (data && (data.job.status === "done" || data.job.status === "failed")) break;
    await new Promise((r) => setTimeout(r, 200));
  }
  const { data, error } = await api.GET("/cases/{case_id}/review", { params: { path: { case_id: caseId } } });
  if (!data) throw new Error(errorText(error));
  return { ...data, outcome: data.outcome ?? "PEND" };
}

function LiveMode({ info }: { info: DemoInfo }) {
  const [procedure, setProcedure] = useState(PROCEDURES[0]!);
  const [text, setText] = useState("");
  const [state, setState] = useState<RunState | null>(null);
  if (!info.live_enabled) {
    return (
      <p className="text-sm text-slate-600">
        &ldquo;Your own record&rdquo; mode (a live model call) is turned off on this demo; the examples above use
        recorded model responses.
      </p>
    );
  }
  async function run() {
    setState({ status: "running" });
    const { data, error } = await api.POST("/demo/live", { body: { procedure_code: procedure, record_text: text } });
    setState(data ? { status: "done", caseId: "", decided: data } : { status: "error", message: errorText(error) });
  }
  return (
    <div className="space-y-2">
      <p role="alert" className="rounded bg-red-50 p-2 text-sm font-semibold text-red-800">
        Synthetic data only: never type real patient data. Nothing you type is kept after this page.
      </p>
      <label className="block text-sm">
        Procedure{" "}
        <select value={procedure} onChange={(e) => setProcedure(e.target.value)} className="rounded border border-slate-300 p-1">
          {PROCEDURES.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
      </label>
      <textarea
        aria-label="Synthetic record"
        value={text}
        onChange={(e) => setText(e.target.value)}
        maxLength={info.live_max_chars}
        rows={8}
        className="block w-full rounded border border-slate-300 p-2 font-mono text-sm"
        placeholder="Paste a synthetic record (pages separated by form feeds)"
      />
      <button
        type="button"
        disabled={!text.trim() || state?.status === "running"}
        onClick={() => void run()}
        className="rounded bg-slate-900 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
      >
        Run the real model
      </button>
      {state?.status === "running" ? <p className="text-sm">Running…</p> : null}
      {state?.status === "error" ? (
        <p role="alert" className="text-sm text-red-700">
          {state.message} No decision was made; the examples above still work.
        </p>
      ) : null}
      {state?.status === "done" ? <DecisionView decided={state.decided} /> : null}
    </div>
  );
}

export default function TryPage() {
  const router = useRouter();
  const [info, setInfo] = useState<DemoInfo | null>(null);
  const [examples, setExamples] = useState<ExampleView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<Record<string, RunState>>({});

  useEffect(() => {
    void api.GET("/demo/info").then(({ data }) => setInfo(data ?? null));
    void api.GET("/demo/examples").then(({ data, error: failure }) => {
      if (data) setExamples(data);
      else setError(errorText(failure));
    });
  }, []);

  async function run(example: ExampleView) {
    if (!getToken()) {
      router.push("/login?next=/try");
      return;
    }
    setRuns((r) => ({ ...r, [example.id]: { status: "running" } }));
    const { data, error: failure, response } = await api.POST("/demo/examples/{example_id}/run", {
      params: { path: { example_id: example.id } },
    });
    if (response.status === 401) {
      router.push("/login?next=/try");
      return;
    }
    try {
      if (!data) throw new Error(errorText(failure));
      const decided = await waitForDecision(data.case_id);
      setRuns((r) => ({ ...r, [example.id]: { status: "done", caseId: data.case_id, decided } }));
    } catch (e) {
      setRuns((r) => ({ ...r, [example.id]: { status: "error", message: (e as Error).message } }));
    }
  }

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold">Try the agent on synthetic prior-authorization records</h1>
        <p className="text-sm text-slate-700">
          Each example is a synthetic faxed record. &ldquo;Run the agent&rdquo; submits it to the API: the agent cites
          evidence, a deterministic engine decides APPROVE or PEND (it never denies). Decisions come from real
          claude-haiku-5-5 responses recorded once and replayed{info ? ` (${info.extracted_by})` : ""}.
        </p>
      </div>
      {error ? <p role="alert" className="text-red-700">{error}</p> : null}
      {examples === null && !error ? <p className="text-slate-600">Loading…</p> : null}
      <ul className="space-y-4">
        {(examples ?? []).map((ex) => {
          const state = runs[ex.id];
          return (
            <li key={ex.id} data-testid={`example-${ex.id}`} className="space-y-3 rounded border border-slate-200 bg-white p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <span className="rounded bg-indigo-100 px-2 py-0.5 text-xs font-semibold text-indigo-900">{ex.label}</span>
                  <h2 className="mt-1 font-semibold">{ex.title}</h2>
                  <p className="text-sm text-slate-700">{ex.look_for}</p>
                  <p className="text-xs text-slate-500">
                    {ex.procedure_name} · expected decision (gold label): {ex.expected_decision}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void run(ex)}
                  disabled={state?.status === "running"}
                  className="rounded bg-slate-900 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
                >
                  {state?.status === "running" ? "Running…" : "Run the agent"}
                </button>
              </div>
              {state?.status === "error" ? <p role="alert" className="text-sm text-red-700">{state.message}</p> : null}
              {state?.status === "done" ? (
                <>
                  <DecisionView decided={state.decided} />
                  <Link href={`/cases/${state.caseId}`} className="text-sm text-blue-700 underline underline-offset-2">
                    Open it as a reviewer (approve, request information, flag an error)
                  </Link>
                </>
              ) : null}
            </li>
          );
        })}
      </ul>
      {info ? (
        <section className="space-y-2 rounded border border-slate-200 bg-white p-4">
          <h2 className="font-semibold">Your own synthetic record</h2>
          <LiveMode info={info} />
        </section>
      ) : null}
    </div>
  );
}
