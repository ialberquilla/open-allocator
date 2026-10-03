import type { ReactNode } from "react";

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen">
      <nav className="border-b">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <a href="/" className="text-sm font-bold tracking-tight">
            Open Allocator
          </a>
          <span className="text-xs text-muted-foreground">local · 127.0.0.1</span>
        </div>
      </nav>
      <main className="mx-auto max-w-5xl px-4 py-8">{children}</main>
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
