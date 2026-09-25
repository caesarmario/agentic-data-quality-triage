import assert from "node:assert/strict";
import test from "node:test";
import { availableApprovalActions } from "../approval-action-policy";

test("pending pre-dispatch requests expose decisions and revocation", () => {
  assert.deepEqual(
    availableApprovalActions({ status: "pending", execution_status: "not_started", execution_dag_run_id: "" }),
    ["approve", "reject", "cancel"],
  );
});

test("approved pre-dispatch requests expose revocation only", () => {
  assert.deepEqual(
    availableApprovalActions({ status: "approved", execution_status: "not_started", execution_dag_run_id: "" }),
    ["cancel"],
  );
});

test("claimed, terminal, and unknown execution states expose no mutation", () => {
  for (const execution_status of ["dispatching", "dispatched", "succeeded", "failed", "unknown"]) {
    assert.deepEqual(
      availableApprovalActions({ status: "approved", execution_status, execution_dag_run_id: "" }),
      [],
    );
  }
  assert.deepEqual(
    availableApprovalActions({ status: "approved", execution_status: "not_started", execution_dag_run_id: "manual__dispatcher" }),
    [],
  );
  for (const status of ["cancelled", "rejected", "unknown"]) {
    assert.deepEqual(
      availableApprovalActions({ status, execution_status: "not_started", execution_dag_run_id: "" }),
      [],
    );
  }
});
