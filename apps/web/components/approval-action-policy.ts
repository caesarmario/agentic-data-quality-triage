import type { Approval } from "@/lib/types";

export type ApprovalAction = "approve" | "reject" | "cancel";

// The API remains authoritative if dispatch claims a request after rendering.
export function availableApprovalActions(
  approval: Pick<Approval, "status" | "execution_status" | "execution_dag_run_id">,
): ApprovalAction[] {
  if (
    approval.execution_status !== "not_started" ||
    approval.execution_dag_run_id
  )
    return [];
  if (approval.status === "pending") return ["approve", "reject", "cancel"];
  if (approval.status === "approved") return ["cancel"];
  return [];
}
