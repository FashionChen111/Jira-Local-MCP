"""风险引擎测试：分级、两阶段校验、删除拦截。"""
from __future__ import annotations

import pytest

from core import risk_engine
from core.risk_engine import RiskError


def test_classify_levels():
    assert risk_engine.classify_risk("add_comment", {})[0] == "low"
    assert risk_engine.classify_risk("create_issue", {})[0] == "medium"
    assert risk_engine.classify_risk("move_issue", {})[0] == "high"
    assert risk_engine.classify_risk("transition_issue", {"transition_name": "关闭"})[0] == "high"


def test_assert_not_delete_blocks_unknown_op():
    with pytest.raises(RiskError):
        risk_engine.assert_not_delete("delete_issue", {})


def test_assert_not_delete_blocks_delete_payload():
    with pytest.raises(RiskError):
        risk_engine.assert_not_delete("update_field", {"fields": {"action": "delete all"}})


def test_assert_not_delete_allows_normal():
    # 不抛异常即通过
    risk_engine.assert_not_delete("add_comment", {"body": "hello"})


def _mk(op, key):
    class T:
        operation_type = op
        issue_key = key
        risk_level = "low"
    return T()


def test_two_phase_rejects_modify_after_create():
    batch = [_mk("create_issue", None)]
    with pytest.raises(RiskError):
        risk_engine.check_two_phase("add_comment", None, batch)
    with pytest.raises(RiskError):
        risk_engine.check_two_phase("add_comment", "$TASK{x}", batch)


def test_two_phase_allows_real_key():
    batch = [_mk("create_issue", None)]
    # 已存在真实 key 的修改允许（针对既有事务，非本批次新建）
    risk_engine.check_two_phase("add_comment", "PROJ-123", batch)


def test_two_phase_no_create_no_restriction():
    batch = [_mk("add_comment", "PROJ-1")]
    risk_engine.check_two_phase("add_comment", None, batch)  # 无 create 不约束
