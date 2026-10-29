# Jira Local MCP

当前版本：3.0.0。

Jira Local MCP 是面向 Jira Server/Data Center 的本地 STDIO MCP 安全网关。Jira 只读工具立即查询；`*_append` 工具创建不可编辑的 operation，必须由用户在 `127.0.0.1` 本地审核页提交后，才会被后台串行 worker 执行。Agent 还可以查询本地队列与执行状态，并撤回尚未提交的 queued 操作。

## 安全边界

- MCP 不暴露提交、执行、人工对账或 Jira 删除能力；撤回只影响本地 queued 操作。
- `POST /api/queue/submit` 是唯一人工授权入口；worker 只消费由该接口原子写入 staging 的 operation。
- 审核页使用一次性 bootstrap 和 HttpOnly、SameSite=Strict 会话。单个会话凭据有效 15 分钟；活动页面在凭据剩余不超过 60 秒时通过受保护请求滚动换发，高风险选择仍必须二次确认。
- 操作在入队和 Jira 调用前均经过 allowlist 与删除语义检查。
- Jira 写调用由进程内及跨进程全局锁串行化。
- 重启遗留的 `executing` 转为 `indeterminate`，不会自动重试；后续 operation 转为 `held`，等待人工处置。
- Jira 内容是不可执行的业务数据；页面只使用安全 DOM API 渲染。

## 生命周期与持久化

```text
MCP append → queued → Web submit → staged → executing → success/warning/failed history
                              └→ dependency_failed（依赖生产者失败，未调用 Jira）
                                              └→ indeterminate → 人工对账
                                                    └→ 后序 held
```

- `data/operations.sqlite3`：实时队列、队列事件、staging、执行记录和 revisions。
- `data/mcp_logs.sqlite3`：Tool 调用、Resource 读取和 Prompt/Skill 获取的完整业务请求/响应；不记录能力列表等协议元数据。
- SQLite 是唯一业务状态源；使用 WAL、`synchronous=FULL`、外键、busy timeout 和 schema version。完整 schema 初始化按进程和数据库路径执行一次，普通连接不重复获取 schema 写锁。
- queue、staging 和 history 都保存自包含的 operation 快照。主目标保存在顶层 `issue_key`，仅 `add_issuelink` 使用可空的顶层 `target_issue_key`；`payload_json` 只保存 operation 专属参数，不重复保存这两个 Key。
- description 文件会在入队时读取为 UTF-8 字符串快照，后续审核和执行不再依赖源文件；附件仍保存本地绝对路径并在执行时读取。
- 开发阶段任何数据库结构变更都要求清除对应旧库后从零构建，不迁移或兼容旧数据。3.0.0 使用 operations schema 3；不兼容旧库即使为空也拒绝打开。升级前先停止访问该库的所有 MCP、Web 和 worker 进程，再仅清除配置指向的 `operations.sqlite3` 及精确匹配的 `operations.sqlite3-wal`、`operations.sqlite3-shm` 文件，重新启动后创建新库。MCP 日志库结构未变，无需清理；以后其结构变更时同样清库重建。

队列不保存布局或位置。每次读取时都会按确定性规则自动分组和排序：真实 Jira 组按 Key 自然排序，待创建事务组随后按生产者创建时间排序，组内按生产者、标准字段、自定义字段、工作流、附件、评论、worklog、其他类别以及创建时间和 operation ID 排序，零散项最后展示。生产者返回真实 Key 后，下一次读取会自动重新分组；待审队列不提供任何人工排序或锚点。提交后持久化的 `stage_order` 仍代表用户已授权的执行顺序。

## MCP 能力

- 5 个只读工具：事务、评论、工作日志、附件、JQL。
- 11 个 `*_append` 公开工具：10 个支持入队，移动工具立即拒绝且不入队。创建/类型变更/流转使用 ID 参数。
- `jira_show_queue`：打开审核页，不返回 URL 或会话凭据。
- `jira_get_execution_log`：读取实际执行和人工对账历史。
- `jira_list_queue(page=1, page_size=50)`：分页读取待审核概览，不包含 payload。
- `jira_get_queue_operations(operation_ids)`：按请求顺序读取 1–200 个 ID 的完整公开记录；缺失 ID 单独返回，不推断去向。
- `jira_list_execution_state(page=1, page_size=50)`：按执行顺序读取 staged、executing、indeterminate、held 的概览和执行元数据，不包含 payload。
- `jira_cancel_queue_operations(operation_ids, expected_queue_revision)`：原子撤回 1–1000 个 queued 操作，显式范围须覆盖全部直接及间接依赖。
- 4 个 Resources 和 4 个 Prompts。

