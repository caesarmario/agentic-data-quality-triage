import React from "react";
import { Empty } from "@/components/ui/primitives";
import type { DailySummary } from "@/lib/types";

/** Empty checks must not hide an available summary containing open alerts. */
export function DailySummaryGate({ summary, children }: {
  summary: DailySummary;
  children: React.ReactNode;
}) {
  if (summary.availability === "empty") {
    return <Empty title="No quality data yet"
      detail="No DQ result exists for this date. This is not proof of healthy data; check Incident Center for active incidents." />;
  }
  return <>
    {summary.total_checks === 0 && <Empty title="Checks have not run"
      detail="Open alerts still require attention. Daily data quality has not been verified." />}
    {children}
  </>;
}
