"""Web 确认服务（FastAPI + 内嵌 uvicorn）。

- 仅监听 127.0.0.1，端口冲突自动递增（spec §8.4）。
- 在 daemon 线程中运行 uvicorn，MCP 主进程不阻塞。
- 提交 /api/commit 是唯一真实执行入口，绝不通过 MCP 暴露；需 nonce 校验。
"""
from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from config import get_config
from core import commit_guard, jira_executor
from core.audit_logger import get_logger
from core.queue_manager import get_queue

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# Web 服务运行时状态
_server_lock = threading.Lock()
_server_thread: threading.Thread | None = None
_server: Any | None = None
_actual_port: int | None = None

_STARTUP_TIMEOUT_SECONDS = 5.0
_BOOTSTRAP_TTL_SECONDS = 60.0
_SESSION_TTL_SECONDS = 15 * 60.0
_SESSION_COOKIE = "jira_confirm_session"

# bootstrap capability 只交给默认浏览器；换取的会话仅保存在 HttpOnly Cookie 中。
_session_lock = threading.Lock()
_bootstrap_tokens: dict[str, float] = {}
_sessions: dict[str, float] = {}

BATCH_ID_PATTERN = r"^batch-[0-9a-f]{32}$"
TASK_ID_PATTERN = r"^task-[0-9a-f]{32}$"
NONCE_PATTERN = r"^[0-9A-Fa-f]{8}$"

BatchId = Annotated[str, Field(pattern=BATCH_ID_PATTERN)]
TaskId = Annotated[str, Field(pattern=TASK_ID_PATTERN)]


class CommitRequest(BaseModel):
    """严格的提交请求；未知字段和隐式类型转换均拒绝。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    batch_id: BatchId
    task_ids: list[TaskId] = Field(min_length=1, max_length=1000)
    nonce: str = Field(pattern=NONCE_PATTERN)


class CancelRequest(BaseModel):
    """严格的整批取消请求。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    batch_id: BatchId


