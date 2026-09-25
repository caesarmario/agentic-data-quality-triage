/** Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/ */
import { PageHeader } from "@/components/page-header";
import { ApprovalActions } from "@/components/approval-actions";
import { Badge, Card, Empty, Unavailable } from "@/components/ui/primitives";
import { controlPlane, query } from "@/lib/control-plane";
import { formatDate, formatDateTime, statusTone } from "@/lib/format";
import type { ApprovalList } from "@/lib/types";
export const dynamic = "force-dynamic";
export default async function ApprovalQueue() {
  const approvals = await controlPlane<ApprovalList>(
    query("/api/v1/approvals/requests", { limit: 50 }),
  );
  return (
    <>
      <PageHeader
        eyebrow="Approval Queue"
        title="Human decision boundary"
        detail="Approval changes persist durable authorization only. They do not execute remediation from this interface."
      />
      <div className="content-stack">
        {approvals.data ? (
          approvals.data.rows.length ? (
            approvals.data.rows.map((item) => (
              <Card key={item.request_id}>
                <div className="section-heading">
                  <div>
                    <div className="badge-row">
                      <Badge tone={statusTone(item.status)}>
                        {item.status}
                      </Badge>
                      <Badge tone={statusTone(item.risk_level)}>
                        {item.risk_level}
                      </Badge>
                      {item.dry_run && <Badge>dry run</Badge>}
                    </div>
                    <h2>{item.request_id}</h2>
                    <p>{item.reason}</p>
                  </div>
                  <span className="mono subtle">{item.target_dag_id}</span>
                </div>
                <div className="approval-grid">
                  <div>
                    <span>Scope</span>
                    <strong>
                      {formatDate(item.start_date)} to{" "}
                      {formatDate(item.end_date)}
                    </strong>
                  </div>
                  <div>
                    <span>Requested by</span>
                    <strong>{item.requested_by}</strong>
                  </div>
                  <div>
                    <span>Execution state</span>
                    <strong>{item.execution_status}</strong>
                  </div>
                  <div>
                    <span>Updated</span>
                    <strong>{formatDateTime(item.updated_at)}</strong>
                  </div>
                </div>
                {item.execution_error && (
                  <p className="error-text">{item.execution_error}</p>
                )}
                <ApprovalActions approval={item} />
              </Card>
            ))
          ) : (
            <Card>
              <Empty
                title="Approval queue is empty"
                detail="The API returned no durable approval requests."
              />
            </Card>
          )
        ) : (
          <Unavailable
            message={approvals.error ?? "Approval queue unavailable."}
          />
        )}
      </div>
    </>
  );
}
