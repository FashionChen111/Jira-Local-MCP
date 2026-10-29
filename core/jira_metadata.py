"""Scoped Jira metadata reads and JSON projections, independent of MCP and queues."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Annotated, Literal

from jira.exceptions import JIRAError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationInfo, field_validator
from requests.exceptions import RequestException

from core.jira_client import get_client
from core.runtime_logger import redact

MAX_RESPONSE_BYTES = 32 * 1024
MAX_FIELD_SCAN = 1000


class MetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after")
    @classmethod
    def valid_identifier(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str):
            if not value.strip() or any(char in value for char in ("/", "\\", "\x00")):
                raise ValueError("标识必须非空且不得包含路径")
            if info.field_name == "issue_key" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*", value):
                raise ValueError("必须提供真实 Jira Key，不能使用 operation ID")
            if info.field_name in {"issue_type_id", "transition_id"} and not (value.isascii() and value.isdecimal()):
                raise ValueError("ID 必须为十进制数字字符串")
        return value


class CreateContext(MetadataInput):
    kind: Literal["create"]
    project_key: str
    issue_type_id: str


class EditContext(MetadataInput):
    kind: Literal["edit"]
    issue_key: str


class TransitionContext(MetadataInput):
    kind: Literal["transition"]
    issue_key: str
    transition_id: str


class CatalogContext(MetadataInput):
    kind: Literal["catalog"]


class ProjectContext(MetadataInput):
    kind: Literal["project"]
    project_key: str


class IssueContext(MetadataInput):
    kind: Literal["issue"]
    issue_key: str


WriteContext = Annotated[CreateContext | EditContext | TransitionContext, Field(discriminator="kind")]
FieldContext = Annotated[CreateContext | EditContext | TransitionContext | CatalogContext, Field(discriminator="kind")]
ProjectOrIssueContext = Annotated[ProjectContext | IssueContext, Field(discriminator="kind")]


class DirectoryScope(MetadataInput):
    kind: Literal["directory"] = "directory"


class FieldScope(MetadataInput):
    kind: Literal["field"]
    context: WriteContext
    field_id: str


UserSearchScope = Annotated[DirectoryScope | IssueContext | FieldScope, Field(discriminator="kind")]


def json_value(value: object) -> JsonValue:
    """Preserve JSON identities while unwrapping SDK resources recursively."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON 不支持非有限数值")
        return value
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    raw: object = getattr(value, "raw", None)
    if isinstance(raw, Mapping):
        return json_value(raw)
    raise ValueError(f"无法序列化 SDK 值：{type(value).__name__}")


def object_value(value: object) -> dict[str, JsonValue]:
    result = json_value(value)
    if not isinstance(result, dict):
        raise ValueError("Jira 响应应为对象")
    return result


def project_value(value: object) -> JsonValue:
    """Remove presentation-only properties without converting values to labels."""
    data = json_value(value)
    if isinstance(data, list):
        return [project_value(item) for item in data]
    if isinstance(data, dict):
        return {key: project_value(item) for key, item in data.items()
                if key not in {"self", "avatarUrls", "iconUrl", "emailAddress", "html"}}
    return data


def selected(data: Mapping[str, JsonValue], names: Sequence[str]) -> dict[str, JsonValue]:
    return {name: project_value(data[name]) for name in names if name in data}


