/**
 * Public API and persisted-report types used by the web control plane.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
export type Alert = {
  alert_id?: string | null;
  alert_key: string;
  alert_display_id: string;
  created_at?: string | null;
  updated_at?: string | null;
  status: string;
  alert_type: string;
  severity: string;
  table_name: string;
  metric: string;
  dt?: string | null;
  dimension: string;
  observed_value?: number | null;
  expected_value?: number | null;
  threshold_value?: number | null;
  report_s3_uri: string;
};

export type AlertList = { row_count: number; alerts: Alert[]; summary: string };
export type DailySummary = {
  dt: string;
  check_counts: { status: string; count: number }[];
  alert_counts: { severity: string; count: number }[];
  total_checks: number;
  total_open_alerts: number;
  duration_ms: number;
  summary: string;
};
export type AuditLog = {
  row_count: number;
  rows: {
    audit_id?: string;
    ts?: string;
    actor: string;
    action: string;
    tool_name: string;
    status: string;
    duration_ms?: number | null;
    error_message: string;
    llm_route?: {
      runtime_mode: string;
      used_heuristic: boolean;
      estimated_cost_display: string;
    } | null;
  }[];
  summary: string;
};
export type IncidentHistory = {
  row_count: number;
  rows: {
    memory_id: string;
    recorded_at: string;
    outcome_status: string;
    summary: string;
    confidence?: number | null;
    top_hypothesis_category: string;
    requires_human_approval: boolean;
    approval_state: string;
    report_id: string;
  }[];
  summary: string;
};
export type DqHistory = {
  row_count: number;
  rows: {
    check_run_id?: string;
    run_at?: string;
    check_name: string;
    check_type: string;
    status: string;
    severity: string;
    observed_value?: number | null;
    expected_value?: number | null;
    threshold_value?: number | null;
  }[];
  status_counts: Record<string, number>;
  summary: string;
};
export type PipelineRuns = {
  row_count: number;
  rows: {
    run_id: string;
    job_name: string;
    dag_id: string;
    task_id: string;
    status: string;
    started_at?: string;
    ended_at?: string;
    duration_ms?: number | null;
    error_message: string;
  }[];
  status_counts: Record<string, number>;
  summary: string;
};
export type TriageRun = {
  status: string;
  agent_run_id: string;
  alert_key: string;
  alert_display_id: string;
  severity: string;
  confidence: number;
  top_hypothesis?: string | null;
  markdown_report_s3_uri: string;
  json_report_s3_uri: string;
  approval_gated_actions: Record<string, unknown>[];
};
export type Approval = {
  request_id: string;
  created_at: string;
  updated_at: string;
  alert_key: string;
  agent_run_id?: string | null;
  action_type: string;
  risk_level: string;
  status: string;
  requested_by: string;
  reason: string;
  dispatcher_dag_id: string;
  target_dag_id: string;
  start_date?: string | null;
  end_date?: string | null;
  dry_run: boolean;
  decided_by: string;
  decision_comment: string;
  execution_dag_run_id: string;
  execution_status: string;
  execution_error: string;
};
export type ApprovalList = { row_count: number; rows: Approval[] };
export type BlastRadius = {
  table_name: string;
  matched: boolean;
  manifest_source: string;
  max_depth: number;
  max_nodes: number;
  max_depth_reached: number;
  truncated: boolean;
  total_impacted_nodes: number;
  impacted_asset_count: number;
  impacted_test_count: number;
  unresolved_node_count: number;
  resource_type_counts: Record<string, number>;
  impacted_assets: LineageNode[];
  impacted_tests: LineageNode[];
  unresolved_nodes: LineageNode[];
  summary: string;
};
export type LineageNode = {
  unique_id: string;
  resource_type: string;
  name?: string | null;
  relation_name?: string | null;
  description: string;
  depth?: number | null;
  parent_unique_id?: string | null;
};

export type ReportArtifact = {
  status: string;
  s3_uri: string;
  bucket: string;
  key: string;
  media_type: string;
  bytes_read: number;
  returned_bytes: number;
  max_bytes: number;
  truncated: boolean;
  text: string;
};

export type ReportHypothesis = {
  hypothesis_id: string;
  title: string;
  description: string;
  confidence: number | null;
  root_cause_category: string;
  recommended_action: string;
  framing_source: string;
};

export type ReportEvidence = {
  evidence_id: string;
  evidence_type: string;
  tool_name: string;
  summary: string;
  row_count: number | null;
};

export type PersistedTriageReport = {
  report_id: string;
  summary: string;
  impact: string;
  confidence: number | null;
  hypotheses: ReportHypothesis[];
  evidence: ReportEvidence[];
  llm_runtime: {
    route_event_count: number | null;
    requested_routes: string[];
    executed_routes: string[];
    providers: string[];
    models: string[];
    external_model_used: boolean | null;
    heuristic_fallback_used: boolean | null;
    input_tokens: number | null;
    output_tokens: number | null;
    estimated_cost_usd: number | null;
    duration_ms: number | null;
    fallback_reasons: string[];
  };
};

type JsonRecord = Record<string, unknown>;

/**
 * Resolve the JSON sibling emitted alongside a trusted alert's Markdown report.
 * Args: API-returned S3 artifact reference, never a caller-supplied storage URL.
 * Returns: Paired report.json URI or null for unsupported artifact shapes.
 * The API still enforces its storage allowlist and the parser checks alert identity.
 */
