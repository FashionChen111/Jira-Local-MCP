"""Health checks exercise only Jira identity reads and always release the session."""
import pytest
from jira import JIRAError
from requests.exceptions import SSLError, Timeout, ConnectionError
from config import JiraConfig
import healthcheck


class IdentityClient:
    def __init__(self, identity=None, error=None):
        self.identity = {'name': 'user', 'key': 'JIRAUSER1', 'active': True} if identity is None else identity
        self.error = error
        self.closed = False
        self.reads = 0

    def myself(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.identity

    def close(self):
        self.closed = True


@pytest.mark.parametrize('auth_type', ['pat', 'basic'])
def test_identity_check_uses_readonly_options_and_closes(monkeypatch, auth_type):
    client = IdentityClient()
    def factory(**kwargs):
        assert kwargs['get_server_info'] is False
        assert kwargs['max_retries'] == 0
        assert kwargs['options'] == {'server': 'https://jira.example.com', 'verify': True}
        assert kwargs['timeout'] == 30
        if auth_type == 'pat':
            assert kwargs['token_auth'] == 'SECRET'
            assert kwargs['basic_auth'] is None
        else:
            assert kwargs['basic_auth'] == ('user', 'SECRET')
            assert kwargs['token_auth'] is None
        return client
    monkeypatch.setattr(healthcheck, 'JIRA', factory)
    healthcheck.verify_jira_connection(JiraConfig(url='https://jira.example.com', auth_type=auth_type, token='SECRET', username='user', password='SECRET'))
    assert client.reads == 1
    assert client.closed


@pytest.mark.parametrize('identity', [{}, {'displayName': 'User'}, {'name': 'anonymous'}, {'key': 'JIRAUSER1', 'active': False}, [], None])
def test_unconfirmed_identity_fails_and_closes(monkeypatch, identity):
    client = IdentityClient(); client.identity = identity
    monkeypatch.setattr(healthcheck, 'JIRA', lambda **kwargs: client)
    with pytest.raises(healthcheck.JiraHealthcheckError):
        healthcheck.verify_jira_connection(JiraConfig(url='https://jira.example.com', auth_type='pat', token='SECRET'))
    assert client.closed


@pytest.mark.parametrize('error, category', [
    (JIRAError(status_code=401, text='SECRET'), '认证'),
    (JIRAError(status_code=403, text='SECRET'), '权限'),
    (SSLError('SECRET'), '证书'), (Timeout('SECRET'), '超时'),
    (ConnectionError('SECRET'), '连接'), (ValueError('SECRET'), '响应'),
])
def test_error_is_classified_without_secret_and_client_closed(monkeypatch, error, category):
    client = IdentityClient(error=error)
    monkeypatch.setattr(healthcheck, 'JIRA', lambda **kwargs: client)
    with pytest.raises(healthcheck.JiraHealthcheckError) as caught:
        healthcheck.verify_jira_connection(JiraConfig(url='https://jira.example.com', auth_type='pat', token='SECRET'))
    assert category in str(caught.value)
    assert 'SECRET' not in str(caught.value)
    assert client.closed


def test_constructor_failure_is_sanitized(monkeypatch):
    def fail(**kwargs):
        raise ConnectionError('SECRET')
    monkeypatch.setattr(healthcheck, 'JIRA', fail)
    with pytest.raises(healthcheck.JiraHealthcheckError, match='连接') as caught:
        healthcheck.verify_jira_connection(JiraConfig(url='https://jira.example.com', auth_type='pat', token='SECRET'))
    assert 'SECRET' not in str(caught.value)
