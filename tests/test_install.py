"""Setup configuration tests exercise real files and replace terminal input only."""
import os
from pathlib import Path
import warnings
import getpass

import pytest

import config
from env_file import parse_env
from scripts import install


def prepare(tmp_path):
    source = Path(__file__).resolve().parents[1]
    (tmp_path / '.env.example').write_bytes((source / '.env.example').read_bytes())
    return tmp_path


def answers(monkeypatch, inputs, secrets=()):
    inputs, secrets = iter(inputs), iter(secrets)
    monkeypatch.setattr('builtins.input', lambda prompt: next(inputs))
    monkeypatch.setattr(getpass, 'getpass', lambda prompt: next(secrets))


def test_first_pat_configuration(tmp_path, monkeypatch):
    prepare(tmp_path)
    answers(monkeypatch, ['https://jira.example.com', ''], ['p # $HOME'])
    assert install.ensure_configuration(tmp_path)
    assert config.load_config(str(tmp_path / '.env')).jira.token == 'p # $HOME'
    if os.name != 'nt':
        assert (tmp_path / '.env').stat().st_mode & 0o777 == 0o600


def test_basic_configuration(tmp_path, monkeypatch):
    prepare(tmp_path)
    answers(monkeypatch, ['https://jira.example.com', 'basic', 'user'], ['password'])
    assert install.ensure_configuration(tmp_path)
    cfg = config.load_config(str(tmp_path / '.env'))
    assert (cfg.jira.auth_type, cfg.jira.username, cfg.jira.password) == ('basic', 'user', 'password')


