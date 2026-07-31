# JIRA 字段手册

> 本文档可由用户自定义补充，例如填入本 JIRA 实例的自定义字段 ID 与名称对应关系。

## 常用标准字段（写入格式）
| 字段 | 键名 | 写入示例 |
|---|---|---|
| 标题 | `summary` | `"summary": "新标题"` |
| 描述 | `description` | `"description": "文本"` 或 `"description": {"file": "C:\\abs\\path.txt"}` |
| 优先级 | `priority` | `"priority": {"name": "High"}` |
| 经办人 | `assignee` | `"assignee": {"name": "username"}`（Server/DC 用 name） |
| 报告人 | `reporter` | `"reporter": {"name": "username"}` |
| 标签 | `labels` | `"labels": ["backend", "urgent"]` |
| 事务类型 | `issuetype` | `"issuetype": {"name": "Bug"}` |
| 修复版本 | `fixVersions` | `"fixVersions": [{"name": "1.2.0"}]` |
| 组件 | `components` | `"components": [{"name": "API"}]` |

## 自定义字段
- 自定义字段键名形如 `customfield_10001`。
- 通过 `jira_customfield_update_append` 修改，key 用字段 ID。
- 不同字段类型写入格式不同：
  - 文本：`"customfield_10001": "值"`
  - 单选：`"customfield_10002": {"value": "选项"}`
  - 多选：`"customfield_10003": [{"value": "A"}, {"value": "B"}]`
  - 用户：`"customfield_10004": {"name": "username"}`

## 本实例自定义字段对照表
<!-- 用户可在此维护，例如：
| 名称 | ID | 类型 |
|---|---|---|
| 冲刺 | customfield_10020 | 数组 |
-->
（尚未配置，请根据实际 JIRA 实例补充。）

## Server / Data Center 注意事项
- 用户标识用 `name`（用户名），而非 Cloud 的 `accountId`。
- **移动事务（move）REST 不支持**，请在网页端手动操作。
- 子任务 ↔ 标准任务的跨层级类型转换 REST 通常不支持。
