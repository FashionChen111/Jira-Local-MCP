"""面向 MCP 的本地审核队列、撤回、活动执行状态与执行历史工具。"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from core.operations import StoreError, get_services, store_error_data
from core.runtime_logger import get_logger, redact


PageNumber = Annotated[int, Field(strict=True, ge=1)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=200)]
QueueRevision = Annotated[int, Field(strict=True, ge=0)]
OperationId = Annotated[str, Field(strict=True, min_length=35, max_length=35)]
DetailOperationIds = Annotated[list[OperationId], Field(min_length=1, max_length=200)]
CancelOperationIds = Annotated[list[OperationId], Field(min_length=1, max_length=1000)]


def _operation_error(exc: Exception) -> str:
    if isinstance(exc, StoreError):
        data = store_error_data(exc)
    elif isinstance(exc, ValueError):
        data = {"code": "invalid_request", "error": str(exc)}
    else:
        get_logger().error(
            redact(f"本地 operation 请求异常: {type(exc).__name__}: {exc}")
        )
        data = {"code": "internal_error", "error": "本地 operation 请求失败，请查看服务日志"}
    return redact(json.dumps(data, ensure_ascii=False))


def jira_list_queue(page: PageNumber = 1, page_size: PageSize = 50) -> str:
    """分页读取本地待审核队列概览，便于先定位操作再按 ID 获取详情。

    参数：
        page: 页码，从 1 开始，默认为 1。
        page_size: 每页条数，范围 1 至 200，默认为 50。

    返回：
        JSON 清单，包含 items、total、page、page_size 和 revisions。条目仅选择现有
        记录的标识、类型、目标、描述、风险和创建时间，不包含 payload。每次读取当前
        快照；revision 改变时，不得将不同页视为同一快照。业务失败返回 code 和 error。

    副作用：
        只读本地状态，不会修改 Jira、入队、撤回或启动 Web 审核服务。
    """
    try:
        result = get_services().query.list_queue(page, page_size)
        return redact(result.model_dump_json())
    except Exception as exc:
        return _operation_error(exc)


def jira_get_queue_operations(operation_ids: DetailOperationIds) -> str:
    """按 operation ID 批量读取完整待审核记录，供解释内容或准备取消后重新入队。

    参数：
        operation_ids: 1 至 200 个合法且不重复的服务端 operation ID。

    返回：
        JSON 的 items 按请求顺序返回完整公开记录，包括 payload、依赖及提交阻塞原因，
        依赖以 dependency_states 映射返回，键为 operation ID，值为生命周期状态，
        不表达执行顺序；不改写或截断正文。同时返回 revisions 和 missing_operation_ids。缺失仅说明
        不在当前队列，不能据此判断完成或取消。业务失败返回 code 和 error。

    副作用：
        只读本地状态，不会修改 Jira、修改队列或启动 Web 审核服务。
    """
    try:
        result = get_services().query.get_queue_operations(operation_ids)
        return redact(result.model_dump_json())
    except Exception as exc:
        return _operation_error(exc)


def jira_list_execution_state(page: PageNumber = 1, page_size: PageSize = 50) -> str:
    """分页读取已提交但尚未结束的执行状态，按已授权的执行顺序排列。

    参数：
        page: 页码，从 1 开始，默认为 1。
        page_size: 每页条数，范围 1 至 200，默认为 50。

    返回：
        JSON 的 items、total、page、page_size 和 revisions，涵盖 staged、executing、
        indeterminate、held 的记录概览及执行元数据，不含 payload。各次调用为当前快照，
        revision 改变时不能拼接为同一快照。消失不代表成功；完成结果通过
        jira_get_execution_log 查询。业务失败返回 code 和 error。

    副作用：
        只读本地状态，不会修改 Jira、提交、执行或恢复任何操作。
    """
    try:
        result = get_services().query.list_execution_state(page, page_size)
        return redact(result.model_dump_json())
    except Exception as exc:
        return _operation_error(exc)


def jira_cancel_queue_operations(
    operation_ids: CancelOperationIds, expected_queue_revision: QueueRevision,
) -> str:
    """立即撤回明确选择的 queued 操作；范围必须包含全部直接和间接依赖。

    参数：
        operation_ids: 1 至 1000 个合法且不重复的待审核 operation ID，须覆盖依赖闭包。
        expected_queue_revision: 最近查询返回的非负 queue revision，用于乐观并发校验。

    返回：
        成功 JSON 含 cancelled_operation_ids 和 queue_revision。失败含 code 和 error；
        dependency_scope_incomplete 同时返回 affected_operation_ids 和 missing_operation_ids。
        范围不完整、版本冲突或任一 ID 已离开队列均整批拒绝，不自动扩大范围或重试。

    副作用：
        原子删除本地 queued 条目并记录取消事件，不会修改 Jira，不会撤回已提交操作。
        修改应先读取详情，再取消并通过 *_append 重新入队，使用新 ID 重建依赖链；
        两步不原子，重新入队失败时旧操作已撤回，新操作仍需要本地 Web 人工审核提交。
    """
    try:
        cancelled, revision = get_services().queue.cancel(
            expected_queue_revision, operation_ids,
        )
        return redact(json.dumps({
            "cancelled_operation_ids": cancelled, "queue_revision": revision,
        }, ensure_ascii=False))
    except Exception as exc:
        return _operation_error(exc)


ExecutionLogLimit = Annotated[int, Field(strict=True, ge=1)]


def jira_get_execution_log(limit: ExecutionLogLimit = 10) -> str:
    """读取最近持久化的执行尝试及人工对账结果；这是只读工具。

    参数：
        limit: 最多返回的最近记录数，必须是正整数，默认为 10。

    返回：
        脱敏后的 JSON 执行与对账历史，或状态/错误信息。本工具不会将操作
        入队，也不会执行 Jira 写入。

    副作用：
        无 Jira 写入；只会读取本地持久化的执行历史。
    """
    try:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit 必须是大于等于 1 的整数")
        records = get_services().query.read_execution_log(limit)
        return (
            "暂无执行记录。"
            if not records
            else redact(json.dumps(records, ensure_ascii=False, indent=2))
        )
    except Exception as exc:
        return f"查询执行日志失败: {redact(type(exc).__name__ + ': ' + str(exc))}"
