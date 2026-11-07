# Jira Local MCP 产品需求文档

更新日期：2026-09-06

当前实现版本：3.0.0

## 1. 产品定位

Jira Local MCP 是连接通用 Agent 与 Jira Server/Data Center 的本地 MCP 服务。它让 Agent 能直接读取 Jira 信息、批量规划变更，并把所有 Jira 写操作集中到本地 Web 页面中供用户审核和提交。Agent 可以查询本地队列、解释待审核内容，并通过撤回后重新入队完成修正。

产品的核心价值是：

- 让 Agent 使用结构化工具查询事务、评论、工时、附件和 JQL 结果。
- 把多条 Jira 变更先整理为可审阅的 operation 队列，而不是边规划边写入。
- 让用户在浏览器中检查范围、内容、风险和顺序，再决定提交哪些 operation。
- 对已提交 operation 提供串行执行、状态恢复、人工对账和持久化历史。
- 通过中文 Tool、Resource 和 Prompt 描述，让 Agent 能正确理解能力及工作流。

当前产品面向本机单用户场景，使用 STDIO 接入 MCP Client，主要适配 Jira Server/Data Center。

## 2. 目标用户与典型场景

目标用户是需要借助 Agent 处理 Jira 日常工作的开发者、项目负责人、测试人员和运维人员。典型场景包括：

- 查询单个事务及其评论、工作日志和附件信息。
- 用 JQL 筛选一批事务，并规划批量字段修改或状态流转。
- 创建或克隆事务，补充描述、优先级、经办人和自定义字段。
- 为现有事务添加附件、评论、工时或事务链接。
- 在统一审核页核对系统自动分组与顺序，只提交本次确认的条目。
- 审核时让 Agent 分页定位、读取完整记录并解释；需要修正时撤回旧操作，以新 ID 重新入队并重建依赖。
- 查看实际执行结果，并在进程中断后人工确认未知结果和后续任务。

## 3. 产品工作流

```text
查询 Jira → Agent 形成变更计划 → *_append 写入本地队列
                                      ↓
                         jira_show_queue 打开审核页
                                      ↓
                    用户核对、选择、取消或明确提交
                                      ↓
                         staging → 串行 worker → Jira
                                      ↓
                     执行历史 / 未知结果人工对账
```

关键交互含义：

- Jira 只读查询立即访问 Jira；本地队列与执行状态查询只读取 SQLite。
- `*_append` 只表示 operation 已进入本地队列，不代表 Jira 已发生变化。
- 打开审核页也不代表授权或执行。
- Web 提交成功表示 operation 已进入 staging；最终结果以持久化执行记录为准。
- 创建或克隆 operation 的服务端 ID 可作为后续修改的主 `issue_key`，也可作为 `add_issuelink.target_issue_key`，使创建及依赖修改集中进入同一审核流程；生产者成功后，系统用 Jira 返回的真实 Key 原子物化暂存区和队列中的两个显式目标字段。
- 附件上传 operation 的服务端 ID 可通过显式 Jira 文本语法被同一事务的 description、comment 和 worklog 引用；上传成功后，系统以 Jira 返回的附件 ID 和最终文件名原子物化正文。

## 4. 当前已实现功能

### 4.1 MCP 能力面

当前共注册 22 个 Tool、4 个 Resource 和 4 个 Prompt。

#### 只读 Tool（5 个）

| Tool | 当前能力 |
| --- | --- |
| `jira_query_issue` | 查询单个事务，可指定返回字段；默认返回常用标准字段。 |
| `jira_query_comments` | 查询事务最新评论并限制返回条数。 |
| `jira_query_worklogs` | 查询事务最近工作日志并限制返回条数。 |
| `jira_query_attachments` | 查询包含附件 ID 和文件名的元数据，不下载附件。 |
| `jira_jql_search` | 执行分页 JQL 查询，可指定字段、偏移量和分页大小。 |

只读结果以 JSON 文本返回；存在待审核 operation 时，返回内容会附带队列提醒。

#### 写入队 Tool（11 个）

