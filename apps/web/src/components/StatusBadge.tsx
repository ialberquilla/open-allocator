import type { PlanStatus } from "@/lib/api";
import { Badge, type BadgeProps } from "@/components/ui/badge";

const STATUS: Record<PlanStatus, { text: string; variant: BadgeProps["variant"] }> = {
  pending: { text: "Awaiting approval", variant: "warning" },
  expired: { text: "Expired", variant: "ghost" },
  applying: { text: "Applying", variant: "secondary" },
  applied: { text: "Applied", variant: "success" },
  failed: { text: "Failed", variant: "destructive" },
  rejected: { text: "Rejected", variant: "ghost" },
};

export function StatusBadge({ status }: { status: PlanStatus }) {
  const { text, variant } = STATUS[status];
  return <Badge variant={variant}>{text}</Badge>;
}
