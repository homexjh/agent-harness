"""上下文管理单元测试（零依赖，独立于 langgraph/langchain 运行时）。"""
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from src.harness.context import ContextManager


def _long(text_repeat: int, n: int):
    msgs = [HumanMessage(content="start")]
    for i in range(n):
        msgs.append(HumanMessage(content=f"需求{i}：" + ("背景信息，" * text_repeat)))
    return msgs


# 单条约 500 token，整体远超预算 1500，确保触发折叠
_LONG = (500, 6)


def test_fold_triggers_under_budget():
    cm = ContextManager(budget_tokens=1500)
    raw = _long(*_LONG)
    window, cstate = cm.prepare(raw, "t1")
    assert cstate["fold_count"] > 0, "预算较小应触发折叠"
    assert any(
        isinstance(m, SystemMessage) and "[CONTEXT FOLD]" in (m.content or "")
        for m in window
    )
    # 折叠后窗口应明显小于原始
    assert len(window) < len(raw)


def test_recall_restores_folded_content():
    cm = ContextManager(budget_tokens=1500, allow_unsandboxed_recall=True)
    raw = _long(*_LONG)
    cm.prepare(raw, "t2")
    restored = cm.recall("需求0")
    assert "需求0" in restored
    assert "背景信息" in restored  # 全文还原，零丢失


def test_unsandboxed_recall_blocked_by_default():
    cm = ContextManager(budget_tokens=1500)  # allow_unsandboxed_recall 默认 False
    cm.prepare(_long(*_LONG), "t3")
    out = cm.recall("需求0")
    assert "disabled" in out, "未开启时应拒绝无沙箱召回"


def test_latest_message_never_folded():
    # 单条超大消息也须保留在窗口（保底可见）
    cm = ContextManager(budget_tokens=10)
    huge = HumanMessage(content="X" * 100000)
    window, _ = cm.prepare([huge], "t4")
    assert huge.content in [m.content for m in window]


def test_system_hint_preserved_when_fold_stub_present():
    """长会话触发折叠后，system_hint 必须仍然作为第一条 SystemMessage，不能被 fold stub 顶替。"""
    cm = ContextManager(budget_tokens=1500)
    raw = _long(*_LONG)
    system_hint = "Environment Context: Current local time: 2026-07-19 17:19:00 +0800"
    window, cstate = cm.prepare(raw, "t6", system_hint=system_hint)
    assert cstate["fold_count"] > 0, "应触发折叠"
    assert isinstance(window[0], SystemMessage), "首条必须是 SystemMessage"
    assert system_hint in window[0].content, "系统提示必须保留在第一的位置"
    assert "[CONTEXT FOLD]" not in window[0].content, "fold stub 不能占据第一条"
    # 折叠桩应位于系统提示之后
    stubs = [m for m in window[1:] if isinstance(m, SystemMessage) and "[CONTEXT FOLD]" in (m.content or "")]
    assert len(stubs) == 1, "应保留且仅保留一个折叠桩，并排在系统提示之后"


def test_recall_does_not_duplicate_on_reprepare():
    cm = ContextManager(budget_tokens=1500, allow_unsandboxed_recall=True)
    raw = _long(*_LONG)
    cm.prepare(raw, "t5")
    cm.prepare(raw, "t5")  # 再次 prepare 应幂等，不重复落库
    # store 里应仍为原始条数（不翻倍）
    assert cm._store.count("t5") == len(raw)