| Tool | Operation | 当前执行能力 |
| --- | --- | --- |
| `jira_issue_create_append` | `create_issue` | 创建事务；description 按文件、顶层内联值的优先级在入队时归一化为内容快照，`extra_fields` 拒绝覆盖专用字段，初始 description 禁止附件引用。 |
| `jira_issue_clone_append` | `clone_issue` | 核心字段使用类型 ID；field_overrides 补齐字段，单子任务保留 parent；递归子任务拒绝；附件异常记录主体成功的 warning 并放行新事务依赖。 |
| `jira_issue_move_append` | 无 | 立即返回不支持且未入队，内部无 move operation 或执行分支。 |
| `jira_issue_type_change_append` | `change_type` | 更新事务类型；字段映射中的 description 支持文件快照和附件 operation 引用。 |
| `jira_issue_transition_append` | `transition_issue` | 按名称匹配工作流流转；评论和字段 description 支持附件 operation 引用。 |
| `jira_field_update_append` | `update_field` | 更新标准字段；description 文件在入队时快照，并支持附件 operation 引用。 |
| `jira_customfield_update_append` | `update_custom_field` | 按 `customfield_*` 字段 ID 更新自定义字段。 |
| `jira_attachment_add_append` | `add_attachment` | 从本地绝对路径上传附件，可指定显示名称。 |
| `jira_comment_add_append` | `add_comment` | 添加评论，支持同事务附件 operation 引用。 |
| `jira_worklog_add_append` | `add_worklog` | 添加耗时、说明和可选开始时间，说明支持同事务附件 operation 引用。 |
| `jira_issuelink_add_append` | `add_issuelink` | 按 Jira 链接类型连接两个真实或待创建事务。 |

所有 `*_append` Tool 只在本地 SQLite 中创建 operation，并返回 operation ID、基础风险和“尚未执行”状态。

#### 审核与历史 Tool（2 个）

- `jira_show_queue`：按需启动本地 Web 服务并打开默认浏览器；MCP 返回值只报告打开状态，不返回 bootstrap URL 或浏览器会话。
- `jira_get_execution_log`：读取最近的持久化执行与人工对账记录，不修改 Jira。

#### 本地查询与撤回 Tool（4 个，已实现）

- `jira_list_queue(page=1, page_size=50)`：按 Web 分组与组内顺序展开待审核概览，选取标识、类型、目标、描述、风险及创建时间，不返回 payload。
- `jira_get_queue_operations(operation_ids)`：读取 1–200 个合法且不重复的 ID，按请求顺序返回完整公开记录及 revisions；不在当前队列中的 ID 返回到 missing_operation_ids，不推断其状态。
- `jira_list_execution_state(page=1, page_size=50)`：按 stage_order 返回 staged、executing、indeterminate、held 的概览及执行元数据，不返回 payload；完成结果通过现有执行日志查询。
- `jira_cancel_queue_operations(operation_ids, expected_queue_revision)`：原子撤回 1–1000 个 queued 操作，范围必须显式覆盖全部直接和间接依赖。

两个清单每页最多 200 条，返回 items、total、page、page_size 和 revisions；每次调用是独立的当前快照，相关 revision 变化时不能拼接为同一快照。复用 QueryService 和现有公开模型序列化，概览只选字段、详情不改写或截断正文，仍执行敏感信息脱敏。记录、依赖状态和 revisions 在一个显式 SQLite 读事务中读取，不引入跨请求缓存。

撤回不接受级联确认 flag。Web 与 MCP 共用依赖闭包校验和取消事务；范围不足时返回 dependency_scope_incomplete、affected_operation_ids 和 missing_operation_ids。版本冲突、范围不足、未知或已离开队列的 ID 均整批拒绝，不写取消事件、不更新 revision。Web 保留人工确认交互，确认后提交完整 ID 范围并沿用首次请求 revision。

局部查询不再先构建 Web 全量状态：队列概览全局排序后仅组装当前页，详情按 ID 批量读取并按请求顺序返回，活动执行状态按 stage_order 在 SQL 中分页。每个查询的记录、依赖事实、总数与 revisions 均来自同一读事务；Web 全量状态复用相同公开记录转换，不拼接跨事务结果。

业务 Tool 由 `core/mcp_tools/` 子包统一公开：`jira_query` 负责 Jira 只读查询，`jira_enqueue` 负责入队，`queue_tool` 负责本地查询、撤回及执行日志。server 通过函数引用统一注册，继续承担审核页面入口、Prompt、Resource 和服务生命周期。

Agent 可通过取消后重新入队修正待审核内容，两步不原子，重新入队失败时旧操作已撤回；依赖链须使用新 ID 重建，不编辑记录或替换依赖。MCP 不开放提交、执行、恢复、编辑或清空 Tool；本次变更不修改数据库 schema。

