"""Configure and install Jira MCP without starting its execution worker."""
from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from string import Template
from contextlib import contextmanager
from collections.abc import Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from env_file import parse_env
from config import config_from_values


def _env_literal(key: str, value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("配置值必须是单行文本")
    return json.dumps(value, ensure_ascii=False)


def _copy_config_permissions(source: Path, destination: Path) -> None:
    if os.name != "nt":
        os.chmod(destination, source.stat().st_mode & 0o777)
        return

    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    get_security = security.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    get_control = security.GetSecurityDescriptorControl
    get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    get_control.restype = wintypes.BOOL
    set_security = security.SetNamedSecurityInfoW
    set_security.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    set_security.restype = wintypes.DWORD
    free = ctypes.WinDLL("kernel32").LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p

    file_object = 1
    dacl_information = 0x4
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    error = get_security(
        str(source), file_object, dacl_information, None, None,
        ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Keep the original DACL and its inheritance setting before writing the PAT.
        inheritance = 0x80000000 if control.value & 0x1000 else 0x20000000
        error = set_security(
            str(destination), file_object, dacl_information | inheritance, None, None, dacl, None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        free(descriptor)


@contextmanager
def _configuration_lock(path: Path) -> Iterator[None]:
    # Keep this inode: unlinking a lock file permits competing writers to lock different files.
    descriptor = os.open(path.with_name(path.name + ".setup.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save_configuration(path: Path, original: bytes, changes: dict[str, str], *, create: bool = False) -> None:
    text = original.decode("utf-8-sig")
    literals = {key: _env_literal(key, value) for key, value in changes.items()}
    seen: set[str] = set()
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        prefix, separator, _ = line.partition("=")
        key = prefix.strip()
        if key.startswith("export "):
            key = key[len("export "):].lstrip()
        if separator and not key.startswith("#") and key in literals:
            ending = line[len(line.rstrip("\r\n")) :]
            line = f"{prefix}={literals[key]}{ending}"
            seen.add(key)
        lines.append(line)
    result = "".join(lines)
    newline = "\r\n" if "\r\n" in text else "\n"
    for key in literals:
        if key in seen:
            continue
        if result and not result.endswith(("\n", "\r")):
            result += newline
        result += f"{key}={literals[key]}{newline}"
    data = result.encode("utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8")
    with _configuration_lock(path):
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".env.", delete=False) as handle:
                temporary = Path(handle.name)
            if not create:
                _copy_config_permissions(path, temporary)
            temporary.write_bytes(data)
            if create:
                # Publish complete contents atomically; an existing .env is never overwritten.
                os.link(temporary, path)
            else:
                if path.read_bytes() != original:
                    raise ValueError("配置文件在输入期间发生变化，请重新运行安装。")
                os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _legacy_values(path: Path) -> dict[str, str]:
    # Imported only when migrating, so first-time setup needs no third-party package.
    import yaml

    with path.open(encoding="utf-8-sig") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict) or not isinstance(raw.get("jira"), dict):
        raise ValueError("旧配置结构不正确")
    jira = raw["jira"]
    values: dict[str, str] = {}
    for old_key, new_key in (
        ("url", "ATLASSIAN_JIRA_URL"), ("auth_type", "ATLASSIAN_JIRA_AUTH_TYPE"),
        ("token", "ATLASSIAN_JIRA_PAT"), ("username", "ATLASSIAN_JIRA_USERNAME"),
        ("password", "ATLASSIAN_JIRA_PASSWORD"),
    ):
        value = jira.get(old_key, "pat" if old_key == "auth_type" else "")
        if value is None and old_key in ("token", "username", "password"):
            value = ""
        if not isinstance(value, str):
            raise ValueError("旧配置字段类型不正确")
        values[new_key] = value
    verify = jira.get("verify_ssl", True)
    if type(verify) is not bool:
        raise ValueError("旧配置 verify_ssl 必须是布尔值")
    values["ATLASSIAN_JIRA_VERIFY_SSL"] = str(verify).lower()
    sections = {name: {} if raw.get(name) is None else raw[name] for name in ("web", "files")}
    for section, key, new_key, default in (
        (jira, "timeout", "ATLASSIAN_JIRA_TIMEOUT_SECONDS", 30),
        (sections["web"], "port", "JIRA_MCP_WEB_PORT", 8766),
        (sections["files"], "max_txt_bytes", "JIRA_MCP_MAX_TXT_BYTES", 262144),
    ):
        if not isinstance(section, dict):
            raise ValueError("旧配置结构不正确")
        value = section.get(key, default)
        if type(value) is not int:
            raise ValueError("旧配置数值类型不正确")
        values[new_key] = str(value)
    config_from_values(values)
    return values


def _secret(label: str, old: str) -> str:
    prompt = f"{label}（已配置，回车保留；输入不显示）：" if old else f"{label}（必填，输入不显示）："
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(prompt)
    return value if value else old


def ensure_configuration(root: Path, *, edit: bool = False) -> bool:
    path = root / ".env"
    try:
        exists = path.exists()
        original = path.read_bytes() if exists else (root / ".env.example").read_bytes()
        original_values = parse_env(original.decode("utf-8-sig"))
        values = dict(original_values)
        imported: dict[str, str] = {}
        if not exists and (root / "config.yaml").exists():
            choice = input("发现旧 config.yaml，是否导入到 .env？[y/N]：").strip().lower()
            if choice == "y":
                try:
                    imported = _legacy_values(root / "config.yaml")
                except ImportError:
                    print("旧配置导入需要 PyYAML，请先在现有环境安装 requirements.txt 后重试。", file=sys.stderr)
                    return False
                except Exception:
                    print("旧配置无法导入，请检查结构、类型及必填项；原文件未修改。", file=sys.stderr)
                    return False
                values.update(imported)
        print("====== 更新配置 ======" if edit else "====== Jira 连接配置 ======", flush=True)
        old_url = values.get("ATLASSIAN_JIRA_URL", "")
        old_auth = values.get("ATLASSIAN_JIRA_AUTH_TYPE", "pat")
        changes = dict(imported)
        changes["ATLASSIAN_JIRA_URL"] = input(
            "Jira 地址（已配置，回车保留）：" if old_url else "Jira 地址（必填）："
        ).strip() or old_url
        changes["ATLASSIAN_JIRA_AUTH_TYPE"] = input(
            f"认证方式 [pat/basic，默认 {old_auth}]："
        ).strip() or old_auth
        if changes["ATLASSIAN_JIRA_AUTH_TYPE"] == "pat":
            changes["ATLASSIAN_JIRA_PAT"] = _secret("PAT", values.get("ATLASSIAN_JIRA_PAT", ""))
        elif changes["ATLASSIAN_JIRA_AUTH_TYPE"] == "basic":
            old_username = values.get("ATLASSIAN_JIRA_USERNAME", "")
            changes["ATLASSIAN_JIRA_USERNAME"] = input(
                "用户名（已配置，回车保留）：" if old_username else "用户名（必填）："
            ).strip() or old_username
            changes["ATLASSIAN_JIRA_PASSWORD"] = _secret("密码", values.get("ATLASSIAN_JIRA_PASSWORD", ""))
        config_from_values({**values, **changes})
        changes = {key: value for key, value in changes.items()
                   if value != original_values.get(key, "pat" if key == "ATLASSIAN_JIRA_AUTH_TYPE" else "")}
        if changes or not exists:
            _save_configuration(path, original, changes, create=not exists)
            print("已更新 .env。")
        else:
            print("已保留现有配置。")
        return True
    except getpass.GetPassWarning:
        print("当前终端无法隐藏凭据输入，请在系统终端重新运行。", file=sys.stderr)
    except (OSError, UnicodeError, ValueError, EOFError, KeyboardInterrupt):
        print("无法读取或保存配置，请检查 .env 的格式、必填项和目录权限；现有配置未覆盖。", file=sys.stderr)
    return False


def run_install(root: Path) -> int:
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更高版本。", file=sys.stderr)
        return 1
    if not ensure_configuration(root):
        return 1
    print("\n====== 环境配置 ======", flush=True)
    child_env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME"):
        child_env.pop(name, None)
    venv = root / ".venv"
    venv_python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not venv.exists():
        print("正在创建虚拟环境，请稍候...", flush=True)
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)], cwd=root, check=True, env=child_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )
        except (OSError, subprocess.CalledProcessError):
            print("创建虚拟环境失败，请检查 Python 和目录权限；现有环境未删除。", file=sys.stderr)
            return 1
    if not venv_python.is_file():
        print("现有 .venv 不可用，请检查环境；脚本不会自动删除。", file=sys.stderr)
        return 1
    local_check = (
        "import asyncio\n"
        "from server import mcp\n"
        "names = {tool.name for tool in asyncio.run(mcp.list_tools())}\n"
        "assert {'jira_query_issue', 'jira_jql_search', 'jira_show_queue'} <= names, 'Required MCP tools are unavailable'\n"
    )
    steps = (
        ("检查 pip", [str(venv_python), "-m", "pip", "--version"]),
        ("安装依赖", [str(venv_python), "-m", "pip", "install", "-c", str(root / "constraints.txt"), "-r", str(root / "requirements.txt")]),
        ("检查 MCP 工具", [str(venv_python), "-c", local_check]),
        ("检查 Jira 认证", [str(venv_python), str(root / "healthcheck.py")]),
    )
    for label, command in steps:
        print(f"{label}中，请稍候...", flush=True)
        quiet = label in ("检查 pip", "安装依赖")
        try:
            subprocess.run(
                command, cwd=root, check=True, env=child_env,
                stdout=subprocess.DEVNULL if quiet else None,
                stderr=subprocess.STDOUT if quiet else None,
            )
        except (OSError, subprocess.CalledProcessError):
            print(f"{label}失败，请检查网络、依赖源或项目环境；现有配置和环境保留。", file=sys.stderr)
            return 1
        print(f"{label}完成。", flush=True)
    try:
        document = _write_agent_setup(root, venv_python)
    except (OSError, ValueError, KeyError):
        print("生成接入文档失败，请检查 MCP_SETUP.template.md 和目录权限。", file=sys.stderr)
        return 1
    print(f"请将 {document} 发给 Agent 完成 MCP 配置。")
    return 0


