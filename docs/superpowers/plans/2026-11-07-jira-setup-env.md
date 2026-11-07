# Jira Setup and Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供与 Wiki 一致的配置、安装和接入体验。
**Architecture:** 保留 Config 对象及业务目录，在配置边界读取 .env。独立安装程序管理配置和环境，独立 healthcheck 只读检查，模板生成本机接入文档。
**Tech Stack:** Python 3.10+、stdlib、现有 PyYAML/Jira SDK、pytest、pip。
**Spec:** docs/superpowers/specs/2026-11-07-jira-setup-env-design.md

## Global Constraints
- Python 3.10+，保留现有 Config/JiraConfig/get_config/set_config 接口、工具签名、认证方式和默认值。
- 不修改 core、web、server.py、数据库 schema 或 data/。
- 不读取用户真实凭据、不执行真实 Jira 写入、不推送或发布。
- 只读项目根 .env，不读取环境变量或 YAML 回退，不插值。
- 用户要求计划并执行，直接按已讨论设计实施；保留现有目录布局。

## Review Focus
- 含 #、空格、单/双引号、$ 的凭据原样往返，不展开变量（Task 1/2）。
- 现有 .env、重复键、并发编辑及保存失败不能静默丢失配置（Task 2）。
- 旧 YAML 错误类型不能静默转换，迁移不覆盖 .env 或原文件（Task 2）。
- Windows/macOS 含空格和中文目录可生成准确启动参数，脚本失败退出非零（Task 3/4）。
- 安装工具检查不启动 worker，认证失败/响应异常均关闭客户端且不输出凭据（Task 3）。

## File Structure
- env_file.py：单行 .env 读写语法；config.py：沿用配置对象并校验 .env。
- scripts/install.py：交互配置、旧配置导入、虚拟环境及阶段编排。
- healthcheck.py：只读身份检查和资源释放。
- setup.bat/setup.command：系统启动入口。
- requirements*.txt/constraints.txt：运行、开发、锁定依赖。
- MCP_SETUP.template.md/README.md/PRD.md/.gitignore：接入与配置文档、敏感产物忽略。

### Task 1: .env 配置边界
**Files:** Create env_file.py/.env.example; Modify config.py/.gitignore/tests/test_config.py; Test tests/test_env_file.py。
**Interfaces:** Consumes existing Config/JiraConfig; Produces parse_env(text: str) -> dict[str,str], load_config(path: str | None = None) -> Config; .env 键见 .env.example。
- [ ] Step 1: 写 tests/test_env_file.py 与配置测试：BOM/CRLF/export/quotes、字面 $ 和 #、错误行不泄密、PAT/Basic、默认值、非法 URL/数值/bool、cwd 和环境变量独立、无 YAML fallback、data 路径契约。
- [ ] Step 2: Run `PYTHON -m pytest tests/test_env_file.py tests/test_config.py -q`。Expected: FAIL，缺少 parse_env 或 .env 未被支持。
- [ ] Step 3: 实现 parse_env 与 load_config，保留现有类型/接口；根目录绝对 .env；严格转换配置，错误只指出键或行。新增示例与忽略规则。
- [ ] Step 4: Run `PYTHON -m pytest tests/test_env_file.py tests/test_config.py -q`。Expected: PASS。
- [ ] Step 5: `git add env_file.py .env.example config.py .gitignore tests/test_env_file.py tests/test_config.py`；`git commit -m "feat(config): 使用项目 .env 管理 Jira 配置"`。

### Task 2: 配置引导及旧 YAML 导入
**Files:** Create scripts/install.py/tests/test_install.py。
**Interfaces:** Consumes parse_env/load_config; Produces ensure_configuration(root: Path, *, edit: bool=False) -> bool, _save_configuration(path: Path, original: bytes, changes: dict[str,str]) -> None。
- [ ] Step 1: 测试临时目录：首次 PAT/Basic 配置、回车保留、无回显失败停止、BOM/CRLF/注释及重复键保留、并发保存拒绝、替换失败保持原文；旧 YAML 同意导入/拒绝导入、现有 .env 优先、错误类型/不完整旧配置拒绝且无敏感输出。
- [ ] Step 2: Run `PYTHON -m pytest tests/test_install.py -q`。Expected: FAIL，安装模块不存在。
- [ ] Step 3: 复用 Wiki 的原子保存和权限保留做法；所有候选配置先 load_config 验证再写入；仅配置变更键。导入必须显式选择，保留旧文件及 data/。
- [ ] Step 4: Run `PYTHON -m pytest tests/test_install.py tests/test_config.py tests/test_env_file.py -q`。Expected: PASS。
- [ ] Step 5: `git add scripts/install.py tests/test_install.py`；`git commit -m "feat(setup): 增加配置引导和旧配置导入"`。

### Task 3: 环境安装和只读检查
**Files:** Create healthcheck.py/setup.bat/setup.command/requirements-dev.txt/constraints.txt/tests/test_healthcheck.py; Modify scripts/install.py/requirements.txt/tests/test_install.py。
**Interfaces:** Consumes ensure_configuration; Produces run_install(root: Path) -> int, verify_jira_connection(cfg: JiraConfig) -> None, healthcheck.main() -> int。
- [ ] Step 1: 测试 SDK 创建选项、myself 身份校验、关闭资源、脱敏错误；安装复用 venv、按顺序执行 pip/工具发现/健康检查、失败停止、无浏览器安装、生成文档仅在成功时执行。
- [ ] Step 2: Run `PYTHON -m pytest tests/test_healthcheck.py tests/test_install.py -q`。Expected: FAIL，新入口不存在。
- [ ] Step 3: 实现独立 SDK 健康检查；分离依赖并锁定现有已安装版本；两平台入口定位自身目录并保留退出码；安装期间不调用 server.run，不启动 worker。
- [ ] Step 4: Run `PYTHON -m pytest tests/test_healthcheck.py tests/test_install.py tests/test_config.py tests/test_env_file.py -q`。Expected: PASS。Run `sh -n setup.command`。Expected: exit 0。
- [ ] Step 5: 提交任务文件，`git commit -m "feat(setup): 增加环境安装和只读连接检查"`。

### Task 4: 接入文档与整体验证
**Files:** Create MCP_SETUP.template.md; Modify scripts/install.py/tests/test_install.py/README.md/PRD.md; Delete config.example.yaml（示例由 .env.example 替代）。
**Interfaces:** Consumes run_install; Produces _write_agent_setup(root: Path, venv_python: Path) -> Path。
- [ ] Step 1: 测试临时中文、空格、引号目录生成 JSON，command/args 绝对且准确，不读取 .env，不泄露凭据；模板错误导致安装失败。
- [ ] Step 2: Run `PYTHON -m pytest tests/test_install.py -q`。Expected: FAIL，接入文档生成未实现。
- [ ] Step 3: 实现模板替换；更新 README/PRD 的配置、安装、接入、开发和升级说明；移除旧模板，保留用户 config.yaml。
- [ ] Step 4: Run `PYTHON -m pytest tests/ -q`。Expected: 全套 PASS；环境阻断则记录具体原因并完成可运行检查。Run `PYTHON -m pyright config.py env_file.py healthcheck.py scripts/install.py` 和 `git diff --check`。Expected: 无错误。
- [ ] Step 5: 提交任务文件，`git commit -m "docs(setup): 完善 Jira 安装与 Agent 接入说明"`。

## Execution Notes
PYTHON 指原项目已有 .venv/bin/python 的绝对路径；不重建开发环境。每任务 task-start/task-done 保留记录。最终使用一次独立审查；不合并、不推送。本机 Windows 和真实账号未验证必须明确说明。
