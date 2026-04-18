-- Test History tables (ARCH-004)
-- Stores a row per pytest run + a row per individual test result.
-- Apply:
--   docker exec -i ict-bot-postgres-1 psql -U ict_bot -d ict_bot < db/test_history_schema.sql

CREATE TABLE IF NOT EXISTS test_runs (
    id              SERIAL PRIMARY KEY,
    git_branch      VARCHAR(80),
    git_sha         VARCHAR(40),
    suite           VARCHAR(40) NOT NULL DEFAULT 'unit',

    -- Counts
    total           INT NOT NULL DEFAULT 0,
    passed          INT NOT NULL DEFAULT 0,
    failed          INT NOT NULL DEFAULT 0,
    skipped         INT NOT NULL DEFAULT 0,
    errors          INT NOT NULL DEFAULT 0,

    -- Timing
    duration_sec    NUMERIC(10,3) NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,

    -- Metadata
    triggered_by    VARCHAR(30) NOT NULL DEFAULT 'manual',
    python_version  VARCHAR(20),
    platform        VARCHAR(40),
    exit_status     VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|passed|failed|error
    summary         TEXT
);

CREATE INDEX IF NOT EXISTS idx_test_runs_branch    ON test_runs(git_branch);
CREATE INDEX IF NOT EXISTS idx_test_runs_sha       ON test_runs(git_sha);
CREATE INDEX IF NOT EXISTS idx_test_runs_suite     ON test_runs(suite);
CREATE INDEX IF NOT EXISTS idx_test_runs_started   ON test_runs(started_at DESC);


CREATE TABLE IF NOT EXISTS test_results (
    id              SERIAL PRIMARY KEY,
    run_id          INT NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
    nodeid          TEXT NOT NULL,
    module          VARCHAR(200),
    test_class      VARCHAR(100),
    test_name       VARCHAR(200),
    outcome         VARCHAR(10) NOT NULL,  -- passed|failed|skipped|error
    duration_sec    NUMERIC(10,4),
    error_message   TEXT,
    traceback       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_test_results_run     ON test_results(run_id);
CREATE INDEX IF NOT EXISTS idx_test_results_module  ON test_results(module);
CREATE INDEX IF NOT EXISTS idx_test_results_outcome ON test_results(outcome);