#### Resource 与 Prompt

当前 Resource：

- `skill://jira-operation-rule`
- `skill://jira-field-guide`
- `skill://risk-classification`
- `config://service-info`

当前 Prompt：

- `operation_queue_workflow`
- `create_with_followups_workflow`
- `safe_query_guide`
- `no_delete_checklist`

这些能力提供中文工作流、字段和风险说明，不直接触发 Jira 写入。

### 4.2 Operation 队列

当前队列支持：

- operation 入队时生成服务端 ID，并保存类型、顶层 `issue_key`、仅链接使用的顶层 `target_issue_key`、不可变 operation 专属 payload、描述、来源、风险、创建时间和 attempt。
- 按 issue 自动聚类标准字段、自定义字段、工作流、附件、评论和 worklog operation。
- 创建/克隆及以主 Key 引用它们的后续修改组成待创建事务分组，生产者固定在首位；真实 Key 解析后，依赖项在下一次读取时按普通分组规则重新分类。链接的第二目标只参与依赖，不改变主分组。
- 移动、类型变更和事务链接在目标已解析后以零散 operation 展示。
- Jira issue 分组优先并按事务 Key 自然排序；待创建事务分组随后按生产者创建时间排序；零散项最后按创建时间和 operation ID 排序。
- 组内固定按生产者、标准字段、自定义字段、工作流、附件、评论、worklog、其他类别，再按创建时间和 operation ID 排序。
- 待审队列不持久化位置、布局或人工锚点，也不提供人工排序控件/API；提交后才把自动展示顺序经稳定拓扑排序固化为 staging `stage_order`。
- 队列修改使用 revision 乐观并发校验；过期页面会收到冲突响应并刷新。
- 取消与提交均支持多选，并以事务方式保证整批成功或整批不变。
- 提交选择必须包含仍在队列中的依赖生产者，服务端按依赖拓扑稳定生成 stage order；页面会可见地联动勾选生产者，但后端不会暗中扩大授权。
- 事务目标引用和附件正文引用由两个对称的领域规则模块解析，通过统一依赖门面参与状态查询、闭包、拓扑排序和失败传播。
- 取消范围缺少下游依赖时，本地明细弹窗按后端顺序展示全部受影响操作，标记原选项和新增依赖，列出完整 ID、类型、说明及关联/目标事务；用户一次确认即可取消全部，无需补勾。长列表滚动、长文本换行，支持窄屏、返回及 Escape。
- 弹窗使用首次请求的展示快照和 revision，轮询不替换确认内容；版本冲突后刷新并重新确认，不自动重试。展示记录缺失时停止预览并刷新，超过单次 1000 项时禁用确认，会话失效时关闭弹窗。同一时间仅允许一个取消流程，网络失败后恢复入口供用户核实状态。

当一次提交中同一事务包含 5 个或更多标准/自定义字段 operation 时，这些条目的提交风险会升级为高风险，并要求二次确认。

### 4.3 本地审核页

审核页目前包含四个区域：

- 实时审核队列：查看 operation 详情与自动分组顺序，支持勾选、全选、取消和提交，不支持人工排序。
- 暂存与阻塞：展示 `staged`、`held` 和 `indeterminate` 状态及可用人工处置。
- 执行中：展示当前 `executing` operation。
- 执行历史：按本地日期筛选并分页查看结果。

页面通过短时浏览器会话访问，定时轮询状态，并在刷新数据时保留仍然有效的选择和展开状态。每个凭据固定有效 900 秒；有效的页面、API 或 ETag `304` 请求在剩余不超过 60 秒时换发新凭据，旧 ID 只保留到原到期时间。停止请求后所有凭据最多再存活 15 分钟，新进程不会继承旧会话。首次收到 `403` 后，前端停止轮询、禁用审核操作并明确提示会话失效。高风险选择在提交前要求用户再次确认；依赖失败且尚未授权的队列项会保留展示、禁用提交并只允许取消。

### 4.4 执行与恢复

后台 worker 已实现：

