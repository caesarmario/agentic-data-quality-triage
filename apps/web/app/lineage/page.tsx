/** Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/ */
import { PageHeader } from "@/components/page-header";
import {
  Badge,
  Card,
  Empty,
  Metric,
  Unavailable,
} from "@/components/ui/primitives";
import { controlPlane, query } from "@/lib/control-plane";
import type { BlastRadius } from "@/lib/types";
export const dynamic = "force-dynamic";
type Props = { searchParams: Promise<{ table?: string }> };
export default async function LineagePage({ searchParams }: Props) {
  const { table } = await searchParams;
  const result = table?.trim()
    ? await controlPlane<BlastRadius>(
        query("/api/v1/lineage/dbt/blast-radius", {
          table_name: table.trim(),
          max_depth: 5,
          max_nodes: 100,
        }),
      )
    : null;
  const nodes = result?.data
    ? [
        ...result.data.impacted_assets,
        ...result.data.impacted_tests,
        ...result.data.unresolved_nodes,
      ]
    : [];
  return (
    <>
      <PageHeader
        eyebrow="Lineage & Blast Radius"
        title="Bounded downstream impact"
        detail="Search a fully qualified table name against the configured dbt manifest. Results remain bounded by API policy."
      />
      <div className="content-stack">
        <Card>
          <form className="search-form" action="/lineage">
            <label htmlFor="table">Warehouse table</label>
            <div>
              <input
                id="table"
                name="table"
                defaultValue={table ?? ""}
                placeholder="for example dq.fct_orders_daily"
                maxLength={255}
                required
              />
              <button className="button primary" type="submit">
                Inspect blast radius
              </button>
            </div>
          </form>
        </Card>
        {!result && (
          <Card>
            <Empty
              title="Enter a table to inspect lineage"
              detail="No manifest query is issued until you provide a fully qualified warehouse table."
            />
          </Card>
        )}
        {result && !result.data && (
          <Unavailable
            message={result.error ?? "Lineage lookup unavailable."}
          />
        )}
        {result?.data && !result.data.matched && (
          <Card>
            <Empty
              title="No dbt node matched this table"
              detail={result.data.summary}
            />
          </Card>
        )}
        {result?.data && result.data.matched && (
          <>
            <div className="metric-grid">
              <Metric
                label="Impacted nodes"
                value={result.data.total_impacted_nodes}
                detail="Bounded downstream traversal"
              />
              <Metric
                label="Assets"
                value={result.data.impacted_asset_count}
                detail="Non-test downstream resources"
              />
              <Metric
                label="Tests"
                value={result.data.impacted_test_count}
                detail="Affected dbt tests"
              />
              <Metric
                label="Max depth reached"
                value={result.data.max_depth_reached}
                detail={
                  result.data.truncated
                    ? "Traversal truncated at API bound"
                    : "Traversal completed within bound"
                }
                tone={result.data.truncated ? "warning" : "neutral"}
              />
            </div>
            <Card>
              <div className="section-heading">
                <div>
                  <h2>Downstream graph inventory</h2>
                  <p>{result.data.summary}</p>
                </div>
                {result.data.truncated && (
                  <Badge tone="warning">truncated</Badge>
                )}
              </div>
              {nodes.length ? (
                <div className="lineage-list">
                  {nodes.map((node) => (
                    <article key={node.unique_id} className="lineage-node">
                      <span className="depth-marker">{node.depth ?? "?"}</span>
                      <div>
                        <div className="badge-row">
                          <Badge>{node.resource_type}</Badge>
                          {node.parent_unique_id && <Badge>has parent</Badge>}
                        </div>
                        <strong>
                          {node.relation_name || node.name || node.unique_id}
                        </strong>
                        <p>
                          {node.description ||
                            "No public dbt description recorded."}
                        </p>
                        <small className="mono">{node.unique_id}</small>
                      </div>
                    </article>
                  ))}
                </div>
              ) : (
                <Empty
                  title="No downstream nodes returned"
                  detail="The matched root has no returned downstream assets, tests, or unresolved manifest references."
                />
              )}
            </Card>
          </>
        )}
      </div>
    </>
  );
}
