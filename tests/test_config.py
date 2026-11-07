"""Configuration contracts and .env validation, without real credentials."""
import os
import pytest
import config


def write_config(tmp_path, extra='', base='ATLASSIAN_JIRA_URL=https://jira.example.com/context/\nATLASSIAN_JIRA_PAT=SECRET\n'):
    path = tmp_path / '.env'
    path.write_text(base + extra, encoding='utf-8')
    return path


def test_load_config_derives_sqlite_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'BASE_DIR', str(tmp_path))
    cfg = config.load_config(str(write_config(tmp_path)))
    assert cfg.jira.url == 'https://jira.example.com/context'
    assert cfg.jira.token == 'SECRET'
    assert cfg.jira.verify_ssl is True
    assert cfg.jira.timeout == 30
    assert cfg.web_port == 8766
    assert cfg.max_txt_bytes == 262144
    assert cfg.operations_db_file == str(tmp_path / 'data/operations.sqlite3')
    assert cfg.mcp_logs_db_file == str(tmp_path / 'data/mcp_logs.sqlite3')
    assert not hasattr(cfg, 'data_dir')
    assert not hasattr(cfg, 'queue_file') and not hasattr(cfg, 'commit_log_file')


def test_pat_and_advanced_values(tmp_path):
    path = write_config(tmp_path, 'ATLASSIAN_JIRA_PAT="p # $X"\nATLASSIAN_JIRA_VERIFY_SSL=false\nATLASSIAN_JIRA_TIMEOUT_SECONDS=45\nJIRA_MCP_WEB_PORT=9000\nJIRA_MCP_MAX_TXT_BYTES=512\n')
    cfg = config.load_config(str(path))
    assert cfg.jira.token == 'p # $X'
    assert cfg.jira.verify_ssl is False
    assert (cfg.jira.timeout, cfg.web_port, cfg.max_txt_bytes) == (45, 9000, 512)


@pytest.mark.parametrize('item', [
    'ATLASSIAN_JIRA_URL=', 'ATLASSIAN_JIRA_URL=ftp://host',
    'ATLASSIAN_JIRA_URL=https://user:SECRET@host', 'ATLASSIAN_JIRA_URL=https://host?x=SECRET',
    'ATLASSIAN_JIRA_URL=https://host/#SECRET', 'ATLASSIAN_JIRA_AUTH_TYPE=other',
    'ATLASSIAN_JIRA_PAT=', 'ATLASSIAN_JIRA_AUTH_TYPE=basic',
    'ATLASSIAN_JIRA_VERIFY_SSL=maybe', 'ATLASSIAN_JIRA_TIMEOUT_SECONDS=0',
    'ATLASSIAN_JIRA_TIMEOUT_SECONDS=1.5', 'JIRA_MCP_WEB_PORT=65536',
    'JIRA_MCP_WEB_PORT=-1', 'JIRA_MCP_MAX_TXT_BYTES=0',
])
def test_invalid_values_name_key_without_exposing_value(tmp_path, item):
    with pytest.raises(ValueError) as error:
        config.load_config(str(write_config(tmp_path, item + '\n')))
    assert item.split('=')[0] in str(error.value) or 'ATLASSIAN_JIRA_USERNAME' in str(error.value)
    assert 'SECRET' not in str(error.value)


def test_default_file_is_independent_of_cwd_and_environment(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.setattr(config, 'CONFIG_PATH', str(path))
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv('ATLASSIAN_JIRA_URL', 'https://wrong.example.com')
    assert config.load_config().jira.url == 'https://jira.example.com/context'


def test_missing_env_does_not_fall_back_to_yaml(tmp_path):
    (tmp_path / 'config.yaml').write_text('jira: {url: https://host, token: SECRET}', encoding='utf-8')
    with pytest.raises(FileNotFoundError, match='setup'):
        config.load_config(str(tmp_path / '.env'))


def test_config_repr_and_sensitive_values_do_not_expose_credentials(cfg):
    assert 'SECRET_TOKEN' not in repr(cfg)
    assert 'jira.example.com' in cfg.sensitive_values()


def test_existing_pat_selector_remains_readable(tmp_path):
    assert config.load_config(str(write_config(tmp_path, 'ATLASSIAN_JIRA_AUTH_TYPE=pat\n'))).jira.token == 'SECRET'


def test_basic_selector_is_rejected_even_when_all_credentials_are_present(tmp_path):
    path = write_config(tmp_path, 'ATLASSIAN_JIRA_AUTH_TYPE=basic\nATLASSIAN_JIRA_USERNAME=user\nATLASSIAN_JIRA_PASSWORD=PASSWORD_SECRET\n')
    with pytest.raises(ValueError, match='PAT') as caught:
        config.load_config(str(path))
    assert 'SECRET' not in str(caught.value)
