# Jira PAT Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 仅支持 PAT，安装只询问地址和 PAT。
**Architecture:** 删除 JiraConfig 的多认证字段，客户端与健康检查统一 token_auth；安装不再选择认证方式。旧显式 Basic 配置在边界拒绝，现有 PAT 配置可继续使用。
**Tech Stack:** Python 3.10+、现有 Jira SDK/PyYAML、pytest。
**Spec:** docs/superpowers/specs/2026-11-07-jira-pat-only-design.md

## Global Constraints
- 不改变队列、审批、执行及数据库，不引入新依赖。
- 不读取真实配置、不连接 Jira，不自动改写用户现有配置。
- 保留隐藏输入、回车保留、原子保存、并发校验、错误脱敏及资源关闭。
- 现有工作区继续使用，用户明确要求计划并执行，不增加交接确认。

## Review Focus
- 旧 .env 显式 basic 即使包含 PAT 也要拒绝（配置及安装测试）。
- 旧 YAML Basic 不得写入模板 .env；PAT 导入不得复制用户名密码（导入测试）。
- 首次安装只有一个明文地址输入和一个隐藏 PAT 输入（提示捕获测试）。
- 运行客户端和健康检查不得传入 basic_auth（SDK 边界测试）。
- 空 PAT 和认证失败继续脱敏，保存失败及并发保护保留（原测试与配置验证）。

## File Structure
修改 config.py（认证模型与验证）、scripts/install.py（交互与导入）、healthcheck.py/core/jira_client.py（SDK认证参数）、.env.example/README.md/PRD.md（现行说明）；更新 tests/conftest.py/test_config.py/test_install.py/test_healthcheck.py，新增 tests/test_jira_client.py。

### Task 1: 认证契约收口及使用文档
**Files:** 上述文件。
**Interfaces:** JiraConfig(url: str, token: str = '', verify_ssl: bool = True, timeout: int = 30)，get_config/load_config 保持；UnsupportedAuthenticationError(ValueError) 表示显式非 PAT 的旧配置；ensure_configuration(root, edit=False) 和 verify_jira_connection(cfg) 保持接口。

- [x] Step 1: 更新 fixture 和认证测试，删除 Basic 成功测试，写 PAT 专属 SDK 和提示测试；新增旧 Basic 拒绝及 PAT 导入不复制无用凭据测试。关键断言：
```python
assert captured_inputs == ['Jira 地址（必填）：']
assert saved.jira.token == 'fake-pat'
assert kwargs['token_auth'] == 'fake-pat'
assert 'basic_auth' not in kwargs
assert not (root / '.env').exists()  # Basic 导入拒绝
```
- [x] Step 2: Run `PYTHON -m pytest tests/test_config.py tests/test_install.py tests/test_healthcheck.py tests/test_jira_client.py -q`。Expected: FAIL，旧交互要求认证方式、旧客户端模型尚未收口。
- [x] Step 3: 移除模型 auth_type/username/password、Basic 客户端分支及安装选择；显式旧非 PAT 配置抛 UnsupportedAuthenticationError，安装显示固定 PAT 指引；只导入有效 PAT 和现有高级配置。更新示例和现行说明。
- [x] Step 4: Run 同 Step 2 命令。Expected: PASS。Run `PYTHON -m pytest tests/ --ignore=tests/test_web_browser.py -q`，再运行 `PYTHON -m pytest tests/test_web_browser.py --browser-channel=chrome -q`。Expected: 两组全部 PASS（原有事件循环冲突要求分进程）。Run `PYTHON -m pyright --pythonpath PYTHON config.py healthcheck.py core/jira_client.py scripts/install.py` 和 `git diff --check`。Expected: 无错误。
- [x] Step 5: 只暂存相关文件，`git commit -m "refactor(auth): 统一 Jira PAT 认证和安装流程"`。

## Delivery
一次独立整份修改审查；如有重要问题，先复现再修复并回归。保持本地隔离工作区，不合并或推送。Windows 和真实账号未验证需注明。

## 执行记录

- RED：26 failed、52 passed、3 errors；旧认证模型、安装提示和 Basic 接受行为被测试捕获。
- GREEN：81 项配置、安装、健康检查、客户端边界测试通过。
- 完整回归：后端 415 passed、Chrome 浏览器 22 passed，两个既有弃用警告。
- 定向 pyright：0 errors / 0 warnings，git diff --check 通过。
- 沙箱内一次 Web 启动测试被本机端口权限阻断；获准运行后完整通过。
- Windows 和真实 Jira 认证未实测；未读取真实配置或使用用户凭据。

## 最终独立审查

审查范围 b2ddfce..ce42f67。独立审查未发现 Critical/Important/Minor 问题，Declined to judge 为空；独立复跑 81 项定向测试通过。配置模型、两个 SDK 入口、安装提示、旧配置拒绝、日志脱敏消费者和原有保存保护均完成核对。无执行方案偏离或暂缓问题。代码保留在当前隔离工作区，未合并、推送或修改用户真实配置。