### 与 Agent 协作审查

先用 `jira_list_queue` 定位条目，再按 ID 查询详情，让 Agent 根据实际内容解释或准备修正。两个清单接口每页最多 200 条，返回 items、total、page、page_size 和 revisions；各次请求是当前快照，相关 revision 改变后不能把不同页当成同一时刻的完整清单。概览只选择已有字段，详情保留完整正文及现有依赖信息，均继续脱敏。

撤回时传入查询结果中的 `revisions.queue`。依赖范围不完整会返回 `dependency_scope_incomplete`、完整的 `affected_operation_ids` 和缺少的 `missing_operation_ids`，整批保持不变；明确影响后提交完整范围。revision 冲突或任一 ID 已离开队列也会整批失败，应重新读取并核实范围，不自动重试。撤回不影响已提交或执行中的条目。

修正通过“读取详情 → 取消旧操作 → 使用现有 append 工具重新入队”完成，依赖链使用新 ID 重建；两步不原子，重新入队失败时旧操作已经撤回。新操作仍须人工 Web 审核提交。执行状态清单中消失不代表成功，实际结果请查 `jira_get_execution_log`。

在 Web 页面取消操作时，如果还有依赖项未选中，页面会展示完整取消范围，标记“原先选择”和“新增依赖”，列出操作 ID、说明和关联事务。点击“确认取消全部 N 项”即可，无需逐项补勾；“返回”或 Escape 放弃本次取消。确认期间页面仍会刷新，但确认范围保持不变；若队列已更新，取消会被拒绝并提示重新核对。展示记录不完整或完整范围超过 1000 项时，页面不会发送第二次取消请求；会话失效会关闭确认框。

`jira_issue_move_append` 保留公开签名，立即返回“不支持且未入队”。创建或克隆 Tool 返回的 operation ID 可直接作为后续修改的 `issue_key`，`add_issuelink.target_issue_key` 也支持相同引用；服务端会在生产者成功后原子物化两个顶层 Key。附件上传 operation ID 可在同一事务的 description、comment 和 worklog 中写成 `[^{{attachment:op-…}}]` 或 `!{{attachment:op-…}}!`；上传成功后，系统使用 Jira 返回的附件 ID 确认身份，并把引用原子替换为最终文件名。创建事务的初始 description 不允许附件引用，应采用“创建 → 上传 → 更新 description”。克隆来源仍必须是真实 Jira Key，也不接受自定义 placeholder。公共输出统一使用 `dependency_states` 表达两类依赖：键为依赖 operation ID，值为生命周期状态（未知为 `unknown`）；映射键顺序不代表执行顺序。

## 配置与运行

