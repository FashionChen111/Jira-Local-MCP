# Jira 配置与安装体验设计

依据：用户已要求按上一轮方案输出计划并执行，采用当前会话内 executing-plans。

目标：与 Wiki 相同的 `.env` 配置、交互安装、只读认证检查和本机接入文档。
范围：配置边界、安装脚本、依赖分类、文档及相应测试。Python 3.10+，保留现有 Config/JiraConfig/get_config/set_config 接口、工具签名、认证方式和默认值；不修改 core、web、server.py、数据库 schema 或 data/。不读取用户真实凭据、不执行真实 Jira 写入、不推送或发布。

方案比较：只换配置文件无法解决安装接入问题；迁移整个包布局扩大风险；选择保留当前布局并增加独立安装与检查入口。

配置：只读项目根 .env，不读取环境变量或 YAML 回退，不插值；支持 UTF-8 BOM、CRLF、注释、export 和单行引号；格式错误仅报行号。PAT 默认，Basic 保留。URL 必须为无内嵌凭据、无 query/fragment 的 http(s) URL，保留上下文路径。TLS bool 显式解析；timeout 正整数、port 1..65535、max_txt_bytes 正整数。配置对象和 data/ 路径保持不变。

安装：setup.bat/setup.command 调 scripts/install.py；仅更新 URL、认证类型及该类型凭据，回车保留，隐藏输入失败则停止。现有 .env 优先；不存在时可由用户明确选择从旧 YAML 导入，旧文件和 data/ 均不改变。保存采用同目录临时文件、权限保留、并发内容检查和原子替换；新文件创建权限 0600（POSIX）。不覆盖已有 .env；不删除虚拟环境。安装运行依赖，不装测试浏览器。失败退出非零，输出阶段与脱敏错误。

依赖：runtime requirements、dev requirements 和从现有环境导出的 constraints；不重建现有开发环境。YAML 保留为一次性旧配置导入依赖。

健康检查：独立 Jira SDK 实例，关闭默认 serverInfo 探测、重试设为 0，仅调用 myself，校验身份，finally close；不导入 server、不启动 worker、不创建队列数据库。错误类别可识别但不输出原异常正文。检查不证明所有项目权限。

接入：生成 MCP_SETUP.md，含 JSON 编码的绝对虚拟环境 Python 和 server.py 参数；不依赖 cwd、不含凭据。模板覆盖 Codex/Claude Code/WorkBuddy/DSH，提醒按实际版本核对注册格式；仅注册与工具发现，不写 Jira。移动目录重新 setup。发布物仅包含 .env.example，不打包真实配置。

验证：临时文件和离线替身覆盖配置、特殊字符、导入、原子保存、并发修改、安装失败阶段、只读检查关闭和生成 JSON；现有后端和浏览器回归。Windows 无运行环境时只能静态检查并注明，真实账号验证不自动进行。
