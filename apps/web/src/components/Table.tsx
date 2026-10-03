import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** A plain data table that scrolls sideways on a narrow screen. */
export function Table({ head, children, minWidth = 640 }: { head: ReactNode[]; children: ReactNode; minWidth?: number }) {
  return (
    <div className="table-scroll -mx-5 overflow-x-auto px-5">
      <table className="w-full border-collapse text-sm" style={{ minWidth }}>
        <thead>
          <tr className="border-b text-left text-[11px] tracking-wider text-muted-foreground uppercase">
            {head.map((cell, index) => (
              <th key={index} className="py-2 pr-4 font-semibold last:pr-0">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Cell({ children, className, numeric }: { children: ReactNode; className?: string; numeric?: boolean }) {
  return (
    <td className={cn("border-b border-border/60 py-2.5 pr-4 align-top last:pr-0", numeric && "tabular-nums", className)}>
      {children}
    </td>
  );
}
