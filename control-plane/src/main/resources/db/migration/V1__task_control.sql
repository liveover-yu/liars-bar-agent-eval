CREATE TABLE scheduler_lock (id INTEGER PRIMARY KEY);
INSERT INTO scheduler_lock (id) VALUES (1);

CREATE TABLE eval_run (
    run_id VARCHAR(36) PRIMARY KEY,
    idempotency_key VARCHAR(200) NOT NULL UNIQUE,
    config_hash VARCHAR(64) NOT NULL,
    config_json TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    suite_version VARCHAR(32) NOT NULL,
    total_cases INTEGER NOT NULL,
    call_count INTEGER NOT NULL DEFAULT 0,
    lease_token VARCHAR(36),
    worker_id VARCHAR(128),
    lease_until BIGINT,
    last_error TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX idx_eval_run_claim ON eval_run (status, lease_until, created_at);

CREATE TABLE eval_case (
    run_id VARCHAR(36) NOT NULL REFERENCES eval_run(run_id),
    case_id VARCHAR(100) NOT NULL,
    probe_id VARCHAR(100) NOT NULL,
    repeat_index INTEGER NOT NULL,
    case_seed BIGINT NOT NULL,
    ordinal INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    finished_attempt_id VARCHAR(36),
    PRIMARY KEY (run_id, case_id)
);

CREATE TABLE eval_attempt (
    attempt_id VARCHAR(36) PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL,
    case_id VARCHAR(100) NOT NULL,
    lease_token VARCHAR(36) NOT NULL,
    attempt_number INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'STARTED',
    outcome VARCHAR(16),
    error_type VARCHAR(32),
    latency_ms BIGINT NOT NULL DEFAULT 0,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    was_challenged BOOLEAN,
    reference_decision BOOLEAN,
    finish_hash VARCHAR(64),
    finish_json TEXT,
    created_at BIGINT NOT NULL,
    finished_at BIGINT,
    FOREIGN KEY (run_id, case_id) REFERENCES eval_case(run_id, case_id),
    UNIQUE (run_id, case_id, attempt_number)
);
CREATE INDEX idx_attempt_run ON eval_attempt (run_id, status);
