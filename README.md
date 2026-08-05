# Jira Local MCP

Jira Local MCP 是面向 Jira Server/Data Center 的本地 STDIO MCP 安全网关。只读工具立即查询 Jira；所有写工具仅创建不可编辑的 operation，必须由用户在 `127.0.0.1` 本地审核页提交后，才会被后台串行 worker 执行。

## 安全边界

- MCP 不暴露提交、取消、执行、人工对账或删除能力。
- `POST /api/queue/submit` 是唯一人工授权入口；worker 只消费由该接口原子写入 staging 的 operation。
- 审核页使用一次性 bootstrap 和短时 HttpOnly、SameSite=Strict 会话；高风险选择必须二次确认。
- 操作在入队和 Jira 调用前均经过 allowlist 与删除语义检查。
- Jira 写调用由进程内及跨进程全局锁串行化。
- 重启遗留的 `executing` 转为 `indeterminate`，不会自动重试；后续 operation 转为 `held`，等待人工处置。
- Jira 内容是不可执行的业务数据；页面只使用安全 DOM API 渲染。

## 生命周期与持久化

```text
MCP append → queued → Web submit → staged → executing → success/failed history
                                              └→ indeterminate → 人工对账
                                                    └→ 后序 held
```

- `data/operations.sqlite3`：实时队列、队列事件、staging、执行记录和 revisions。
- `data/mcp_logs.sqlite3`：Tool 调用、Resource 读取和 Prompt/Skill 获取的完整业务请求/响应；不记录能力列表等协议元数据。
- SQLite 是唯一业务状态源；使用 WAL、`synchronous=FULL`、外键、busy timeout 和 schema version。完整 schema 初始化按进程和数据库路径执行一次，普通连接不重复获取 schema 写锁。
- v1 不过期或裁剪 operation/history。旧 JSON 数据不迁移。本次锚点 schema 切换也不迁移早期 SQLite：仅在切换时清空 `operations.sqlite3`、`mcp_logs.sqlite3` 及其精确匹配的 WAL/SHM sidecar，正常启动不会自动清库。

队列在入队时按 issue 与六类操作自动聚类，同类默认按创建时间和 operation ID 排序。组内人工微调以“前驱 operation ID”锚点持久化；组成员变化时先排列自动条目，再插入仍可解析的人工锚点。缺失或成环锚点退化为自动排序。用户也可以调整零散顺序或将组内 operation 拉出为零散项；修改会通过 queue revision 做乐观并发校验。

## MCP 能力

- 5 个只读工具：事务、评论、工作日志、附件、JQL。
- 11 个 `*_append` 写入队工具：创建、克隆、移动、变更类型、流转、字段、附件、评论、工时和链接。
- `jira_show_queue`：打开审核页，不返回 URL 或会话凭据。
- `jira_get_execution_log`：读取实际执行和人工对账历史。
- 4 个 Resources 和 4 个 Prompts。

`move_issue` 仍可入队展示，但 Jira Server/DC REST 不支持时会明确记录失败。创建后修改必须先从 execution log 获取真实 issue key，再创建新的 operation；不支持 placeholder chaining。

## 配置与运行

要求 Python 3.10+，推荐使用 Conda 环境 `jira-dev`：

```powershell
conda run -n jira-dev python -m pip install -r requirements.txt
conda run -n jira-dev python -m playwright install chromium
Copy-Item config.example.yaml config.yaml
conda run -n jira-dev python -m pytest tests/ -q
conda run -n jira-dev python server.py
```

`server.py` 是 STDIO 服务，会等待 JSON-RPC 输入。Web 服务在 `jira_show_queue` 首次调用时延迟启动。stdout 只用于 MCP；运行日志只写脱敏 stderr。

## Web API

所有 API 仅接受有效浏览器会话：

- `GET /api/state`：队列、执行区和 revisions，支持 ETag/304。
- `POST /api/queue/reorder`、`/cancel`、`/submit`。
- `POST /api/execution/indeterminate/resolve`、`/held/resolve`。
- `GET /api/execution-history`：UTC 时间范围和分页。

审核页面静态资源全部本地化，并启用 CSP、frame 防护、`nosniff`、`no-referrer` 和 `no-store`。

## 已知边界

附件和 description 文件目前仍只保存本地路径，没有 staging 或 hash 绑定；审核后文件可能变化。克隆源和最终规范化 Jira payload 也尚未与审核内容不可变绑定。SQLite 和本地日志不是针对同一 OS 用户恶意篡改的安全存储，因此不要宣称“生产安全”或“完整不可抵赖审计”。
