/** Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/ */
import { AlertTriangle, CheckCircle2, CircleHelp, Clock3 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge as ShadcnBadge } from "./badge";
import { Card as ShadcnCard } from "./card";

// Compatibility wrappers preserve the existing pages' custom token classes during migration.
export function Card({
  className,
  ...props
}: React.ComponentProps<typeof ShadcnCard>) {
  return <ShadcnCard className={cn("card", className)} {...props} />;
}

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: React.ReactNode;
  tone?: "danger" | "warning" | "success" | "neutral";
  className?: string;
}) {
  const variant =
    tone === "danger"
      ? "destructive"
      : tone === "warning"
        ? "secondary"
        : tone === "success"
          ? "default"
          : "outline";

  return (
    <ShadcnBadge
      variant={variant}
      className={cn("badge", `badge-${tone}`, className)}
    >
      {children}
    </ShadcnBadge>
  );
}
export function Metric({
  label,
  value,
  detail,
  tone = "neutral",
}: {
  label: string;
  value: string | number;
  detail: string;
  tone?: "danger" | "warning" | "success" | "neutral";
}) {
  const Icon =
    tone === "danger"
      ? AlertTriangle
      : tone === "success"
        ? CheckCircle2
        : tone === "warning"
          ? Clock3
          : CircleHelp;
  return (
    <Card className="metric">
      <div className="metric-heading">
        <span>{label}</span>
        <Icon aria-hidden="true" size={17} />
      </div>
      <strong>{value}</strong>
      <p>{detail}</p>
    </Card>
  );
}
export function Unavailable({ message }: { message: string }) {
  return (
    <div className="notice notice-unavailable">
      <AlertTriangle size={18} aria-hidden="true" />
      <div>
        <strong>Data unavailable</strong>
        <p>{message}</p>
      </div>
    </div>
  );
}
export function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <CircleHelp size={22} aria-hidden="true" />
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}
