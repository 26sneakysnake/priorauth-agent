"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { clearToken } from "@/lib/auth";

export const BANNER = "Demonstration on synthetic data. Every restart of the demo resets it; never enter real data.";

export function Header() {
  const router = useRouter();
  return (
    <header className="border-b border-slate-200 bg-white">
      <p role="note" data-testid="demo-banner" className="bg-amber-100 px-4 py-1.5 text-center text-sm font-semibold text-amber-950">
        {BANNER}
      </p>
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
        <nav className="flex items-center gap-4">
          <Link href="/try" className="font-semibold">
            Prior-auth review
          </Link>
          <Link href="/try" className="text-sm text-slate-700 underline underline-offset-2">
            Try it
          </Link>
          <Link href="/queue" className="text-sm text-slate-700 underline underline-offset-2">
            Review queue
          </Link>
        </nav>
        <p className="hidden text-xs text-slate-600 sm:block">Synthetic records only. Not for clinical use.</p>
        <button
          type="button"
          onClick={() => {
            clearToken();
            router.push("/login");
          }}
          className="text-sm text-slate-700 underline underline-offset-2"
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
