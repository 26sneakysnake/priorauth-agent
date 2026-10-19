"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { api, errorText } from "@/lib/api";
import { setToken } from "@/lib/auth";

function LoginForm() {
  const router = useRouter();
  const next = useSearchParams().get("next");
  const [username, setUsername] = useState("demo-reviewer");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

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
    router.push(next && next.startsWith("/") && !next.startsWith("//") ? next : "/queue");
  }

  return (
    <form onSubmit={submit} aria-label="Sign in" className="mx-auto mt-12 max-w-sm space-y-4 rounded border border-slate-200 bg-white p-6">
      <h1 className="text-lg font-semibold">Reviewer sign-in</h1>
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
      <p className="text-xs text-slate-500">Demo account: demo-reviewer (password set when the demo API starts).</p>
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
