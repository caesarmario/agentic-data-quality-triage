"use client";
/**
 * Explicit triage trigger. It never auto-runs and renders only the API result.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Play, ShieldCheck } from "lucide-react";
import type { TriageRun } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge, Unavailable } from "@/components/ui/primitives";
import { statusTone } from "@/lib/format";
export function TriageRunner({ alertKey }: { alertKey: string }) {
  const router = useRouter();
  const [result, setResult] = useState<TriageRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const response = await fetch("/api/control-plane/api/v1/triage/run", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ alert_key: alertKey }),
      });
      const payload = (await response.json()) as
        | TriageRun
        | { detail?: string };
      if (!response.ok)
        throw new Error(
          "detail" in payload ? payload.detail : "Triage request failed.",
        );
      setResult(payload as TriageRun);
      router.refresh();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Triage request failed.",
      );
    } finally {
      setRunning(false);
    }
  }
  return (
    <div className="runner">
      <Button
        className="button primary"
        onClick={run}
        disabled={running}
      >
        <Play data-icon="inline-start" size={16} aria-hidden="true" />
        {running ? "Running bounded triage..." : "Run triage"}
      </Button>
      <p className="muted">
        <ShieldCheck size={15} aria-hidden="true" /> Runs the existing workflow
        for this alert. It does not approve or execute remediation.
      </p>
      {error && <Unavailable message={error} />}
      {result && (
        <div className="result-panel" role="status" aria-live="polite">
          <div>
            <Badge tone={statusTone(result.severity)}>{result.severity}</Badge>
            <Badge>{`${Math.round(result.confidence * 100)}% confidence`}</Badge>
          </div>
          <strong>
            {result.top_hypothesis || "No top hypothesis returned"}
          </strong>
          <p>
            Run <span className="mono">{result.agent_run_id}</span> returned{" "}
            {result.approval_gated_actions.length} approval-gated action(s).
          </p>
          <p className="muted">
            Report artifacts remain in approved storage; this UI does not expose
            their raw contents automatically.
          </p>
        </div>
      )}
    </div>
  );
}