- 按持久化 `stage_order` 串行领取 operation。
- 使用进程内锁、跨进程文件锁和 SQLite 原子 claim，避免并发 worker 重复领取。
- 对成功和失败结果写入 execution record，包含 attempt、时间、耗时、摘要、错误、新建事务 key，以及嵌套的附件上传结果。
- 创建/克隆成功时，在同一 SQLite 结果事务中批量更新 queue/staging 中匹配的主 `issue_key` 和链接 `target_issue_key`；执行依赖项时只向 Jira 传递两个目标都已物化的 operation。
- 附件上传成功时直接读取 SDK 返回的附件 ID 与 Jira 最终文件名；响应缺少文件名时仅按附件 ID 回查元数据。系统在同一结果事务中把支持路径里的 `[^{{attachment:op-…}}]`、`!{{attachment:op-…}}!` 分别替换为 Jira 原生文件名引用。
- 生产者明确失败或被放弃时，已暂存依赖以 `dependency_failed` 终止并明确记录“未调用 Jira”，无关 operation 继续执行。
- 普通失败作为终态记录，不在后台静默重复调用 Jira。
- 若进程中断时 operation 处于 `executing`，启动恢复会将其标记为 `indeterminate`，并把后续条目标记为 `held`。
- 用户可把 indeterminate 标记为“已生效”“未生效”或“放弃”；创建/克隆标记为“已生效”时必须补录真实 Key，附件上传标记为“已生效”时必须同时补录附件 ID 和最终文件名；“未生效”会复用原 operation ID、增加 attempt 并重新入队。
- 用户可将 held 条目恢复到 staging、退回审核队列或放弃。

### 4.5 持久化与可观测性

当前运行数据位于 `data/`：

- `operations.sqlite3`：队列、队列事件、staging、执行记录和 queue/execution/history revisions。
- `mcp_logs.sqlite3`：Tool 调用、Resource 读取和 Prompt 获取的完整 MCP 业务请求/响应及时间信息。
- `jira_execution.lock`：跨进程 Jira 写入互斥锁。

queue、staging 和 history 都保存自包含的 operation 快照：两个目标 Key 位于独立列，`payload_json` 保存 operation 专属参数和尚未物化的显式附件引用。事务目标与附件正文采用独立的领域解析模块，不使用通用字段 resolver。已确认的附件 ID 与最终文件名保存在 `AttachmentIdentity` 中，统一 `AttachmentResult` 表达每个附件的处理状态；执行记录通过 `attachment_results_json` 保存结果数组，无附件时为 `[]`；公共输出统一使用 `dependency_states` 映射表达依赖 operation ID 及生命周期状态（未知为 `unknown`），映射键顺序不代表执行顺序；物化关系分别记录为 `issue_key_resolved` 与 `attachment_resolved` 事件。

SQLite 使用 WAL、`synchronous=FULL`、外键、busy timeout 和 schema version。业务队列事务与 MCP 日志相互独立，日志记录失败不会改变 MCP 业务结果。
开发阶段任何数据库结构变更都必须显式提高对应 schema 版本，清除该库旧数据后从零构建，不提供旧库迁移或兼容层。3.0.0 使用 operations schema 3；旧库无论是否有业务数据，只要版本或结构标识不符，或已有结构但未标记版本，都拒绝打开并提示清理。使用者须先停止访问该库的所有 MCP、Web 和 worker 进程，再仅清除配置指向的 SQLite 文件及精确匹配的 `-wal`、`-shm` sidecar，重启后创建新库。MCP 日志库独立，结构未变时无需清除。

### 4.6 配置与运行

当前支持：

- Python 3.10+。
- Jira Server/Data Center。
- Personal Access Token 或 Basic Auth，由项目根目录 `.env` 统一管理；不使用进程环境变量覆盖，不回退读取旧 YAML。
- Windows/macOS 安装入口及 Linux Python 安装脚本，创建或复用 `.venv`，只读检查认证并生成本机 `MCP_SETUP.md`。
- 已有 YAML 仅在 `.env` 不存在且用户同意时导入；保留原配置和 `data/`，本次配置升级不改变数据库结构。
- 可配置 TLS 证书校验和 Jira 请求超时。
- 可配置 Web 起始端口；端口占用时在后续 99 个端口内自动寻找可用项。
- 可配置本地 `.txt` 描述文件大小上限，默认 256 KiB。
- STDIO MCP 启动方式；Web 服务按需启动，无独立前端构建步骤。

## 5. 当前产品范围说明

以下内容用于说明 3.0.0 的实际行为，不代表永久产品边界：

