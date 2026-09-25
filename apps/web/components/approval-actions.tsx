"use client";
/**
 * Approval actions require an explicit operator identity and invoke only the
 * server-side proxy; no token is ever rendered or stored in this component.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Ban, Check, X } from "lucide-react";
import type { Approval } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { availableApprovalActions } from "@/components/approval-action-policy";
export function ApprovalActions({ approval }: { approval: Approval }) {
  const router = useRouter();
  const [actor, setActor] = useState("");
  const [comment, setComment] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [isError, setIsError] = useState(false);
  const [busy, setBusy] = useState(false);
  const actions = availableApprovalActions(approval);
  if (!actions.length) return null;
  async function act(action: "approve" | "reject" | "cancel") {
    if (!actor.trim()) {
      setIsError(true);
      setMessage(
        "Operator identity is required before changing durable approval state.",
      );
      return;
    }
    setBusy(true);
    setIsError(false);
    setMessage(null);
    const suffix = action === "cancel" ? "cancel" : "decision";
    const body =
      action === "cancel"
        ? { cancelled_by: actor, comment }
        : { decision: action, decided_by: actor, comment };
    try {
      const response = await fetch(
        `/api/control-plane/api/v1/approvals/requests/${encodeURIComponent(approval.request_id)}/${suffix}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const payload = (await response.json()) as {
        detail?: string;
        status?: string;
      };
      if (!response.ok)
        throw new Error(payload.detail ?? "Approval update failed.");
      setMessage(`Saved durable state: ${payload.status ?? "updated"}.`);
      router.refresh();
    } catch (cause) {
      setIsError(true);
      setMessage(
        cause instanceof Error ? cause.message : "Approval update failed.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="approval-actions">
      <label>
        Operator identity
        <input
          value={actor}
          onChange={(event) => setActor(event.target.value)}
          maxLength={200}
          placeholder="Required"
        />
      </label>
      <label>
        Comment
        <input
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          maxLength={2000}
          placeholder="Optional rationale"
        />
      </label>
      <div className="button-row">
        {actions.includes("approve") && (
          <>
            <Button
              variant="default"
              className="button success"
              disabled={busy}
              onClick={() => act("approve")}
            >
              <Check data-icon="inline-start" size={15} />
              Approve
            </Button>
            <Button
              variant="destructive"
              className="button danger"
              disabled={busy}
              onClick={() => act("reject")}
            >
              <X data-icon="inline-start" size={15} />
              Reject
            </Button>
          </>
        )}
        {actions.includes("cancel") && (
          <Button
            variant="outline"
            className="button secondary"
            disabled={busy}
            onClick={() => act("cancel")}
          >
            <Ban data-icon="inline-start" size={15} />
            Cancel
          </Button>
        )}
      </div>
      {message && (
        <p role={isError ? "alert" : "status"} className={isError ? "error-text" : "muted"}>
          {message}
        </p>
      )}
    </div>
  );
}
