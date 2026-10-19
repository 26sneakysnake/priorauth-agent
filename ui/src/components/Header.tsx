"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { clearToken } from "@/lib/auth";

export function Header() {
  const router = useRouter();
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
        <Link href="/queue" className="font-semibold">
          Prior-auth review
        </Link>
        <p className="text-xs text-slate-600">Synthetic records only. Not for clinical use.</p>
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