- 当前部署模型是同一台机器、同一用户、一个 MCP 服务连接一个 Jira 配置。
- 当前 operation 只覆盖第 4.1 节列出的类型；服务端按现有 allowlist 接受操作。
- 本地附件路径在实际执行时读取；description 文件在入队时读取并保存为字符串快照，源路径不会进入 operation。
- 移动工具保持签名并直接拒绝；无内部可入队或执行 move 分支。
- 克隆支持 field_overrides、单个子任务与附件复制明细；递归克隆子任务不支持。
- 数据库仅初始化全新库，不兼容旧库必须清除后重建，不提供迁移或兼容层。

## 6. 后续产品规划

以下为基于当前架构的演进方向，按产品价值排序，不表示已经交付，也不承诺具体版本日期。

### 6.1 近期：补齐 Jira 操作闭环

- 按需求扩展组、Sprint、Assets 与选配目录；验证特定实例的字段用户过滤语义。
- 按实际需求评估递归子任务克隆；当前不纳入范围。
- 为移动事务提供与目标 Jira 版本兼容的实现或明确的引导式替代流程。
- 增加 operation 执行前的规范化 payload 预览，让审核内容更接近最终 Jira 请求。
- 将附件保存为可核对的内容快照或哈希绑定，并在页面展示内容与大小信息。
- 建立多版本 Jira Server/Data Center 集成测试矩阵，覆盖权限、工作流、字段配置和 Jira 自动化场景。

### 6.2 中期：提升批量审核与日常效率

- 队列搜索、筛选、按风险或 operation 类型聚合，以及更强的批量选择能力。
- 保存和复用常见 JQL、字段映射、operation 模板与审核视图。
- 提供提交前差异视图、影响范围摘要、执行进度与失败定位。
- 仅在 operation 契约和审核方式都明确时，增加新的显式、可审核目标引用；不恢复通用 resolver 注册与分派框架。
- 增加可配置的数据保留、历史导出、运行诊断和数据库清理重建工具。
- 在具备明确幂等语义时，为可安全重试的 operation 设计可配置重试策略；未知结果仍进入可观察的人工对账流程。

### 6.3 长期：扩展部署与协作模式

- 支持多个 Jira 实例和 Jira Cloud 适配层。
- 支持远程审核、多用户、角色权限、通知和多级审批。
- 提供更适合长期运行的服务化部署、健康检查、备份恢复和运维界面。
- 建立可扩展 operation/connector 机制，逐步纳入更多 Jira 管理动作；对影响更大的动作配套更细粒度预览与授权流程。
- 在引入多用户或多租户模式时，扩展身份、租户隔离、审计导出和策略配置能力。

## 7. 当前版本验收基线

3.0.0 的功能验收以代码和自动化测试为准，至少应满足：

- FastMCP 实际注册 22 个 Tool、4 个 Resource 和 4 个 Prompt，且能力描述完整、中文、可被 Agent 理解。
- 5 个只读 Tool 只查询 Jira；11 个 `*_append` Tool 只创建本地 operation。
- 审核页可以读取自动分组排序的实时状态、取消或提交选择，并处理高风险二次确认；不存在人工排序入口。
- 创建/克隆 operation ID 可被全部修改 Tool 引用；依赖闭包、拓扑执行、成功 Key 解析、部分提交和级联取消均符合契约。
- 附件上传 operation ID 可被同一事务的 description、comment 和 worklog 显式引用；Jira 重命名附件后，正文只使用该次上传返回的最终文件名，ID 与文件名作为单个结果保存和恢复。
- queue revision、ETag、严格请求模型和原子队列变更均按接口契约工作。
- 单凭据 15 分钟有效，活动页可滚动换发；页面、API、304、旧 ID 原时限失效、停止请求失效和进程重启失效均符合会话契约。
- worker 在单线程、多线程和多进程场景下不重复领取同一 operation。
- 成功、失败、`dependency_failed`、中断恢复、indeterminate 对账和 held 处置均留下可查询记录；未解析 operation ID 不会进入 Jira executor。
- MCP 业务交换日志与 operation 数据库独立工作。
- 页面在真实 Chromium 中能安全、稳定地展示来自 Jira 和 operation 的任意业务文本。
- `conda run -n jira-dev python -m pytest tests/ -q` 完整通过。

## 8. 文档事实来源

- 当前已实现行为：以代码与测试为准。
- 产品定位和后续方向：以本 PRD 为准。
- 安装、配置和 MCP Client 接入：以 `README.md`、`.env.example` 与安装生成的 `MCP_SETUP.md` 为准。
- Agent 开发约束与修改检查项：以 `AGENTS.md` 为准。

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