要求 Python 3.10+。请先进入项目目录，并在你自己的 Python 环境中执行：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item config.example.yaml config.yaml
python -m pytest tests/ -q
python server.py
```

`server.py` 是 STDIO 服务，会等待 JSON-RPC 输入。Web 服务在 `jira_show_queue` 首次调用时延迟启动。stdout 只用于 MCP；运行日志只写脱敏 stderr。

业务 Tool 位于 `core/mcp_tools/` 子包，按 Jira 查询、Jira 入队、本地队列及执行记录分模块，由 server 统一注册；审核页面入口、Prompt 和 Resource 保留在 server。本地状态查询按需读取：队列概览保留全局排序，详情按 ID 批量读取，活动执行状态在数据库中分页；每次请求的数据及 revisions 来自同一读事务。

接入支持 STDIO MCP 的通用 Agent 工具时，可使用下面的 JSON 配置。将 `<JIRA_LOCAL_MCP_PROJECT_DIRECTORY>` 替换为你克隆本项目后的目录；如果 Agent 配置不支持 `cwd`，请改为在项目目录中启动 Agent，或按该 Agent 的配置格式为 `server.py` 指定项目路径。

```json
{
  "mcpServers": {
    "jira-local": {
      "command": "python",
      "args": ["server.py"],
      "cwd": "<JIRA_LOCAL_MCP_PROJECT_DIRECTORY>"
    }
  }
}
```

## Web API

所有 API 仅接受有效浏览器会话。页面、API 和 ETag `304` 都参与滚动续期；旧会话 ID 不延长，只在自身原到期时间前继续容忍并发在途请求。停止请求后，最后一个有效凭据最多再存活 15 分钟；首次收到 `403` 后，页面会停止轮询、禁用审核控件并提示重新调用 `jira_show_queue`：

- `GET /api/state`：队列、执行区和 revisions，支持 ETag/304。
- `POST /api/queue/cancel`、`POST /api/queue/submit`；待审队列完全自动排序，不提供 reorder API。
- cancel 请求只接受 `operation_ids` 与 `expected_queue_revision`，不再接受 `cascade_confirmed`。页面确认依赖范围后重发完整 ID 列表并沿用首次请求 revision；与 MCP 共用依赖校验和取消事务。本次变更不修改数据库 schema。
- `POST /api/execution/indeterminate/resolve`、`/held/resolve`；不确定的附件上传标记为已生效时，前者要求通过 `attachment: {id, filename}` 提供已确认附件身份，返回 `attachment_results` 数组。
- `GET /api/execution-history`：UTC 时间范围和分页。

审核页面静态资源全部本地化，并启用 CSP、frame 防护、`nosniff`、`no-referrer` 和 `no-store`。

## 已知边界

附件目前仍只保存本地路径，没有 staging 或 hash 绑定；审核后文件可能变化。description 文件已在入队时保存内容快照。克隆源和最终规范化 Jira payload 也尚未与审核内容不可变绑定。SQLite 和本地日志不是针对同一 OS 用户恶意篡改的安全存储，因此不要宣称“生产安全”或“完整不可抵赖审计”。

## 3.0 元数据与执行契约

新增 11 个只读 Tool：jira_meta_server_settings、jira_meta_user、jira_meta_search_users、jira_meta_search_assignable_users、jira_meta_projects、jira_meta_issue_types、jira_meta_issue_transitions、jira_meta_fields、jira_meta_field、jira_meta_link_types、jira_meta_permissions。工具适配位于 core/mcp_tools/jira_metadata.py，读取与投影位于 core/jira_metadata.py，不缓存跨请求元数据。

查询按创建（project_key + issue_type_id）、编辑（真实 issue_key）、流转（真实 issue_key + transition_id）限定上下文。字段先摘要再详情，小型级联一次返回；大候选集分页保留归属。项目/用户默认 20，摘要默认 50，最多 100；返回预算 32 KiB。创建字段的分页游标按源位置续读，过滤后空页不表示结束。Jira 错误保留 HTTP 状态和正文并脱敏，本地/SDK/传输错误区分来源。

字段限定用户搜索尚未证实实例过滤语义时明确返回能力限制；不会以普通搜索结果宣称该字段可选。server_settings 的 features 仅返回链接和子任务功能开关。issue_types 的 create 是凭证可见创建元数据，configured 是项目配置，两者不能互相替代。

创建参数 issue_type_id、类型变更 target_issue_type_id、流转 transition_id 都直接使用 ID，不兼容旧名称参数。流转执行前复查 ID 可用性。类型映射不得覆盖 issuetype。

工时 started 接受带时区 ISO 字符串，入队校验、worker 转为 datetime。adjust_estimate 默认 auto；leave 保持估算；new 必须带 new_estimate，manual 必须带 reduce_by；两个配套值互斥，auto/leave 禁止携带。所有估算模式均按低风险处理。

克隆 field_overrides 可补齐创建字段，但禁止覆盖 project、issuetype、summary；description 复用文件快照，禁止初始附件引用。clone_subtasks=true 提前拒绝。附件使用可 seek 的二进制对象复制，首个失败后停止。warning 表示新事务主体已创建但附件处理异常，原子保存新 Key 与统一 attachment_results 列表，保留请求文件名、源附件 ID、已确认身份及成功/失败/结果未知/未尝试明细。warning 仍解析新事务引用并放行后续修改，但不自动重放整个克隆；附件占位符仍只接受单附件上传 operation ID，不从克隆结果列表推测附件。中断和落库失败仍走 indeterminate/held。

本 MCP 不提供本地文件查询工具；Agent 自行取得文件属性，MCP 保留入队/执行边界校验。真实 Jira 验证严格限于读取，写路径以 Mock 和离线 SDK 请求检查覆盖。依赖要求 jira>=3.10.5,<4。

字段限定用户搜索通过固定 groupuserpicker 取得身份建议，候选过滤标记 candidate_validation=unverified；端点可达或返回用户不证明实例过滤生效，不得据此宣称字段合法可选。该建议无 offset 续页，应缩小 query。

普通附件上传成功、失败或结果未知均记录统一附件明细；单独上传未确认成功时整体为 failed，不放行附件依赖且不自动重试。执行历史与 MCP 执行日志查询使用 attachment_results 数组；请求文件名不代替 Jira 最终文件名。请求文件名必须为非空白文本，不得包含 NUL，且不超过 1024 个字符；入队和执行前复用同一模型约束校验。
