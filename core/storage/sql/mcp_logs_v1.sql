CREATE TABLE IF NOT EXISTS mcp_business_exchanges (
    exchange_id TEXT PRIMARY KEY,
    request_received_at INTEGER NOT NULL CHECK (request_received_at >= 0),
    response_returned_at INTEGER NOT NULL CHECK (response_returned_at >= request_received_at),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_business_exchange_time
ON mcp_business_exchanges(request_received_at, exchange_id);
