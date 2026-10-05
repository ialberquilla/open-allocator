import { Activity, BookOpen, LayoutGrid, TrendingUp } from "lucide-react";

import logo from "@/assets/1tx-logo.svg";
import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/", label: "Overview", icon: LayoutGrid },
  { href: "/book", label: "Book", icon: BookOpen },
  { href: "/performance", label: "Performance", icon: TrendingUp },
  { href: "/activity", label: "Activity", icon: Activity },
];

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  if (href === "/activity") return pathname.startsWith("/activity") || pathname.startsWith("/approve");
  return pathname.startsWith(href);
}

export function Navbar({ pathname }: { pathname: string }) {
  return (
    <>
      <header className="sticky top-0 z-40 border-b border-border/60 bg-background/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-4 sm:px-6">
          <a href="/" className="flex shrink-0 items-center gap-2">
            <img src={logo} alt="1tx" width={50} height={20} />
            <span className="text-sm font-bold tracking-tight">Open Allocator</span>
          </a>

          {/* Below md the links live in the bottom bar instead. */}
          <nav className="hidden flex-1 items-center gap-1 md:flex">
            {LINKS.map(({ href, label, icon: Icon }) => (
              <a
                key={href}
                href={href}
                className={cn(
                  "flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm font-medium whitespace-nowrap transition-colors",
                  isActive(pathname, href) ? "bg-secondary text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon className="size-3.5" />
                {label}
              </a>
            ))}
          </nav>

          <span className="ml-auto shrink-0 rounded-md border border-border bg-secondary px-2 py-1 text-xs font-bold tracking-widest text-muted-foreground uppercase md:ml-0">
            Local
          </span>
        </div>
      </header>

      <nav
        aria-label="Primary"
        className="fixed inset-x-0 bottom-0 z-40 border-t border-border/60 bg-background/90 backdrop-blur md:hidden"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        <ul className="mx-auto grid max-w-lg grid-cols-4">
          {LINKS.map(({ href, label, icon: Icon }) => {
            const active = isActive(pathname, href);
            return (
              <li key={href}>
                <a
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex h-16 flex-col items-center justify-center gap-1 px-1 transition-colors",
                    active ? "text-primary" : "text-muted-foreground",
                  )}
                >
                  <span
                    className={cn(
                      "flex h-8 w-12 items-center justify-center rounded-full transition-colors",
                      active && "bg-primary/15",
                    )}
                  >
                    <Icon className="size-5" />
                  </span>
                  <span className="text-[11px] leading-none font-semibold">{label}</span>
                </a>
              </li>
            );
          })}
        </ul>
      </nav>
    </>
  );
}
