import type { ReactNode } from "react";

import { Navbar } from "@/components/Navbar";

export function Shell({ pathname, children }: { pathname: string; children: ReactNode }) {
  return (
    <div className="bg-glow flex min-h-screen flex-col">
      <Navbar pathname={pathname} />
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6">{children}</main>
      {/* Room for the fixed mobile tab bar; it is not in flow. */}
      <footer className="border-t border-border/60 py-6 pb-[calc(5rem+env(safe-area-inset-bottom))] md:pb-6">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-4 gap-y-1 px-4 text-xs text-muted-foreground sm:px-6">
          <span>Informational only. Not investment advice.</span>
          <span className="text-border">·</span>
          <span>Served locally on 127.0.0.1. Numbers are read from chain and from 1Tx.</span>
        </div>
      </footer>
    </div>
  );
}

export function Notice({ tone = "default", children }: { tone?: "default" | "destructive" | "warning"; children: ReactNode }) {
  const toneClass = {
    default: "border-border bg-card",
    destructive: "border-destructive/40 bg-destructive/10",
    warning: "border-warning/40 bg-warning/10",
  }[tone];
  return <div className={`rounded-xl border p-4 text-sm ${toneClass}`}>{children}</div>;
}