export function resolvePersistedJsonReportUri(reference: string): string | null {
  if (!/^s3:\/\/[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\/[A-Za-z0-9_/=.-]+\/report\.(md|json)$/.test(reference)) return null;
  if (reference.split("/").some((piece) => piece === "." || piece === "..")) return null;
  return reference.replace(/\/report\.md$/, "/report.json");
}

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function string(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

/**
 * Parse only allowlisted, operator-safe fields from a bounded JSON report.
 * The embedded report identity must match the selected API alert before any
 * report content is available to the page.
 */
export function parsePersistedTriageReport(
  text: string,
  expectedAlertKey: string,
): PersistedTriageReport | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }

  const report = record(parsed);
  const reportAlert = record(report?.alert);
  if (!report || string(reportAlert?.alert_key) !== expectedAlertKey)
    return null;

  const runtime = record(report.llm_runtime) ?? {};
  const hypotheses = Array.isArray(report.hypotheses) ? report.hypotheses : [];
  const evidence = Array.isArray(report.evidence) ? report.evidence : [];

  return {
    report_id: string(report.report_id),
    summary: string(report.summary),
    impact: string(report.impact),
    confidence: number(report.confidence),
    hypotheses: hypotheses.flatMap((item) => {
      const value = record(item);
      return value
        ? [
            {
              hypothesis_id: string(value.hypothesis_id),
              title: string(value.title),
              description: string(value.description),
              confidence: number(value.confidence),
              root_cause_category: string(value.root_cause_category),
              recommended_action: string(value.recommended_action),
              framing_source: string(value.framing_source),
            },
          ]
        : [];
    }),
    evidence: evidence.flatMap((item) => {
      const value = record(item);
      return value
        ? [
            {
              evidence_id: string(value.evidence_id),
              evidence_type: string(value.evidence_type),
              tool_name: string(value.tool_name),
              summary: string(value.summary),
              row_count: number(value.row_count),
            },
          ]
        : [];
    }),
    llm_runtime: {
      route_event_count: number(runtime.route_event_count),
      requested_routes: strings(runtime.requested_routes),
      executed_routes: strings(runtime.executed_routes),
      providers: strings(runtime.providers),
      models: strings(runtime.models),
      external_model_used: bool(runtime.external_model_used),
      heuristic_fallback_used: bool(runtime.heuristic_fallback_used),
      input_tokens: number(runtime.input_tokens),
      output_tokens: number(runtime.output_tokens),
      estimated_cost_usd: number(runtime.estimated_cost_usd),
      duration_ms: number(runtime.duration_ms),
      fallback_reasons: strings(runtime.fallback_reasons),
    },
  };
}
