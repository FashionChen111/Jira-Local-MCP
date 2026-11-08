# 为当前 Agent 配置 Jira MCP

本文件由安装脚本生成，仅包含本机接入信息。请在当前客户端添加或更新 stdio MCP，名称为 `jira-local-mcp`，保留其他服务器和用户设置。

## 本机安装信息

操作系统：$SYSTEM。项目目录（JSON 字符串）：

```json
$PROJECT_DIR
```

## stdio 启动配置

`command` 为虚拟环境 Python 的绝对路径，`args` 为独立参数数组；不需要设置工作目录。

```json
$MCP_CONFIG
```

## 接入步骤

1. 识别当前客户端；无法识别时再请用户选择 Codex、Claude Code、WorkBuddy 或 DSH。
2. 检查启动路径是否存在。移动过安装目录时，在新目录重新运行 setup，使用新生成的文档。
3. 检查当前客户端的帮助或配置格式，将上述条目合并到现有配置；已有同名条目则更新，保留其他设置。
4. Codex、Claude Code 优先使用各自的 MCP 管理命令，保持 command 和 args 为分开的参数；WorkBuddy 在 MCP 配置入口合并 JSON。
5. 重载连接并发现 `jira_query_issue`、`jira_jql_search` 和 `jira_show_queue`。仅检查工具是否可用，不调用审核入口、不入队或提交任何操作。
6. 报告配置位置、服务器名称、已注册/已连接状态；需要重启时说明具体操作。权限不足时提供可粘贴配置，不宣称已完成连接。

DSH / DeepSeek Harness：按当前版本支持的配置格式，在当前 profile 的 `cordis.patch.yml` 中合并以下条目，保留其他 patch，再重载。

```yaml
- insert:
    - id: mcp-jira
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: jira-local-mcp
        transport: stdio
        command: $PYTHON_COMMAND
        args: $SERVER_ARGS
```

## 凭据和验证范围

凭据保存在项目 `.env`，不放入客户端配置。禁止为配置客户端而读取、输出或上传 `.env`、旧 `config.yaml` 或凭据，也不要要求用户把 PAT/密码粘贴到对话中。

本次接入仅执行注册和工具发现，不进行 Jira 写入、队列提交或执行。健康检查只证明当前身份认证通过，不代表所有项目均有权限。
