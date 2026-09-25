/**
 * Shared lifecycle links keep alert selection visible across non-open states.
 * Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
 */
const statuses = ["triaged", "open", "resolved"] as const;

export function AlertLifecycleLinks({
  page,
  selected,
}: {
  page: string;
  selected: string;
}) {
  return (
    <p className="muted">
      Show:{" "}
      {statuses.map((status, index) => (
        <span key={status}>
          {index > 0 && " | "}
          <a
            className={selected === status ? "table-link" : "text-link"}
            href={`${page}?status=${status}`}
          >
            {status}
          </a>
        </span>
      ))}
    </p>
  );
}
