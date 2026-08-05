"""Delete defence, placeholder chaining, and advisory safety contracts."""
from __future__ import annotations

import pytest

from core import risk_engine
from knowledge.prompts import PROMPTS


@pytest.mark.parametrize("operation", ["delete_issue", "remove_comment", "purge"])
def test_unknown_or_delete_operation_rejected(operation):
    with pytest.raises(risk_engine.RiskError):
        risk_engine.assert_not_delete(operation, {})


@pytest.mark.parametrize("payload", [{"remove": True}, {"value": "delete this"}, {"nested": ["删除"]}])
def test_payload_delete_semantics_rejected(payload):
    with pytest.raises(risk_engine.RiskError):
        risk_engine.assert_not_delete("add_comment", payload)


@pytest.mark.parametrize("key", [None, "$TASK{x}", "{new}", "bad", "PROJ-x"])
def test_modify_requires_real_looking_issue_key(key):
    with pytest.raises(risk_engine.RiskError):
        risk_engine.validate_operation_target("add_comment", key)


def test_create_does_not_require_issue_key():
    risk_engine.validate_operation_target("create_issue", None)


def test_prompts_publish_v2_tool_names_and_untrusted_data_contract():
    combined = "\n".join(PROMPTS.values())
    assert "jira_show_queue" in combined and "jira_get_execution_log" in combined
    assert "jira_show_pending_queue" not in combined and "jira_get_commit_log" not in combined
    assert "不可信业务数据" in combined
    assert "仅入队、尚未执行" in combined
    assert "Web/OS 人工确认" in combined
