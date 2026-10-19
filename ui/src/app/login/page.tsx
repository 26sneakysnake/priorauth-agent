"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { type DemoInfo, api, errorText } from "@/lib/api";
import { setToken } from "@/lib/auth";

function LoginForm() {
  const router = useRouter();
  const next = useSearchParams().get("next");
  const [username, setUsername] = useState("demo-reviewer");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [demo, setDemo] = useState<DemoInfo | null>(null);

  useEffect(() => {
    void api.GET("/demo/info").then(({ data }) => setDemo(data ?? null));
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    const { data, error: failure } = await api.POST("/auth/token", { body: { username, password } });
    if (!data) {
      setError(errorText(failure));
      return;
    }
    if (data.role === "system") {
      setError("This account submits cases; it cannot review them.");
      return;
    }
    setToken(data.access_token);
    router.push(next && next.startsWith("/") && !next.startsWith("//") ? next : "/try");
  }

  return (
    <form onSubmit={submit} aria-label="Sign in" className="mx-auto mt-12 max-w-sm space-y-4 rounded border border-slate-200 bg-white p-6">
      <h1 className="text-lg font-semibold">Reviewer sign-in</h1>
      {demo ? (
        <div data-testid="demo-account" className="rounded bg-indigo-50 p-3 text-sm text-indigo-950">
          <p className="font-semibold">Public demo account (reviewer role, synthetic data only)</p>
          <p>
            Username: <code>{demo.reviewer_username}</code>
            <br />
            Password: <code>{demo.reviewer_password}</code>
          </p>
          <button
            type="button"
            onClick={() => {
              setUsername(demo.reviewer_username);
              setPassword(demo.reviewer_password);
            }}
            className="mt-2 rounded bg-indigo-700 px-2 py-1 text-xs font-semibold text-white"
          >
            Use the demo account
          </button>
        </div>
      ) : null}
      <label className="block text-sm">
        Username
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          className="mt-1 block w-full rounded border border-slate-300 p-2"
        />
      </label>
      <label className="block text-sm">
        Password
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          className="mt-1 block w-full rounded border border-slate-300 p-2"
        />
      </label>
      <button type="submit" className="w-full rounded bg-slate-900 py-2 text-sm font-semibold text-white">
        Sign in
      </button>
      {error ? (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      ) : null}
    </form>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
