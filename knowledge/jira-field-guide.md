# JIRA 字段手册

> 本文档可由用户自定义补充，例如填入本 JIRA 实例的自定义字段 ID 与名称对应关系。

## 根据元数据构造字段值

先根据本次操作的创建、编辑或流转元数据确认字段 ID、类型及可用性，再按本手册构造字段值。元数据负责提供 Jira 的实际信息，本手册负责解释常见类型的写法；不需要枚举所有文本、数字或用户。

- `schema.type` 表示字段类型；`schema.items` 表示数组元素类型。
- `schema.system` / `schema.custom` 帮助识别具体标准字段或自定义字段实现。下表适用于已识别的 Jira 原生类型；插件字段不能仅凭 `type=string` 或 `any` 套用普通文本格式。
- `required`、`operations`、`allowedValues` 保留所在操作上下文的含义。元数据未提供 `allowedValues` 不等于空选项，也不等于任意值都允许。
- 字段类型可以支撑常见值的形式判断，但不保证包含所有长度、范围、清空规则或插件校验。普通局部编辑中，没有修改的已有必填字段不需要仅因 `required=true` 就重复传入。

下表为字段映射中单个值的示例，不是完整的 Tool 参数。示例 ID 和用户名必须替换为当前查询取得的真实值。

| 元数据类型 | 字段值示例 | 如何取值 |
|---|---|---|
| `string`（原生文本/多行文本） | `"说明文字"` | 用户提供正文 |
| `number` | `12` 或 `12.5` | 使用 JSON 数字，不把数字包装成字符串 |
| `date` | `"2026-09-07"` | 使用 `YYYY-MM-DD` 日期 |
| `datetime` | `"2026-09-07T09:30:00.000+0800"` | 使用带时区的日期时间，不自行改变用户指定的时区 |
| `array` + `items=string`（如 labels） | `["backend", "urgent"]` | 使用字符串数组；已有标签建议不是封闭选项表 |
| `user` | `{"name":"username"}` | 按姓名查询用户，取得 Server/DC 的 name |
| `array` + `items=user` | `[{"name":"user_a"},{"name":"user_b"}]` | 分别确认用户 name |
| `option` | `{"id":"10001"}` | 从当前字段的 allowedValues 选择；原生选项也可使用对应 value |
| `array` + `items=option` | `[{"id":"10001"},{"id":"10002"}]` | 从当前字段的 allowedValues 选择多个选项 |
| `option-with-child` | `{"id":"10001","child":{"id":"10002"}}` | 同时保留父项与所属子项，不能只填子项 |
| `priority` | `{"id":"10001"}` | 选择当前字段的优先级；创建 Tool 的专用 priority 参数使用名称 |
| `array` + `items=component/version` | `[{"id":"10001"}]` | 选择当前上下文的组件或版本 |
| `timetracking` | `{"originalEstimate":"2h","remainingEstimate":"1h"}` | 使用明确带单位的估时；实际记录工时使用独立 worklog Tool |

用户字段的查询步骤：

1. 确认字段是 `user` 或 `array[user]`；字段查询无需返回全部用户。
2. 经办人 assignee 使用项目/事务范围内的可指派用户查询；普通用户自定义字段或 reporter 使用用户搜索取得 name。
3. 同名显示名需要按用户名消歧。将查询结果的 name 写入 `{"name":...}`，不要填 displayName、JIRAUSER key 或 Cloud accountId。
4. 评论提及使用 `[~username]`。已知目标事务且支持相应查询时，可以按该事务的可浏览用户缩小搜索范围。

用户能被搜索到只证明身份存在；如具体字段配置了额外用户范围，需要相应字段候选信息。经办人的可指派资格也不替代字段编辑入口检查。

已注册的查询路径：

- 创建：`jira_meta_projects(query=项目线索)` → `jira_meta_issue_types(project_key=P)` → `jira_meta_fields(context={"kind":"create","project_key":P,"issue_type_id":T})`。
- 编辑：`jira_meta_fields(context={"kind":"edit","issue_key":I})` → `jira_meta_field(context=同一上下文,field_id=所选ID)`。
- 流转：`jira_meta_issue_transitions(issue_key=I)` → `jira_meta_fields(context={"kind":"transition","issue_key":I,"transition_id":X})` → 所需单字段详情。
- 用户：`jira_meta_user()` 查询当前令牌；`jira_meta_search_users(query=姓名)` 取得 name；`scope={"kind":"issue","issue_key":I}` 限定可浏览用户。经办人用 `jira_meta_search_assignable_users(context={"kind":"issue","issue_key":I},query=姓名)`，创建时改用项目上下文。
- 字段用户候选使用 search_users 的 `scope={"kind":"field","context":写上下文,"field_id":字段ID}`。实例过滤尚未验证时返回明确能力限制，不能将普通用户名命中视为该字段合法候选。
- 创建字段过滤的空页不代表结束，始终读取返回的 `page.next_start_at`；不能用已返回条数推算源游标。列表最多 100 条，元数据响应预算 32 KiB；遇到限制缩小 query，不使用截断值。
- 小型级联树一次返回；大树分页仍包含所选子项的父项。`options_page.next_start_at` 是匹配节点游标，祖先仅用于保留归属。
- 真实事务查询拒绝 operation ID；未来事务的编辑字段/可用流转必须等实际 Key 产生后再查询。

