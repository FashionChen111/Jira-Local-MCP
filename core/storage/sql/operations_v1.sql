CREATE TABLE IF NOT EXISTS revisions (
    name TEXT PRIMARY KEY CHECK (name IN ('queue','execution','history')),
    value INTEGER NOT NULL CHECK (value >= 0)
);
INSERT OR IGNORE INTO revisions(name,value) VALUES ('queue',0),('execution',0),('history',0);

CREATE TABLE IF NOT EXISTS queue_items (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    issue_key TEXT,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high')),
    payload_json TEXT NOT NULL,
    description TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    layout_kind TEXT NOT NULL CHECK (layout_kind IN ('grouped','standalone')),
    category TEXT CHECK (category IN ('standard_field','custom_field','workflow','attachment','comment','worklog')),
    layout_scope TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 1),
    manual_after_operation_id TEXT,
    CHECK (
      (layout_kind='grouped' AND issue_key IS NOT NULL AND category IS NOT NULL AND layout_scope=issue_key
       AND (manual_after_operation_id IS NULL OR manual_after_operation_id='' OR manual_after_operation_id<>operation_id))
      OR (layout_kind='standalone' AND layout_scope='' AND manual_after_operation_id IS NULL)
    ),
    UNIQUE(layout_scope, position)
);

CREATE TABLE IF NOT EXISTS queue_events (
    event_id TEXT PRIMARY KEY,
    occurred_at INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('enqueue','dequeue','internal')),
    action_code TEXT NOT NULL CHECK (action_code IN (
      'mcp_append','submit_for_execution','cancelled_by_user','reorder',
      'requeued_not_applied','held_resumed','held_requeued','held_abandoned'
    )),
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_events_operation ON queue_events(operation_id, occurred_at);

CREATE TABLE IF NOT EXISTS execution_staging (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    issue_key TEXT,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high')),
    payload_json TEXT NOT NULL,
    description TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    stage_order INTEGER NOT NULL UNIQUE CHECK (stage_order >= 1),
    state TEXT NOT NULL CHECK (state IN ('staged','executing','indeterminate','held')),
    submission_risk_level TEXT NOT NULL CHECK (submission_risk_level IN ('low','medium','high')),
    execution_id TEXT UNIQUE,
    submitted_at INTEGER NOT NULL,
    started_at INTEGER,
    held_reason TEXT,
    blocked_by_execution_id TEXT,
    CHECK (
      (state IN ('staged','held') AND execution_id IS NULL AND started_at IS NULL)
      OR (state IN ('executing','indeterminate') AND execution_id IS NOT NULL AND started_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_staging_order ON execution_staging(stage_order);
CREATE INDEX IF NOT EXISTS idx_staging_state ON execution_staging(state);

CREATE TABLE IF NOT EXISTS execution_records (
    execution_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    operation_type TEXT NOT NULL,
    issue_key TEXT,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high')),
    submission_risk_level TEXT NOT NULL CHECK (submission_risk_level IN ('low','medium','high')),
    payload_json TEXT NOT NULL,
    description TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    outcome TEXT CHECK (outcome IN ('success','failed','indeterminate')),
    summary TEXT,
    error TEXT,
    new_issue_key TEXT,
    elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    resolution TEXT CHECK (resolution IN ('succeeded','not_applied','abandoned')),
    resolved_at INTEGER,
    UNIQUE(operation_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_execution_history ON execution_records(started_at DESC, execution_id DESC);
