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