def error_response(exc: Exception) -> dict[str, JsonValue]:
    """Expose Jira's response, never the headers embedded in str(JIRAError)."""
    cause = exc
    while not isinstance(cause, (JIRAError, RequestException)) and isinstance(cause.__cause__, Exception):
        cause = cause.__cause__
    if isinstance(cause, JIRAError):
        response = cause.response
        body: JsonValue = cause.text or ""
        status: JsonValue = cause.status_code
        if response is not None:
            status = response.status_code
            try:
                body = json_value(response.json())
            except (ValueError, TypeError):
                body = response.text
        result: dict[str, JsonValue] = {"source": "jira", "status_code": status, "body": body}
    else:
        source = "transport" if isinstance(cause, RequestException) else "tool" if isinstance(cause, (ValueError, TypeError)) else "sdk"
        result = {"source": source, "exception": type(cause).__name__, "message": str(cause)}
    safe = object_value(json.loads(redact(json.dumps(result, ensure_ascii=False))))
    if byte_size(safe) > MAX_RESPONSE_BYTES:
        content = json.dumps(safe.pop("body", safe.pop("message", "")), ensure_ascii=False)
        safe["body"] = content.encode("utf-8")[:MAX_RESPONSE_BYTES // 2].decode("utf-8", errors="ignore")
        safe["truncated"] = True
        while byte_size(safe) > MAX_RESPONSE_BYTES:
            safe["body"] = str(safe["body"])[:len(str(safe["body"])) // 2]
    return safe


def byte_size(value: object) -> int:
    # FastMCP publishes the dictionary as an indented JSON text block as well.
    return len(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def envelope(source: str, context: MetadataInput | None = None) -> dict[str, JsonValue]:
    return {"source": source, "context": json_value(context.model_dump()) if context else None,
            "observed_at": datetime.now(timezone.utc).isoformat()}


def detail(item: JsonValue, source: str, context: MetadataInput | None = None) -> dict[str, JsonValue]:
    result = {**envelope(source, context), "item": item}
    if byte_size(result) > MAX_RESPONSE_BYTES:
        raise ValueError("单项结果超过本地 32 KiB 预算，请缩小查询；未返回截断值")
    return result


def check_page(start_at: int, max_results: int) -> None:
    if type(start_at) is not int or start_at < 0 or type(max_results) is not int or not 1 <= max_results <= 100:
        raise ValueError("start_at 必须非负，max_results 必须在 1–100 之间")


def query_text(query: str | None, *, required: bool = False) -> str | None:
    if query is None and not required:
        return None
    if not isinstance(query, str) or not query.strip() or len(query) > 255:
        raise ValueError("query 必须为 1–255 字符的非空文本")
    return query.strip()


def matches(item: JsonValue, query: str | None) -> bool:
    if query is None:
        return True
    values = [item.get(key) for key in ("id", "field_id", "name", "key", "value")] if isinstance(item, dict) else [item]
    return any(query.casefold() in str(value).casefold() for value in values if value is not None)


def listing(items: list[JsonValue], source: str, context: MetadataInput | None,
            start_at: int, max_results: int, *, query: str | None = None,
            server_page: bool = False, total: int | None = None,
            consumed: int | None = None) -> dict[str, JsonValue]:
    check_page(start_at, max_results)
    query = query_text(query)
    if server_page:
        candidates = list(items)
        count = len(items) if consumed is None else consumed
        more: bool | None = start_at + count < total if total is not None else (None if count else False)
        next_at = start_at + count if more is not False else None
    else:
        filtered = [item for item in items if matches(item, query)]
        filtered.sort(key=lambda item: str(item.get("id", item.get("key", ""))) if isinstance(item, dict) else str(item))
        total = len(filtered)
        candidates = filtered[start_at:start_at + max_results]
        more = start_at + len(candidates) < total
        next_at = start_at + len(candidates) if more else None
    page: dict[str, JsonValue] = {"mode": "server" if server_page else "local", "start_at": start_at,
                                  "max_results": max_results, "total": total, "has_more": more, "next_start_at": next_at}
    result = {**envelope(source, context), "items": candidates, "page": page}
    if candidates and byte_size({**result, "items": candidates[:1]}) > MAX_RESPONSE_BYTES:
        raise ValueError("单条记录超过返回预算，无法安全分页")
    while byte_size(result) > MAX_RESPONSE_BYTES and candidates:
        candidates.pop()
        page.update(has_more=True, next_start_at=start_at + len(candidates))
        if server_page and consumed is not None:
            raise ValueError("过滤后的源页超过返回预算，请减小 max_results；未推进游标")
    return result


def _rows(values: object) -> list[dict[str, JsonValue]]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("Jira 列表响应结构不受支持")
    return [object_value(item) for item in values]


def _source_total(values: object) -> int | None:
    total: object = getattr(values, "total", None)
    return total if type(total) is int else None


def server_settings(section: Literal["attachments", "time_tracking", "features"]) -> dict[str, JsonValue]:
    client = get_client()
    if section == "attachments":
        raw = object_value(client.attachment_meta())
        item = {key: raw[src] for key, src in (("enabled", "enabled"), ("upload_limit_bytes", "uploadLimit")) if src in raw}
    else:
        raw = object_value(client._get_json("configuration"))
        item = selected(raw, ("issueLinkingEnabled", "subTasksEnabled")) if section == "features" else selected(raw, ("timeTrackingEnabled", "timeTrackingConfiguration"))
    return detail(item, f"configuration:{section}")


def user(username: str | None) -> dict[str, JsonValue]:
    raw = object_value(get_client().myself() if username is None else get_client().user(query_text(username, required=True)))
    return detail(_user(raw, full=True), "myself" if username is None else "user")


def _user(raw: dict[str, JsonValue], *, full: bool = False) -> dict[str, JsonValue]:
    item = selected(raw, ("name", "key", "displayName", "active", "timeZone", "locale") if full else ("name", "key", "displayName", "active"))
    if isinstance(raw.get("name"), str):
        item["mention"] = f"[~{raw['name']}]"
    return item


def search_users(query: str, scope: UserSearchScope, start_at: int, max_results: int) -> dict[str, JsonValue]:
    query = query_text(query, required=True) or ""
    check_page(start_at, max_results)
    client = get_client()
    if isinstance(scope, FieldScope):
        if start_at != 0:
            raise ValueError("字段 picker 不支持 offset；start_at 必须为 0")
        field = field_detail(scope.context, scope.field_id, None, 0, 100)
        item = field.get("item")
        if not isinstance(item, dict):
            raise ValueError("当前写上下文未找到用户字段，不能调用 picker")
        schema = item.get("schema")
        if not isinstance(schema, dict) or not (
            schema.get("type") == "user"
            or (schema.get("type") == "array" and schema.get("items") == "user")
        ):
            raise ValueError("当前字段不是已识别的用户类型")
        context = scope.context
        if isinstance(context, CreateContext):
            project_id = object_value(client.project(context.project_key)).get("id")
            issue_type_id = context.issue_type_id
        else:
            current = object_value(client.issue(context.issue_key, fields="project,issuetype"))
            current_fields = object_value(current.get("fields", {}))
            project_id = object_value(current_fields.get("project", {})).get("id")
            issue_type_id = object_value(current_fields.get("issuetype", {})).get("id")
        if not isinstance(project_id, str) or not isinstance(issue_type_id, str):
            raise ValueError("字段 picker 缺少从 Jira 读取的项目/类型 ID")
        # Always use this fixed endpoint, never a field-provided autoCompleteUrl.
        raw = object_value(client._get_json("groupuserpicker", params={
            "fieldId": scope.field_id, "projectId": project_id,
            "issueTypeId": issue_type_id, "query": query, "maxResults": max_results,
        }))
        users = object_value(raw.get("users", {}))
        items: list[JsonValue] = [_user(row) for row in _rows(users.get("users", []))]
        if len(items) > max_results:
            raise ValueError("picker 未遵守建议数量上限，请缩小 query")
        total = users.get("total")
        total = total if type(total) is int else None
        result: dict[str, JsonValue] = {
            **envelope("groupuserpicker", scope), "items": items,
            "page": {"mode": "suggestions", "total": total, "next_start_at": None,
                     "has_more": total > len(items) if total is not None else None},
            "candidate_validation": "unverified",
            "message": "字段限定 picker 的过滤语义尚未完成实例验证；以上仅为端点返回的身份建议，不能据此证明该字段合法可选。建议不支持续页，必要时缩小 query。",
        }
        if byte_size(result) > MAX_RESPONSE_BYTES:
            raise ValueError("picker 建议超过返回预算，请缩小 query 或 max_results")
        return result
    if isinstance(scope, IssueContext):
        values = client.search_allowed_users_for_issue(query, issueKey=scope.issue_key, startAt=start_at, maxResults=max_results)
        source = "user/viewissue/search"
    else:
        values = client.search_users(user=query, startAt=start_at, maxResults=max_results, includeActive=True, includeInactive=False)
        source = "user/search"
    return listing([_user(raw) for raw in _rows(values)], source, scope, start_at, max_results, server_page=True)


def assignable_users(context: ProjectOrIssueContext, query: str, start_at: int, max_results: int) -> dict[str, JsonValue]:
    query = query_text(query, required=True) or ""
    check_page(start_at, max_results)
    kwargs = {"project": context.project_key} if isinstance(context, ProjectContext) else {"issueKey": context.issue_key}
    values = get_client().search_assignable_users_for_issues(username=query, startAt=start_at, maxResults=max_results, **kwargs)
    return listing([_user(raw) for raw in _rows(values)], "user/assignable/search", context, start_at, max_results, server_page=True)


def projects(query: str | None, start_at: int, max_results: int) -> dict[str, JsonValue]:
    check_page(start_at, max_results)
    return listing([selected(raw, ("id", "key", "name", "projectTypeKey")) for raw in _rows(get_client().projects())], "projects", None, start_at, max_results, query=query)


def issue_types(project_key: str, purpose: Literal["create", "configured"], start_at: int, max_results: int) -> dict[str, JsonValue]:
    context = ProjectContext(kind="project", project_key=project_key)
    check_page(start_at, max_results)
    if purpose == "create":
        values = get_client().project_issue_types(project_key, startAt=start_at, maxResults=max_results)
    else:
        values = object_value(get_client().project(project_key)).get("issueTypes", [])
    return listing([selected(raw, ("id", "name", "subtask")) for raw in _rows(values)], f"issue_types:{purpose}", context, start_at, max_results,
                   server_page=purpose == "create", total=_source_total(values) if purpose == "create" else None)


def transitions(issue_key: str, start_at: int, max_results: int) -> dict[str, JsonValue]:
    context = IssueContext(kind="issue", issue_key=issue_key)
    check_page(start_at, max_results)
    items: list[JsonValue] = []
    for raw in _rows(get_client().transitions(issue_key)):
        item = selected(raw, ("id", "name", "hasScreen"))
        if isinstance(raw.get("to"), dict):
            item["to"] = selected(object_value(raw["to"]), ("id", "name"))
        items.append(item)
    return listing(items, "transitions", context, start_at, max_results)


def _field_page(context: FieldContext, start_at: int = 0, max_results: int = 100) -> tuple[list[dict[str, JsonValue]], int | None]:
    client = get_client()
    if isinstance(context, CreateContext):
        values = client.project_issue_fields(context.project_key, context.issue_type_id, startAt=start_at, maxResults=max_results)
        return [{**row, "field_id": row.get("fieldId", row.get("id"))} for row in _rows(values)], _source_total(values)
    if isinstance(context, CatalogContext):
        return [{**row, "field_id": row.get("id")} for row in _rows(client.fields())], None
    if isinstance(context, EditContext):
        raw = object_value(client.editmeta(context.issue_key))
    else:
        values = _rows(client.transitions(context.issue_key, id=context.transition_id, expand="transitions.fields"))
        raw = next((row for row in values if str(row.get("id")) == context.transition_id), {})
    fields = raw.get("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("字段元数据不是对象")
    return [{**object_value(value), "field_id": key} for key, value in fields.items()], None


def _field_summary(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result = selected(row, ("field_id", "name", "required", "operations"))
    if isinstance(row.get("schema"), dict):
        result["schema"] = selected(object_value(row["schema"]), ("type", "items", "system", "custom"))
    if "hasDefaultValue" in row:
        result["has_default_value"] = row["hasDefaultValue"]
    values = row.get("allowedValues")
    result["top_level_option_count"] = len(values) if isinstance(values, list) else None
    return result


def fields(context: WriteContext, query: str | None, required_only: bool, start_at: int, max_results: int) -> dict[str, JsonValue]:
    check_page(start_at, max_results)
    query = query_text(query)
    rows, total = _field_page(context, start_at, max_results)
    items: list[JsonValue] = [_field_summary(row) for row in rows if (not required_only or row.get("required") is True) and matches(row, query)]
    result = listing(items, f"fields:{context.kind}", context, start_at, max_results,
                     server_page=isinstance(context, CreateContext), total=total, consumed=len(rows))
    if isinstance(context, CreateContext) and (query or required_only):
        page = object_value(result["page"])
        page["source_total"] = page["total"]
        page["total"] = None
        result["page"] = page
    return result


def _option_nodes(values: list[JsonValue], query: str | None) -> list[tuple[JsonValue, tuple[dict[str, JsonValue], ...]]]:
    result: list[tuple[JsonValue, tuple[dict[str, JsonValue], ...]]] = []
    def walk(nodes: list[JsonValue], ancestors: tuple[dict[str, JsonValue], ...], inherited: bool) -> None:
        for value in nodes:
            matched = inherited or matches(value, query)
            children = value.get("children") if isinstance(value, dict) else None
            node = project_value({key: item for key, item in value.items() if key != "children"} if isinstance(value, dict) else value)
            if matched:
                result.append((node, ancestors))
            if isinstance(children, list) and isinstance(node, dict):
                walk(children, (*ancestors, node), matched)
    walk(values, (), False)
    return result


def _option_tree(nodes: list[tuple[JsonValue, tuple[dict[str, JsonValue], ...]]]) -> list[JsonValue]:
    result: list[JsonValue] = []
    for node, ancestors in nodes:
        target = result
        for ancestor in ancestors:
            found = next((item for item in target if isinstance(item, dict) and {key: value for key, value in item.items() if key != "children"} == ancestor), None)
            if not isinstance(found, dict):
                found = {**ancestor, "children": []}
                target.append(found)
            children = found.setdefault("children", [])
            if not isinstance(children, list):
                raise ValueError("级联 children 结构无效")
            target = children
        target.append(project_value(node))
    return result


def field_detail(context: FieldContext, field_id: str, options_query: str | None, options_start_at: int, options_max_results: int) -> dict[str, JsonValue]:
    if not field_id or not field_id.replace("_", "").isalnum():
        raise ValueError("field_id 必须为字段标识，不能是路径")
    check_page(options_start_at, options_max_results)
    options_query = query_text(options_query)
    if isinstance(context, CatalogContext) and (options_query is not None or options_start_at != 0 or options_max_results != 100):
        raise ValueError("catalog 不支持选项查询参数")
    offset = 0
    row = None
    while offset < MAX_FIELD_SCAN:
        rows, total = _field_page(context, offset)
        row = next((item for item in rows if item.get("field_id") == field_id), None)
        offset += len(rows)
        if row is not None or not isinstance(context, CreateContext) or not rows or (total is not None and offset >= total):
            break
    if row is None:
        result = detail(None, f"field:{context.kind}", context)
        result["message"] = "本次元数据中未找到字段" if offset < MAX_FIELD_SCAN else "达到本地 1000 字段扫描上限，尚未完成查询"
        return result
    item = selected(row, ("field_id", "name", "custom", "schema", "required", "operations", "clauseNames", "searchable", "orderable", "navigable"))
    for src, dest in (("hasDefaultValue", "has_default_value"), ("defaultValue", "default_value")):
        if src in row:
            item[dest] = project_value(row[src])
    values = row.get("allowedValues")
    if isinstance(values, list) and not isinstance(context, CatalogContext):
        nodes = _option_nodes(values, options_query)
        chosen = nodes[options_start_at:options_start_at + options_max_results]
        while True:
            item["allowedValues"] = _option_tree(chosen)
            next_at = options_start_at + len(chosen)
            item["options_page"] = {"mode": "local", "total": len(nodes), "filtered": options_query is not None,
                                    "has_more": next_at < len(nodes), "next_start_at": next_at if next_at < len(nodes) else None}
            if byte_size({**envelope("field", context), "item": item}) <= MAX_RESPONSE_BYTES:
                break
            if not chosen:
                raise ValueError("字段详情或单个选项路径超过返回预算")
            chosen.pop()
            if not chosen and nodes[options_start_at:options_start_at + 1]:
                raise ValueError("单个选项路径超过返回预算")
    elif options_query:
        item["options_note"] = "Jira 未提供可在本地过滤的 allowedValues"
    return detail(item, f"field:{context.kind}", context)


def link_types(query: str | None, start_at: int, max_results: int) -> dict[str, JsonValue]:
    check_page(start_at, max_results)
    items: list[JsonValue] = [selected(row, ("id", "name", "inward", "outward")) for row in _rows(get_client().issue_link_types(force=True))]
    return listing(items, "issueLinkType", None, start_at, max_results, query=query)


def permissions(context: ProjectOrIssueContext, permission_keys: list[str]) -> dict[str, JsonValue]:
    if not 1 <= len(permission_keys) <= 20 or len(set(permission_keys)) != len(permission_keys) or any(not key.strip() for key in permission_keys):
        raise ValueError("permission_keys 必须为 1–20 个非空、不重复的权限 key")
    kwargs = {"projectKey": context.project_key} if isinstance(context, ProjectContext) else {"issueKey": context.issue_key}
    raw = object_value(get_client().my_permissions(**kwargs)).get("permissions", {})
    if not isinstance(raw, dict):
        raise ValueError("权限响应结构无效")
    items: list[JsonValue] = []
    for key in permission_keys:
        value = raw.get(key)
        if not isinstance(value, dict):
            value = next((item for item in raw.values() if isinstance(item, dict) and item.get("key") == key), None)
        items.append(selected(value, ("key", "name", "havePermission")) if isinstance(value, dict) else {"key": key, "unknown": True})
    return detail(items, "mypermissions", context)