class ResolveIndeterminateRequest(BaseModel):
    """人工处置不确定任务；该操作本身不会写 Jira。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    batch_id: BatchId
    task_id: TaskId
    resolution: Literal["succeeded", "not_applied", "abandoned"]


def _purge_expired_sessions_locked(now: float) -> None:
    for token, expires_at in list(_bootstrap_tokens.items()):
        if expires_at <= now:
            del _bootstrap_tokens[token]
    for session_id, expires_at in list(_sessions.items()):
        if expires_at <= now:
            del _sessions[session_id]


def create_confirmation_url(base_url: str) -> str:
    """创建只供默认浏览器使用的一次性、短时 bootstrap capability。"""
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _session_lock:
        _purge_expired_sessions_locked(now)
        _bootstrap_tokens[token] = now + _BOOTSTRAP_TTL_SECONDS
    return f"{base_url.rstrip('/')}/bootstrap/{token}"


def confirmation_session_ttl_minutes() -> int:
    """返回供确认工具向用户展示的浏览器会话有效分钟数。"""
    return int(_SESSION_TTL_SECONDS // 60)


def _has_valid_session(request: Request) -> bool:
    session_id = request.cookies.get(_SESSION_COOKIE)
    if not session_id:
        return False
    now = time.monotonic()
    with _session_lock:
        _purge_expired_sessions_locked(now)
        expires_at = _sessions.get(session_id)
        if expires_at is None:
            return False
        return True


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "确认会话无效，请重新打开确认页面"}, status_code=403)


def create_app() -> FastAPI:
    app = FastAPI(title="jira-local-mcp confirm")

    def _batch_payload(batch: Any) -> dict[str, Any]:
        return {
            "batch_id": batch.batch_id,
            "nonce": batch.nonce,  # nonce 仅经此 HTTP 通道展示
            "created_at": batch.created_at,
            "kind": batch.kind,
            "recovery_from": batch.recovery_from,
            "tasks": [
                task.to_public_dict()
                for task in batch.tasks
                if task.state == "pending"
            ],
        }

    @app.get("/", response_class=HTMLResponse)
    @app.get("/queue/all", response_class=HTMLResponse)
    def confirm_page(request: Request) -> Any:
        if not _has_valid_session(request):
            return HTMLResponse(
                "确认会话无效，请重新调用 jira_show_pending_queue 打开确认页面。",
                status_code=403,
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
                    "确认链接无效或已过期，请重新打开确认页面。",
                    status_code=403,
                )
            session_id = secrets.token_urlsafe(32)
            _sessions[session_id] = now + _SESSION_TTL_SECONDS

        response = RedirectResponse("/queue/all", status_code=303)
        response.set_cookie(
            _SESSION_COOKIE,
            session_id,
            max_age=int(_SESSION_TTL_SECONDS),
            httponly=True,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/queue/all")
    def api_queue(request: Request) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        queue = get_queue()
        queue.recover_interrupted_tasks()
        batches = [
            _batch_payload(batch)
            for batch in queue.list_presented_batches()
        ]
        attention_required = []
        for task in queue.list_indeterminate_tasks():
            item = task.to_public_dict()
            item["state"] = task.state
            attention_required.append(item)
        return JSONResponse(
            {
                "batches": batches,
                "attention_required": attention_required,
                "execution_busy": commit_guard.execution_busy(),
            }
        )

    @app.post("/api/commit")
    async def api_commit(request: Request, body: CommitRequest) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        try:
            result = jira_executor.execute_batch(
                body.batch_id, body.task_ids, body.nonce
            )
            return JSONResponse(result)
        except jira_executor.CommitBusyError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except jira_executor.CommitError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # 兜底，不泄露栈
            get_logger().error(f"/api/commit 异常: {type(e).__name__}")
            return JSONResponse({"error": "服务器内部错误"}, status_code=500)

    @app.post("/api/cancel")
    async def api_cancel(request: Request, body: CancelRequest) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        removed = get_queue().remove_batch(body.batch_id)
        if not removed:
            raise HTTPException(status_code=404, detail="批次不存在或不可取消")
        return JSONResponse({"batch_id": body.batch_id, "removed": removed})

    @app.post("/api/indeterminate/resolve")
    async def api_resolve_indeterminate(
        request: Request, body: ResolveIndeterminateRequest
    ) -> Any:
        if not _has_valid_session(request):
            return _unauthorized()
        try:
            result = get_queue().resolve_indeterminate(
                body.batch_id, body.task_id, body.resolution
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        jira_executor.append_reconciliation_log(body.batch_id, result)
        return JSONResponse(
            {
                "batch_id": body.batch_id,
                "task_id": body.task_id,
                "state": result.resolved_state,
                "resolution": body.resolution,
                "execution_id": result.original_task.execution_id,
                "recovery_task_id": (
                    result.recovery_task.task_id if result.recovery_task else None
                ),
                "recovery_batch_id": result.recovery_batch_id,
            }
        )

    return app


def _find_free_port(start: int) -> int:
    port = start
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port  # 未占用
        port += 1
    raise RuntimeError(f"未能在 {start}-{start+99} 找到可用端口")


def ensure_started() -> str:
    """确保 Web 服务已启动，返回访问 base url。幂等。"""
    global _server_thread, _server, _actual_port
    with _server_lock:
        if _server_thread is not None and _server_thread.is_alive():
            if _actual_port is None:
                raise RuntimeError("Web 确认服务仍在启动，尚未就绪")
            return f"http://127.0.0.1:{_actual_port}"

        import uvicorn

        cfg = get_config()
        port = _find_free_port(cfg.web_port)
        _actual_port = None
        app = create_app()

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            # uvicorn 默认日志走 stderr（access/default），不污染 stdout
        )
        server = uvicorn.Server(config)
        _server = server

        def _run() -> None:
            server.run()

        _server_thread = threading.Thread(target=_run, daemon=True, name="jira-web")
        _server_thread.start()

        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not _server_thread.is_alive():
                _server_thread = None
                _server = None
                _actual_port = None
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
            _actual_port = None
            raise RuntimeError("Web 确认服务启动超时，Uvicorn 尚未就绪")

        get_logger().info(f"Web 确认服务已启动: http://127.0.0.1:{port}")
        return f"http://127.0.0.1:{port}"


def get_base_url() -> str | None:
    if _actual_port is None:
        return None
    return f"http://127.0.0.1:{_actual_port}"
