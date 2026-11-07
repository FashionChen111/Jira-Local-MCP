"""The runtime SDK boundary always authenticates using the configured PAT."""
import jira
from core import jira_client


def test_runtime_client_uses_pat_only(cfg, monkeypatch):
    expected = object()
    def factory(**kwargs):
        assert kwargs == {
            'options': {'server': 'https://jira.example.com', 'verify': True},
            'token_auth': 'SECRET_TOKEN', 'timeout': 30,
        }
        return expected
    monkeypatch.setattr(jira, 'JIRA', factory)
    assert jira_client._build_client() is expected
