"""Localhost-only authenticated review and authorization service."""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from config import get_config
from core import jira_client
from core.executing import worker as execution_worker
from core.runtime_logger import get_logger, redact
from core.operations import (
    AttachmentResult,
    StoreError,
    get_services,
    is_jira_issue_key,
    is_operation_id,
    store_error_data,
)
from web.review_sessions import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    ReviewSessionManager,
    credential_ttl_minutes,
    default_manager,
)

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_templates = Jinja2Templates(directory=os.path.join(_WEB_DIR, "templates"))
_server_lock = threading.Lock()
_server_thread: threading.Thread | None = None
_server: Any | None = None
_actual_port: int | None = None
_STARTUP_TIMEOUT_SECONDS = 5.0


def _validated_operation_id(value: str) -> str:
    if not is_operation_id(value):
        raise ValueError("operation ID 格式无效")
    return value


OperationId = Annotated[
    str,
    Field(min_length=35, max_length=35),
    AfterValidator(_validated_operation_id),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IdListModel(StrictModel):
    operation_ids: list[OperationId] = Field(min_length=1, max_length=1000)

    @field_validator("operation_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("operation_ids 不得重复")
        return value


class SubmitRequest(IdListModel):
    expected_queue_revision: int = Field(ge=0)
    high_risk_confirmed: bool


class CancelRequest(IdListModel):
    expected_queue_revision: int = Field(ge=0)


class ResolveIndeterminateRequest(StrictModel):
    operation_id: OperationId
    resolution: Literal["succeeded", "not_applied", "abandoned"]
    high_risk_confirmed: bool = False
    new_issue_key: str | None = Field(
        default=None, max_length=255
    )
    attachment_result: AttachmentResult | None = None

    @model_validator(mode="after")
    def key_only_for_success(self) -> "ResolveIndeterminateRequest":
        if self.new_issue_key is not None and not is_jira_issue_key(
            self.new_issue_key
        ):
            raise ValueError("new_issue_key 格式无效")
        if self.resolution != "succeeded" and (
            self.new_issue_key is not None
            or self.attachment_result is not None
        ):
            raise ValueError("仅已生效处置可以提供生产者结果")
        return self


class ResolveHeldRequest(IdListModel):
    action: Literal["resume", "requeue", "abandon"]


def create_confirmation_url(base_url: str) -> str:
    return default_manager.create_confirmation_url(base_url)


def confirmation_credential_ttl_minutes() -> int:
    return credential_ttl_minutes()


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "确认会话无效，请重新打开确认页面"}, status_code=403)


def _store_error(exc: Exception) -> JSONResponse:
    data = store_error_data(exc) if isinstance(exc, StoreError) else {
        "error": str(exc), "code": "state_conflict",
    }
    data["error"] = redact(str(data["error"]))
    return JSONResponse(data, status_code=409)


