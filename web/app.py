"""Localhost-only authenticated review and authorization service."""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config import get_config
from core import jira_client
from core.execution import worker as execution_worker
from core.runtime_logger import get_logger
from core.operations import (
    ConfirmationRequired,
    DependencyConfirmationRequired,
    ExecutionBlocked,
    RevisionConflict,
    StoreError,
    get_store,
)

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_templates = Jinja2Templates(directory=os.path.join(_WEB_DIR, "templates"))
_server_lock = threading.Lock()
_server_thread: threading.Thread | None = None
_server: Any | None = None
_actual_port: int | None = None
_STARTUP_TIMEOUT_SECONDS = 5.0
_BOOTSTRAP_TTL_SECONDS = 60.0
_SESSION_TTL_SECONDS = 15 * 60.0
_SESSION_COOKIE = "jira_confirm_session"
_session_lock = threading.Lock()
_bootstrap_tokens: dict[str, float] = {}
_sessions: dict[str, float] = {}
OperationId = Annotated[str, Field(pattern=r"^op-[0-9a-f]{32}$")]


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


class QueueGroup(StrictModel):
    issue_key: str = Field(min_length=1, max_length=255)
    operation_ids: list[OperationId] = Field(min_length=1, max_length=1000)

    @field_validator("operation_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("组内 operation_ids 不得重复")
        return value


class PlannedQueueGroup(StrictModel):
    producer_operation_id: OperationId
    operation_ids: list[OperationId] = Field(min_length=1, max_length=1000)

    @field_validator("operation_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("计划分组内 operation_ids 不得重复")
        return value


class ReorderRequest(StrictModel):
    expected_queue_revision: int = Field(ge=0)
    groups: list[QueueGroup] = Field(max_length=1000)
    planned_groups: list[PlannedQueueGroup] = Field(
        default_factory=list, max_length=1000
    )
    standalone_operation_ids: list[OperationId] = Field(max_length=1000)

    @model_validator(mode="after")
    def complete_unique_layout(self) -> "ReorderRequest":
        ids = [item for group in self.groups for item in group.operation_ids]
        ids.extend(
            item
            for group in self.planned_groups
            for item in group.operation_ids
        )
        ids.extend(self.standalone_operation_ids)
        if len(ids) > 1000:
            raise ValueError("排序布局最多包含 1000 个 operation")
        if len(ids) != len(set(ids)):
            raise ValueError("排序布局中的 operation ID 不得重复")
        if len({group.issue_key for group in self.groups}) != len(self.groups):
            raise ValueError("issue 分组不得重复")
        if len(
            {group.producer_operation_id for group in self.planned_groups}
        ) != len(self.planned_groups):
            raise ValueError("待创建事务分组不得重复")
        return self


class SubmitRequest(IdListModel):
    expected_queue_revision: int = Field(ge=0)
    high_risk_confirmed: bool


class CancelRequest(IdListModel):
    expected_queue_revision: int = Field(ge=0)
    cascade_confirmed: bool = False


class ResolveIndeterminateRequest(StrictModel):
    operation_id: OperationId
    resolution: Literal["succeeded", "not_applied", "abandoned"]
    high_risk_confirmed: bool = False
    new_issue_key: str | None = Field(
        default=None, pattern=r"^[A-Z][A-Z0-9]+-\d+$", max_length=255
    )

    @model_validator(mode="after")
    def key_only_for_success(self) -> "ResolveIndeterminateRequest":
        if self.resolution != "succeeded" and self.new_issue_key is not None:
            raise ValueError("仅已生效处置可以提供 new_issue_key")
        return self


class ResolveHeldRequest(IdListModel):
    action: Literal["resume", "requeue", "abandon"]


def _purge_expired_sessions_locked(now: float) -> None:
    for mapping in (_bootstrap_tokens, _sessions):
        for token, expires_at in list(mapping.items()):
            if expires_at <= now:
                del mapping[token]


def create_confirmation_url(base_url: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _session_lock:
        _purge_expired_sessions_locked(now)
        _bootstrap_tokens[token] = now + _BOOTSTRAP_TTL_SECONDS
    return f"{base_url.rstrip('/')}/bootstrap/{token}"


def confirmation_session_ttl_minutes() -> int:
    return int(_SESSION_TTL_SECONDS // 60)


def _has_valid_session(request: Request) -> bool:
    session_id = request.cookies.get(_SESSION_COOKIE)
    if not session_id:
        return False
    now = time.monotonic()
    with _session_lock:
        _purge_expired_sessions_locked(now)
        return session_id in _sessions


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "确认会话无效，请重新打开确认页面"}, status_code=403)


def _store_error(exc: Exception) -> JSONResponse:
    extra: dict[str, Any] = {}
    if isinstance(exc, DependencyConfirmationRequired):
        code = "dependency_confirmation_required"
        extra["affected_operation_ids"] = exc.operation_ids
    elif isinstance(exc, ConfirmationRequired):
        code = "confirmation_required"
    elif isinstance(exc, RevisionConflict):
        code = "revision_conflict"
    elif isinstance(exc, ExecutionBlocked):
        code = "execution_blocked"
    else:
        code = "state_conflict"
    return JSONResponse(
        {"error": str(exc), "code": code, **extra}, status_code=409
    )


def create_app() -> FastAPI:
    app = FastAPI(title="jira-local-mcp review")
    app.mount(
        "/static",
        StaticFiles(directory=os.path.join(_WEB_DIR, "static")),
        name="static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
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
        if not _has_valid_session(request):
            return HTMLResponse(
                "确认会话无效，请重新调用 jira_show_queue。", status_code=403
            )
        return _templates.TemplateResponse(request, "confirm.html", {})

    @app.get("/bootstrap/{token}", include_in_schema=False)
    def bootstrap(token: str) -> Any:
        now = time.monotonic()
        with _session_lock:
            _purge_expired_sessions_locked(now)
            expires_at = _bootstrap_tokens.pop(token, None)
            if expires_at is None or expires_at <= now:
                return HTMLResponse(
                    "确认链接无效或已过期，请重新打开确认页面。", status_code=403
                )
            session_id = secrets.token_urlsafe(32)
            _sessions[session_id] = now + _SESSION_TTL_SECONDS
        response = RedirectResponse("/queue", status_code=303)
        response.set_cookie(
            _SESSION_COOKIE,
            session_id,
            max_age=int(_SESSION_TTL_SECONDS),
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/state")
    def api_state(request: Request) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        state = get_store().get_state()
        revisions = state["revisions"]
        etag = f'W/"q{revisions["queue"]}-e{revisions["execution"]}-h{revisions["history"]}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(state, headers={"ETag": etag})

    @app.post("/api/queue/reorder")
    def api_reorder(request: Request, body: ReorderRequest) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        try:
            revision = get_store().reorder(
                body.expected_queue_revision,
                [group.model_dump() for group in body.groups],
                body.standalone_operation_ids,
                [group.model_dump() for group in body.planned_groups],
            )
            return JSONResponse({"queue_revision": revision})
        except (StoreError, ValueError) as exc:
            return _store_error(exc)

    @app.post("/api/queue/cancel")
    def api_cancel(request: Request, body: CancelRequest) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        try:
            cancelled, revision = get_store().cancel(
                body.expected_queue_revision,
                body.operation_ids,
                body.cascade_confirmed,
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
    def api_submit(request: Request, body: SubmitRequest) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        try:
            store = get_store()
            store.validate_submission(
                body.expected_queue_revision,
                body.operation_ids,
                body.high_risk_confirmed,
            )
            if not jira_client.ping():
                return JSONResponse(
                    {"error": "Jira 连接不可用，队列保持不变"}, status_code=503
                )
            accepted, revisions = store.submit(
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
        request: Request, body: ResolveIndeterminateRequest
    ) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        try:
            result = get_store().resolve_indeterminate(
                body.operation_id,
                body.resolution,
                body.high_risk_confirmed,
                body.new_issue_key,
            )
            execution_worker.wake_worker()
            return JSONResponse(result)
        except StoreError as exc:
            return _store_error(exc)

    @app.post("/api/execution/held/resolve")
    def api_resolve_held(request: Request, body: ResolveHeldRequest) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        try:
            result = get_store().resolve_held(body.operation_ids, body.action)
            execution_worker.wake_worker()
            return JSONResponse(result)
        except StoreError as exc:
            return _store_error(exc)

    @app.get("/api/execution-history")
    def api_execution_history(
        request: Request,
        start_at: int | None = Query(default=None, ge=0),
        end_at: int | None = Query(default=None, ge=0),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        if start_at is not None and end_at is not None and end_at <= start_at:
            return JSONResponse({"error": "end_at 必须晚于 start_at"}, status_code=422)
        return JSONResponse(
            get_store().execution_history(start_at, end_at, page, page_size)
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
