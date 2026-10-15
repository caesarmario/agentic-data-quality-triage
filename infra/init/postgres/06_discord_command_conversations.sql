BEGIN;
ALTER TABLE dq_control.discord_copilot_requests
    DROP CONSTRAINT IF EXISTS discord_copilot_requests_source_kind_check;
ALTER TABLE dq_control.discord_copilot_requests
    ADD CONSTRAINT discord_copilot_requests_source_kind_check
    CHECK (source_kind IN ('message', 'modal', 'command'));
COMMIT;
