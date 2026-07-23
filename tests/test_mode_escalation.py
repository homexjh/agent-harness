"""反应式意图升级守卫（ModeEscalationGuardian）单元测试。

核心断言：无论用户怎么措辞，只要模型在 chat 下尝试调用重工具（exec/write_file/
edit_file/desktop_screenshot），守卫就放行执行并升级会话到 coding；轻工具不受影响；
用户手动锁定 chat 时拒绝重工具以尊重选择。
"""
from __future__ import annotations

from src.harness.security.guard import (
    ModeEscalationGuardian,
    set_escalation_thread,
    set_request_mode,
    set_request_locked,
    pop_escalation,
    clear_request,
)

_TID = "test-thread-1"


def _setup(mode: str, locked: bool):
    clear_request(_TID)
    set_escalation_thread(_TID)
    set_request_mode(_TID, mode)
    set_request_locked(_TID, locked)
    return ModeEscalationGuardian()


def _teardown():
    set_escalation_thread(None)
    clear_request(_TID)


def test_chat_auto_heavy_tool_escalates():
    g = _setup("chat", locked=False)
    r = g.check("write_file", {"path": "a.py", "content": "x=1"})
    assert r.allowed is True
    # 会话被标记为 coding，且升级信号已回写
    from src.harness.security.guard import _mode_by_thread, _esc_by_thread

    assert _mode_by_thread.get(_TID) == "coding"
    assert _esc_by_thread.get(_TID) == "coding"
    _teardown()


def test_chat_auto_exec_escalates():
    g = _setup("chat", locked=False)
    r = g.check("exec", {"command": "ls"})
    assert r.allowed is True
    assert pop_escalation(_TID) == "coding"
    _teardown()


def test_coding_mode_no_escalation():
    g = _setup("coding", locked=False)
    r = g.check("write_file", {"path": "a.py", "content": "x=1"})
    assert r.allowed is True
    # coding 下直接放行，不写升级信号
    assert pop_escalation(_TID) is None
    _teardown()


def test_chat_locked_heavy_tool_denied():
    g = _setup("chat", locked=True)
    r = g.check("write_file", {"path": "a.py", "content": "x=1"})
    assert r.allowed is False
    # 锁定 chat 时不应升级
    assert pop_escalation(_TID) is None
    _teardown()


def test_chat_light_tool_no_escalation():
    g = _setup("chat", locked=False)
    r = g.check("read_file", {"path": "a.py"})
    assert r.allowed is True
    assert pop_escalation(_TID) is None
    _teardown()


def test_unknown_mode_defaults_to_chat():
    # 未设置模式（tid 未登记）时按 chat 处理；这里显式设一个非 chat 之外的值也走放行+升级
    g = _setup("chat", locked=False)
    r = g.check("edit_file", {"path": "a.py", "old": "x", "new": "y"})
    assert r.allowed is True
    assert pop_escalation(_TID) == "coding"
    _teardown()
