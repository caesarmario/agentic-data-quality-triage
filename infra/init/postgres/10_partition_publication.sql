-- Partition publication control state. Applied by the main migration owner only.
CREATE SCHEMA IF NOT EXISTS dq_control;

CREATE TABLE IF NOT EXISTS dq_control.partition_revisions (
    dataset text NOT NULL,
    partition_dt date NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    watermark text NOT NULL DEFAULT '',
    PRIMARY KEY (dataset, partition_dt)
);

CREATE TABLE IF NOT EXISTS dq_control.partition_publications (
    publication_id uuid PRIMARY KEY,
    execution_id text NOT NULL,
    dataset text NOT NULL,
    partition_dt date NOT NULL,
    target_table text NOT NULL,
    stage_table text NOT NULL,
    backup_table text NOT NULL,
    expected_revision bigint NOT NULL,
    expected_watermark text NOT NULL,
    before_fingerprint text NOT NULL,
    staged_fingerprint text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'prepared', 'publishing', 'published', 'unknown',
        'compensating', 'compensated', 'aborted'
    )),
    intent text NOT NULL CHECK (intent IN ('daily', 'recovery', 'simulation_empty')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (execution_id, target_table, partition_dt),
    FOREIGN KEY (dataset, partition_dt)
        REFERENCES dq_control.partition_revisions (dataset, partition_dt)
);

ALTER TABLE dq_control.partition_publications
    DROP CONSTRAINT IF EXISTS partition_publications_status_check;
ALTER TABLE dq_control.partition_publications
    ADD CONSTRAINT partition_publications_status_check CHECK (status IN
        ('prepared','publishing','published','unknown','compensating','compensated','aborted'));

CREATE INDEX IF NOT EXISTS partition_publications_unresolved
    ON dq_control.partition_publications (dataset, partition_dt)
    WHERE status IN ('prepared', 'publishing', 'unknown', 'compensating');