## 与入队参数的对应关系

- 标准字段值放入 `jira_field_update_append` 的 fields；自定义字段值放入 `jira_customfield_update_append` 的 custom_fields。
- 创建 Tool 的 project_key、issue_type_id、summary、description、priority、assignee 使用已有专用参数；其他字段才放入 extra_fields。issue_type_id 使用类型 ID，priority 使用候选名称，assignee 使用 name 字符串。
- 流转字段交给 `jira_issue_transition_append` 的 fields。附件、评论、工时、链接使用对应的独立 append Tool。
- 普通字段更新当前发送 `fields=`；即使元数据 operations 包含 add/remove，也不能直接把 REST 的 update 操作数组当成 fields 值传入。
- 本手册只帮助构造参数；append 仅在本地入队，尚未执行，仍需本地 Web 人工审核提交。

## 常用标准字段（写入格式）
| 字段 | 键名 | 写入示例 |
|---|---|---|
| 标题 | `summary` | `"summary": "新标题"` |
| 描述 | `description` | `"description": "文本"` 或 `"description": {"file": "C:\\abs\\path.txt"}` |
| 优先级 | `priority` | `"priority": {"name": "High"}` |
| 经办人 | `assignee` | `"assignee": {"name": "username"}`（Server/DC 用 name） |
| 报告人 | `reporter` | `"reporter": {"name": "username"}` |
| 标签 | `labels` | `"labels": ["backend", "urgent"]` |
| 事务类型 | `issuetype` | `"issuetype": {"id": "10001"}` |
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

## description 与附件引用
- `{"description": {"file": "...txt"}}` 会在入队时读取为 UTF-8 字符串快照，排队后不再读取源文件。
- 同一事务中待上传附件的链接引用为 `[^{{attachment:op-…}}]`，图片引用为 `!{{attachment:op-…}}!`；`op-…` 必须来自 `jira_attachment_add_append`。
- 创建事务的初始 description 不接受附件引用。新事务应按“创建 → 上传附件 → 更新 description”入队。

## Server / Data Center 注意事项
- 用户标识用 `name`（用户名），而非 Cloud 的 `accountId`。
- **移动事务（move）REST 不支持**，请在网页端手动操作。
- 子任务 ↔ 标准任务的跨层级类型转换 REST 通常不支持。

## 类型、流转、克隆与工时

- 创建使用 issue_type_id；类型变更使用 target_issue_type_id，fields_mapping 不得覆盖 issuetype；流转使用 transition_id。名称只用于解释，不能代替 ID；不兼容旧名称参数。
- 克隆读取执行时的源 project、issuetype ID、summary、description、priority；单个子任务保留 parent。field_overrides 可补齐创建必填字段或覆盖基础值，禁止 project、issuetype、summary；parent 覆盖必须是同项目的真实标准父事务。clone_subtasks=true 会拒绝且不入队。
- 克隆 field_overrides.description 支持 `{"file":"绝对路径.txt"}` 入队快照；初始描述禁止待上传附件引用。
- 工时 started 为带时区的合法 ISO 字符串，例如 `2026-09-09T09:30:00+08:00`，入队校验、worker 转 datetime 后交给 SDK；禁止推测时区。time_spent 使用 2h、1d 等单位，换算依据 jira_meta_server_settings(section="time_tracking")。
- adjust_estimate 默认 auto 自动减少估算；leave 保持估算；new 必须提供 new_estimate；manual 必须提供 reduce_by。auto/leave 不接受两个配套值，new_estimate/reduce_by 互斥。需要比较时只查询 timetracking 当前值，不要求先读工时历史。
- 链接用 jira_meta_link_types 返回的 name。参数 issue_key=I、target_issue_key=J 表达 `I —outward→ J`，反向读 `J —inward→ I`，不得把方向描述当类型名。
- 数组字段发送整体 set：先 `jira_query_issue(I,fields=[该字段ID])`，保留所有原成员及其身份，再构造完整新数组。此操作不具原子追加能力，审核期间的新变更仍可能产生冲突。
- warning 表示克隆新事务主体已成功，但附件复制存在异常；执行日志保留新 Key 和 attachment_results 数组，包含请求文件名、源附件 ID、已确认附件身份及成功/失败/结果未知/未尝试明细。新事务依赖仍会解析，禁止自动重放整个克隆。附件占位符只能引用 add_attachment operation，不选择克隆结果列表中的附件。单独上传失败或结果未知时整体为 failed，保留附件明细且不放行附件依赖。

## 本地文件边界

Agent 使用自身文件能力确认用户指定的路径、文件属性和必要正文；本 MCP 不提供本地文件查询工具。附件上限通过 jira_meta_server_settings(section="attachments") 取得，文本上限通过 config://service-info 取得。MCP 保留绝对路径、UTF-8、.txt、大小限制和执行时附件检查；description 入队固化，附件执行时才读取。

字段限定用户搜索通过固定 groupuserpicker 取得身份建议，候选过滤标记 candidate_validation=unverified；端点可达或返回用户不证明实例过滤生效，不得据此宣称字段合法可选。该建议无 offset 续页，应缩小 query。
