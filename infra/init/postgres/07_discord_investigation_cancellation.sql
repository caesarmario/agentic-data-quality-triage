-- Preserve request history while distinguishing cancellation before any triage call.
BEGIN;
ALTER TABLE dq_control.discord_investigation_claims
    DROP CONSTRAINT IF EXISTS discord_investigation_claims_status_check;
ALTER TABLE dq_control.discord_investigation_claims
    ADD CONSTRAINT discord_investigation_claims_status_check
    CHECK (status IN ('claimed', 'complete', 'uncertain', 'cancelled_before_start'));
COMMIT;