def create_app(
    session_manager: ReviewSessionManager | None = None,
) -> FastAPI:
    session_manager = session_manager or default_manager
    app = FastAPI(title="jira-local-mcp review")
    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(_WEB_DIR, "static")),
        name="static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        path = request.url.path
        protected = path in {"/", "/queue"} or path.startswith("/api/")
        renewal_id: str | None = None
        if protected:
            decision = session_manager.authenticate(
                request.cookies.get(SESSION_COOKIE)
            )
            renewal_id = decision.renewed_session_id
            if not decision.valid:
                response = (
                    _unauthorized()
                    if path.startswith("/api/")
                    else HTMLResponse(
                        "确认会话无效，请重新调用 jira_show_queue。",
                        status_code=403,
                    )
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        if renewal_id is not None:
            response.set_cookie(
                SESSION_COOKIE,
                renewal_id,
                max_age=int(SESSION_TTL_SECONDS),
                httponly=True,
                samesite="strict",
                path="/",
            )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    @app.get("/queue", response_class=HTMLResponse)
    def review_page(request: Request) -> Any:
        return _templates.TemplateResponse(request, "confirm.html", {})

    @app.get("/bootstrap/{token}", include_in_schema=False)
    def bootstrap(token: str) -> Any:
        session_id = session_manager.exchange_bootstrap(token)
        if session_id is None:
            return HTMLResponse(
                "确认链接无效或已过期，请重新打开确认页面。", status_code=403
            )
        response = RedirectResponse("/queue", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=int(SESSION_TTL_SECONDS),
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/state")
    def api_state(request: Request) -> Any:
        state = get_services().query.get_state().model_dump(mode="json")
        revisions = state["revisions"]
        etag = f'W/"q{revisions["queue"]}-e{revisions["execution"]}-h{revisions["history"]}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(state, headers={"ETag": etag})

    @app.post("/api/queue/cancel")
    def api_cancel(body: CancelRequest) -> Any:
        try:
            cancelled, revision = get_services().queue.cancel(
                body.expected_queue_revision,
                body.operation_ids,
            )
            return JSONResponse(
                {
                    "cancelled_operation_ids": cancelled,
                    "queue_revision": revision,
                }
            )
        except StoreError as exc:
            return _store_error(exc)

    @app.post("/api/queue/submit")
    def api_submit(body: SubmitRequest) -> Any:
        try:
            submission = get_services().submission
            submission.validate_submission(
                body.expected_queue_revision,
                body.operation_ids,
                body.high_risk_confirmed,
            )
            if not jira_client.ping():
                return JSONResponse(
                    {"error": "Jira 连接不可用，队列保持不变"}, status_code=503
                )
            accepted, revisions = submission.submit(
                body.expected_queue_revision,
                body.operation_ids,
                body.high_risk_confirmed,
            )
            execution_worker.wake_worker()
            return JSONResponse(
                {"accepted_operation_ids": accepted, "revisions": revisions},
                status_code=202,
            )
        except (StoreError, ValueError) as exc:
            return _store_error(exc)
        except Exception as exc:
            get_logger().error(f"/api/queue/submit 异常: {type(exc).__name__}")
            return JSONResponse({"error": "服务器内部错误"}, status_code=500)

    @app.post("/api/execution/indeterminate/resolve")
    def api_resolve_indeterminate(
        body: ResolveIndeterminateRequest
    ) -> Any:
        try:
            result = get_services().recovery.resolve_indeterminate(
                body.operation_id,
                body.resolution,
                body.high_risk_confirmed,
                body.new_issue_key,
                body.attachment_result,
            )
            execution_worker.wake_worker()
            return JSONResponse(result)
        except StoreError as exc:
            return _store_error(exc)

    @app.post("/api/execution/held/resolve")
    def api_resolve_held(body: ResolveHeldRequest) -> Any:
        try:
            result = get_services().recovery.resolve_held(
                body.operation_ids, body.action
            )
            execution_worker.wake_worker()
            return JSONResponse(result)
        except StoreError as exc:
            return _store_error(exc)

    @app.get("/api/execution-history")
    def api_execution_history(
        start_at: int | None = Query(default=None, ge=0),
        end_at: int | None = Query(default=None, ge=0),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> Any:
        if start_at is not None and end_at is not None and end_at <= start_at:
            return JSONResponse({"error": "end_at 必须晚于 start_at"}, status_code=422)
        return JSONResponse(
            get_services()
            .query.execution_history(start_at, end_at, page, page_size)
            .model_dump(mode="json")
        )

    return app


def _find_free_port(start: int) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"未能在 {start}-{start+99} 找到可用端口")


def ensure_started() -> str:
    global _server_thread, _server, _actual_port
    execution_worker.start_worker()
    with _server_lock:
        if _server_thread is not None and _server_thread.is_alive():
            if _actual_port is None:
                raise RuntimeError("Web 服务仍在启动")
            return f"http://127.0.0.1:{_actual_port}"
        import uvicorn

        port = _find_free_port(get_config().web_port)
        _actual_port = None
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(), host="127.0.0.1", port=port, log_level="warning"
            )
        )
        _server = server
        _server_thread = threading.Thread(
            target=server.run, daemon=True, name="jira-web"
        )
        _server_thread.start()
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not _server_thread.is_alive():
                _server_thread = None
                _server = None
                raise RuntimeError("Web 确认服务启动失败")
            if server.started:
                _actual_port = port
                break
            time.sleep(0.01)
        else:
            server.should_exit = True
            _server_thread.join(timeout=1)
            _server_thread = None
            _server = None
            raise RuntimeError("Web 确认服务启动超时")
        get_logger().info(f"Web 确认服务已启动: http://127.0.0.1:{port}")
        return f"http://127.0.0.1:{port}"
