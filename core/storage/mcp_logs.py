"""Best-effort journal of MCP business request/response content."""
from __future__ import annotations

from core.runtime_logger import get_logger, redact
from core.storage.database import new_id, transaction


def record_exchange(
    request_json: str,
    response_json: str,
    request_received_at: int,
    response_returned_at: int,
    duration_ms: int,
) -> None:
    """Persist one business request and response without summarizing or truncating."""
    try:
        with transaction("mcp_logs") as connection:
            connection.execute(
                """INSERT INTO mcp_business_exchanges(
                  exchange_id,request_received_at,response_returned_at,duration_ms,
                  request_json,response_json
                ) VALUES (?,?,?,?,?,?)""",
                (
                    new_id("exchange"), request_received_at, response_returned_at,
                    duration_ms, redact(request_json), redact(response_json),
                ),
            )
    except Exception as exc:
        try:
            get_logger().error(f"记录 MCP 业务请求/响应失败: {type(exc).__name__}")
        except Exception:
            pass