def main() -> int:
    if sys.argv[1:] == ["--edit"]:
        if ensure_configuration(PROJECT_ROOT, edit=True):
            print("配置处理完成，请重启 MCP 和审核服务；可重新运行安装检查。")
            return 0
        return 1
    if sys.argv[1:]:
        print("Usage: install.py [--edit]", file=sys.stderr)
        return 2
    return run_install(PROJECT_ROOT)


def _write_agent_setup(root: Path, venv_python: Path) -> Path:
    root = root.resolve()
    command = str(venv_python.absolute())
    args = [str(root / "server.py")]
    config = {"mcpServers": {"jira-local-mcp": {"command": command, "args": args}}}
    template = Template((root / "MCP_SETUP.template.md").read_text(encoding="utf-8"))
    document = template.substitute(
        SYSTEM={"win32": "Windows", "darwin": "macOS", "linux": "Linux"}.get(sys.platform, sys.platform),
        PROJECT_DIR=json.dumps(str(root), ensure_ascii=False),
        MCP_CONFIG=json.dumps(config, ensure_ascii=False, indent=2),
        PYTHON_COMMAND=json.dumps(command, ensure_ascii=False),
        SERVER_ARGS=json.dumps(args, ensure_ascii=False),
    )
    output = root / "MCP_SETUP.md"
    output.write_text(document, encoding="utf-8", newline="\n")
    return output


if __name__ == "__main__":
    raise SystemExit(main())