def test_keep_existing_values_and_advanced_settings(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    original = b'\xef\xbb\xbf# preserved\r\nATLASSIAN_JIRA_URL=https://jira.example.com\r\nATLASSIAN_JIRA_PAT=SECRET\r\nJIRA_MCP_WEB_PORT=9001\r\nOTHER=value\r\n'
    path.write_bytes(original)
    answers(monkeypatch, ['', ''], [''])
    assert install.ensure_configuration(tmp_path)
    assert path.read_bytes() == original


def test_hidden_input_failure_keeps_original(tmp_path, monkeypatch, capsys):
    prepare(tmp_path)
    path = tmp_path / '.env'
    path.write_text('ATLASSIAN_JIRA_URL=https://jira.example.com\nATLASSIAN_JIRA_PAT=SECRET\n')
    original = path.read_bytes()
    answers(monkeypatch, ['', ''])
    def unsafe(prompt):
        warnings.warn('unsafe', getpass.GetPassWarning)
        return 'LEAK'
    monkeypatch.setattr(getpass, 'getpass', unsafe)
    assert not install.ensure_configuration(tmp_path)
    assert path.read_bytes() == original
    output = capsys.readouterr()
    assert 'SECRET' not in output.out + output.err


@pytest.mark.parametrize('value', ['a # b', 'a"b', "a'b", 'both\'" # $X', ' leading ', '$HOME'])
def test_atomic_save_literal_values_and_duplicate_keys(tmp_path, value):
    path = tmp_path / '.env'
    original = b'\xef\xbb\xbf# preserved\r\nexport KEY=old\r\nKEY=duplicate\r\nOTHER=kept\r\n'
    path.write_bytes(original)
    path.chmod(0o600)
    install._save_configuration(path, original, {'KEY': value})
    result = path.read_bytes()
    assert result.startswith(b'\xef\xbb\xbf# preserved\r\n')
    assert b'OTHER=kept\r\n' in result
    assert b'\n' not in result.replace(b'\r\n', b'')
    assert parse_env(result.decode('utf-8-sig'))['KEY'] == value


def test_concurrent_change_is_not_overwritten(tmp_path):
    path = tmp_path / '.env'
    path.write_bytes(b'KEY=edited\n')
    with pytest.raises(ValueError):
        install._save_configuration(path, b'KEY=old\n', {'KEY': 'new'})
    assert path.read_bytes() == b'KEY=edited\n'
    assert list(tmp_path.iterdir()) == [path]


def test_failed_replace_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_bytes(b'KEY=old\n')
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        install._save_configuration(path, path.read_bytes(), {'KEY': 'new'})
    assert path.read_bytes() == b'KEY=old\n'
    assert list(tmp_path.iterdir()) == [path]


def test_import_yaml_preserves_original_and_data(tmp_path, monkeypatch):
    prepare(tmp_path)
    path = tmp_path / 'config.yaml'
    original = b'jira:\n  url: https://jira.example.com/context\n  token: SECRET\n  verify_ssl: false\n  timeout: 45\nweb:\n  port: 9002\nfiles:\n  max_txt_bytes: 512\n'
    path.write_bytes(original)
    data = tmp_path / 'data'; data.mkdir(); (data / 'operations.sqlite3').write_bytes(b'untouched')
    answers(monkeypatch, ['y', '', ''], [''])
    assert install.ensure_configuration(tmp_path)
    cfg = config.load_config(str(tmp_path / '.env'))
    assert (cfg.jira.timeout, cfg.web_port, cfg.max_txt_bytes) == (45, 9002, 512)
    assert cfg.jira.verify_ssl is False
    assert path.read_bytes() == original
    assert (data / 'operations.sqlite3').read_bytes() == b'untouched'


def test_declined_yaml_import_uses_new_input(tmp_path, monkeypatch):
    prepare(tmp_path)
    (tmp_path / 'config.yaml').write_text('jira: {token: OLD}')
    answers(monkeypatch, ['n', 'https://jira.example.com', ''], ['NEW'])
    assert install.ensure_configuration(tmp_path)
    assert config.load_config(str(tmp_path / '.env')).jira.token == 'NEW'


def test_existing_env_never_imports_yaml(tmp_path, monkeypatch):
    prepare(tmp_path)
    (tmp_path / '.env').write_text('ATLASSIAN_JIRA_URL=https://jira.example.com\nATLASSIAN_JIRA_PAT=NEW\n')
    (tmp_path / 'config.yaml').write_text('invalid: [SECRET')
    answers(monkeypatch, ['', ''], [''])
    assert install.ensure_configuration(tmp_path)


@pytest.mark.parametrize('yaml_text', ['jira: [SECRET]', 'jira: {url: https://jira.example.com, token: SECRET, verify_ssl: "false"}', 'jira: {url: https://jira.example.com}', 'jira: {url: https://jira.example.com, token: 123}'])
def test_invalid_yaml_import_leaves_env_absent(tmp_path, monkeypatch, capsys, yaml_text):
    prepare(tmp_path)
    (tmp_path / 'config.yaml').write_text(yaml_text)
    answers(monkeypatch, ['y'])
    assert not install.ensure_configuration(tmp_path)
    assert not (tmp_path / '.env').exists()
    output = capsys.readouterr()
    assert 'SECRET' not in output.out + output.err


def environment(root):
    executable = root / '.venv' / ('Scripts/python.exe' if install.sys.platform == 'win32' else 'bin/python')
    executable.parent.mkdir(parents=True)
    executable.touch()
    return executable


def test_install_reuses_environment_and_runs_safe_stages(tmp_path, monkeypatch):
    executable = environment(tmp_path)
    monkeypatch.setattr(install, 'ensure_configuration', lambda root: True)
    commands = []
    def run(command, **kwargs):
        assert kwargs['cwd'] == tmp_path
        assert kwargs['check'] is True
        assert 'PYTHONPATH' not in kwargs['env']
        commands.append(command)
    monkeypatch.setenv('PYTHONPATH', 'unrelated')
    monkeypatch.setattr(install.subprocess, 'run', run)
    generated = []
    monkeypatch.setattr(install, '_write_agent_setup', lambda root, python: generated.append((root, python)) or root / 'MCP_SETUP.md', raising=False)
    assert install.run_install(tmp_path) == 0
    assert len(commands) == 4
    assert all(command[0] == str(executable) for command in commands)
    assert commands[0][1:] == ['-m', 'pip', '--version']
    assert commands[1][1:] == ['-m', 'pip', 'install', '-c', str(tmp_path / 'constraints.txt'), '-r', str(tmp_path / 'requirements.txt')]
    assert commands[2][1] == '-c'
    assert commands[3][1:] == [str(tmp_path / 'healthcheck.py')]
    assert not any('playwright' in str(command) for command in commands)
    assert generated == [(tmp_path, executable)]


@pytest.mark.parametrize('failed_stage', range(4))
def test_install_stops_at_failed_stage_without_document(tmp_path, monkeypatch, capsys, failed_stage):
    environment(tmp_path)
    monkeypatch.setattr(install, 'ensure_configuration', lambda root: True)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if len(commands) == failed_stage + 1:
            raise install.subprocess.CalledProcessError(3, command, stderr='SECRET')
    monkeypatch.setattr(install.subprocess, 'run', run)
    def unexpected(*args):
        pytest.fail('document generated after failed check')
    monkeypatch.setattr(install, '_write_agent_setup', unexpected, raising=False)
    assert install.run_install(tmp_path) == 1
    assert len(commands) == failed_stage + 1
    output = capsys.readouterr()
    assert 'SECRET' not in output.out + output.err


def test_invalid_existing_environment_not_deleted(tmp_path, monkeypatch):
    (tmp_path / '.venv').mkdir()
    (tmp_path / '.venv/keep').write_bytes(b'keep')
    monkeypatch.setattr(install, 'ensure_configuration', lambda root: True)
    assert install.run_install(tmp_path) == 1
    assert (tmp_path / '.venv/keep').read_bytes() == b'keep'


def test_tool_discovery_check_does_not_start_worker(tmp_path, monkeypatch, cfg):
    import subprocess
    real_run = subprocess.run
    environment(tmp_path)
    monkeypatch.setattr(install, 'ensure_configuration', lambda root: True)
    commands = []
    monkeypatch.setattr(install.subprocess, 'run', lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(install, '_write_agent_setup', lambda root, python: root / 'MCP_SETUP.md', raising=False)
    assert install.run_install(tmp_path) == 0
    # Run the emitted code in the same process boundary used by the installer.
    import subprocess
    source = Path(__file__).resolve().parents[1]
    guard = (
        "from core.executing import worker\n"
        "def forbidden():\n"
        "    raise AssertionError('tool discovery started execution worker')\n"
        "worker.start_worker = forbidden\n"
    )
    result = real_run([install.sys.executable, "-c", guard + commands[2][2]], cwd=source, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_generated_document_contains_exact_absolute_json_without_reading_credentials(tmp_path):
    import json
    import re
    root = tmp_path / '中文 Jira "path" with spaces'; root.mkdir()
    source = Path(__file__).resolve().parents[1]
    (root / 'MCP_SETUP.template.md').write_bytes((source / 'MCP_SETUP.template.md').read_bytes())
    python = root / '.venv/bin/python'
    (root / '.env').write_text('THIS FILE IS NOT VALID ENV SECRET')
    document = install._write_agent_setup(root, python)
    text = document.read_text()
    configs = [json.loads(block) for block in re.findall(r'```json\n(.*?)\n```', text, re.S)]
    config = next(value for value in configs if isinstance(value, dict) and 'mcpServers' in value)
    entry = config['mcpServers']['jira-local-mcp']
    assert entry['command'] == str(python.absolute())
    assert entry['args'] == [str(root / 'server.py')]
    assert 'SECRET' not in text
    assert 'cwd' not in entry


def test_missing_template_fails_install(tmp_path, monkeypatch):
    environment(tmp_path)
    monkeypatch.setattr(install, 'ensure_configuration', lambda root: True)
    monkeypatch.setattr(install.subprocess, 'run', lambda *args, **kwargs: None)
    assert install.run_install(tmp_path) == 1
    assert not (tmp_path / 'MCP_SETUP.md').exists()
