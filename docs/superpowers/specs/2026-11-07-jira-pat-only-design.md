# Jira 仅 PAT 认证设计

用户明确要求只支持 PAT，并输出计划、直接执行。目标是避免把 PAT 填入认证方式栏。

选择直接收口认证模型及所有消费者，移除 Basic 分支；仅隐藏安装选项会留下行为不一致。项目仍使用当前 .env、安装入口和 Jira SDK，不引入新依赖，不改变队列、审批、执行及数据库。

新安装流程只询问 Jira 地址和隐藏输入的 PAT，不询问认证方式或用户名密码。.env.example 仅公开 URL/PAT 及现有高级配置。JiraConfig 只承载 url/token/verify_ssl/timeout。运行客户端和健康检查始终传 token_auth，不传 basic_auth。

已有 .env 的 AUTH_TYPE=pat 作为旧键可继续读取，不再生成该键。显式 basic 或其他认证方式必须拒绝，给出 PAT 配置说明；不自动把用户名密码转换为 PAT，不改写旧文件。旧 YAML 的 pat 配置保留导入；Basic 配置拒绝且不创建 .env。忽略旧 YAML 中非活动的用户名密码，不复制到新 .env。

保留配置原子保存、并发校验、回车保留、隐藏输入、错误脱敏及资源关闭。测试使用假凭据和 SDK 替身，不读取真实配置、不连接 Jira。更新现行 README/PRD，历史设计文档不改写。
