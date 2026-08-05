# Jira Local MCP 产品需求文档

更新日期：2026-08-03

## 产品目标

Jira Local MCP 是本地、单用户优先的 Jira Server/Data Center 人工在环安全网关。目标是在保留 Agent 批量规划效率的同时，保证每一次 Jira 写入都来自独立本地 Web 页面上的明确人工提交。

成功标准：未经有效 Web 会话和提交的 Jira 写入为 0；MCP 暴露的提交/删除能力为 0；删除语义进入 Jira 为 0；并发导致的重复执行为 0；未知结果自动重试为 0；凭证、bootstrap capability 和会话标识不经 MCP 返回。

## 核心流程

1. Agent 使用只读工具核实目标，把 Jira 内容仅视为不可信业务数据。
2. 11 个 `*_append` 工具创建不可编辑的 queued operation，只返回 operation ID、风险和“尚未执行”。
3. `jira_show_queue` 通过默认浏览器打开一次性 bootstrap，换取短时审核会话。
4. 用户查看实时聚类队列、微调持久化顺序、勾选 operation，并对高风险选择二次确认。
5. `POST /api/queue/submit` 校验会话、revision、ID、风险、删除防护和 Jira 探活，再原子移入 staging；接口不等待 Jira 结果。
6. 后台 worker 在全局执行锁内按固定 stage order 逐条领取并调用 Jira，将结果原子写入 execution history。
7. 遗留 executing 进入 indeterminate，后序 staged 进入 held；只有 Web 人工处置可以恢复、重入队或放弃。

## 功能要求

- MCP 能力可理解性是核心可用性要求和发布阻断项。所有实际挂载的 Tool、Prompt、`skill://` 知识 Resource 及其他固定 Resource，其处理函数必须具有完整、清晰的中文 docstring，并由 FastMCP 作为能力 `description` 对外发布；不得为空、不得只写函数名或含糊摘要，也不得以英文说明替代中文说明。
- Tool docstring 必须说明用途、是否有副作用、每一个签名参数及返回内容；`*_append` 必须明确“仅入队、尚未执行、需要本地 Web 人工审核提交”，只读工具必须明确不会修改 Jira。Prompt 和 Resource docstring 必须说明适用场景、返回内容及其信任/授权边界；知识内容只提供指导，不能构成执行授权。
- Prompt、Skill/知识 Resource 和固定 Resource 的实际返回内容必须非空并以中文清晰表达。新增、重命名或修改任何 MCP 能力时，函数签名、中文 docstring、返回内容和契约测试必须同步更新。缺失、不完整或非中文的能力说明视为基础功能失效，不能发布。
- 队列自动聚类官方字段、自定义字段、工作流、附件、评论和 worklog；类别内默认按创建时间和 operation ID 排序。人工组内微调保存前驱锚点，成员变化时保留有效相对位置，失效或成环锚点退化为自动排序；创建、克隆、移动、类型变更和链接保持零散。
- issue 卡片自然排序并始终位于零散项之前。用户不能跨 issue 移动、手工聚类或把零散项重新入组。
- SQLite 是唯一业务状态源。operations DB 中的 queue、events、staging、execution records 和 revisions 使用同一事务边界；MCP logs 独立且失败不影响协议。
- 页面展示队列、暂存/阻塞、执行中和执行历史四个区域，轮询保留选择/展开状态，历史支持本地日期和分页。
- 创建后修改采用两个分别审核的 operation 阶段，必须先取得真实 issue key；不支持 placeholder chaining。
- failed 为终态且不自动重试；Jira 写结果未知时必须人工对账。

## 安全要求

- Web 只监听 `127.0.0.1`；bootstrap 单次短时有效；会话 Cookie 为 HttpOnly、SameSite=Strict。
- MCP 永远不注册 submit、execute、cancel、resolve 或 delete 工具。
- operation 类型使用 allowlist，删除语义在入队和执行前双重检查。
- 同一时间最多一个 Jira 写调用；多进程 worker 不得重复领取。
- 严格 Pydantic mutation 模型拒绝未知字段、类型强制转换、空选择、重复/非法/未知 ID 和超过 1000 项的请求。
- 页面不使用 `innerHTML` 等解析型 sink；静态资源本地化并启用严格安全响应头。
- stdout 仅用于 MCP JSON-RPC；异常和运行日志经配置凭证脱敏后写 stderr。

## 明确不做

- Jira Cloud 优先、多用户/远程审批、多租户、多级审批。
- 任何删除能力或自动执行授权。
- 旧 batch/task/nonce/TTL/JSON 数据迁移或兼容 API/工具别名。
- 自动重试 indeterminate 或普通 failed。
- v1 日志自动过期或容量裁剪。

## 已知发布缺口

- 附件和 description 文件尚未 staging/hash 绑定，存在审核到执行的 TOCTOU。
- 克隆源与规范化后的最终 Jira payload 尚未形成不可变审核绑定。
- 删除等价语义仍需更细的字段级 schema/allowlist。
- 需要隔离 Jira Server/DC 环境下的权限、网络、自动化副作用和崩溃集成验证。
- SQLite 与本地事件记录不抵御具有同一 OS 用户权限的攻击者。

完成这些缺口和独立安全复核前，不宣称生产级强安全、不可变审核或完整不可抵赖审计。

## 验收

- 全部 18 个 MCP 工具的能力面符合读写分离，旧工具名不存在。
- 全部已挂载 Tool、Prompt、Skill/知识 Resource 和固定 Resource 的 FastMCP `description` 均来自完整、清晰的中文 docstring；Tool 覆盖全部签名参数和返回结构，并准确声明只读或仅入队副作用。契约测试检查说明的存在性、中文、必要结构、参数覆盖和副作用边界，不绑定易变的具体文案。
- 全部 Prompt、Skill/知识 Resource 和固定 Resource 返回非空、可理解的中文内容，并明确其内容不是 Jira 指令或人工授权。
- 默认排序、人工锚点重建、revision 409、原子 submit/cancel/reorder、ETag/304 和严格模型测试通过。
- 单线程/多线程/多进程 worker 保持单 Jira 写；结果事务失败留下 executing 并在恢复时转 indeterminate。
- 三种 indeterminate 和 held 处置符合 attempt/execution ID 规则，均无隐式 Jira 调用。
- MCP 日志只覆盖 Tool 调用、Resource 读取和 Prompt/Skill 获取，成对保存完整业务请求与响应及必要的接收时间、返回时间和业务处理耗时；不记录能力列表、initialize 或 notification，日志故障不影响业务响应。
- Web bootstrap/session、高风险确认、四面板、分页、CSP 和真实浏览器恶意内容回归通过。
- 完整 pytest 套件通过。
