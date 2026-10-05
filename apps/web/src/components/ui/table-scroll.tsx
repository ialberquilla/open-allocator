import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/** Horizontal scroller for the wide tables.
 *
 *  Every table here carries a min-width, so on a phone it clips. `overflow-x-auto`
 *  alone already scrolls — but with overlay scrollbars the affordance only appears
 *  once you are scrolling, so a clipped column reads as a broken layout rather than
 *  as "swipe me". The edge fades say there is more before the first swipe, and the
 *  thin always-on scrollbar (see .table-scroll in index.css) says how much. */
export function TableScroll({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ start: false, end: false });

  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const max = el.scrollWidth - el.clientWidth;
    setEdges({
      start: el.scrollLeft > 1,
      end: max > 1 && el.scrollLeft < max - 1,
    });
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    measure();
    // The table's own width changes with the data and with the viewport, and
    // neither fires a scroll event — observe both the box and its content.
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    for (const child of Array.from(el.children)) observer.observe(child);
    return () => observer.disconnect();
  }, [measure]);

  return (
    <div className="relative">
      <div
        ref={ref}
        onScroll={measure}
        className={cn("table-scroll overflow-x-auto", className)}
      >
        {children}
      </div>
      <div
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-card to-transparent transition-opacity duration-200",
          edges.start ? "opacity-100" : "opacity-0",
        )}
      />
      <div
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-y-0 right-0 w-10 bg-gradient-to-l from-card to-transparent transition-opacity duration-200",
          edges.end ? "opacity-100" : "opacity-0",
        )}
      />
    </div>
  );
}
