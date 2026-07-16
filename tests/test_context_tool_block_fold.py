"""验证上下文折叠时不会破坏 assistant tool_calls 与 ToolMessage 的配对。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from harness.context.manager import ContextManager
from harness.context.store import TurnStore


def _count(text: str) -> int:
    return max(1, len(text) // 4)


def _make_cm(budget: int):
    return ContextManager(
        budget_tokens=budget,
        count_tokens=_count,
        store=TurnStore(":memory:"),
        allow_unsandboxed_recall=False,
        strip_media=False,
        max_tool_result_chars=0,
    )


def test_tool_call_block_not_split():
    """budget 只够保留最新 user 时，应把包含 assistant tool_calls 的整个 block 一起 fold，
    不能只保留一半（否则会出现 assistant tool_calls 无 ToolMessage 或 ToolMessage 无 tool_calls）。"""
    cm = _make_cm(budget=30)

    raw = [
        HumanMessage(content="search typhoon"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "search", "args": {"q": "typhoon"}}
            ],
        ),
        ToolMessage(content="result one very long content" * 10, tool_call_id="call_1"),
        HumanMessage(content="next question"),
    ]
    window, cstate = cm.prepare(raw, "t1")
    assert cstate["fold_count"] > 0, "should fold something with low budget"

    types = [m.type for m in window]
    # 不能孤立保留任何 ToolMessage
    for i, m in enumerate(window):
        if m.type == "tool":
            prev = window[i - 1] if i > 0 else None
            assert prev is not None and prev.type == "ai", "ToolMessage kept without preceding AI tool_calls"
    # 不能保留 assistant tool_calls 而缺少对应的 ToolMessage
    for i, m in enumerate(window):
        if m.type == "ai" and getattr(m, "tool_calls", None):
            ids = {tc.get("id") for tc in m.tool_calls}
            found = set()
            for j in range(i + 1, len(window)):
                if window[j].type == "tool":
                    found.add(getattr(window[j], "tool_call_id", None))
                elif window[j].type in ("human", "ai", "system"):
                    break
            assert ids.issubset(found), "assistant tool_calls kept without corresponding ToolMessages"
    # 最终保留的应该只有最新 human（+ 可能插入的 system_hint）
    assert types[-1] == "human"


def test_tool_call_block_kept_together():
    """预算足够时整个 block 被保留。"""
    cm = _make_cm(budget=1000)
    raw = [
        HumanMessage(content="search typhoon"),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_1", "name": "search", "args": {"q": "typhoon"}}
            ],
        ),
        ToolMessage(content="result one", tool_call_id="call_1"),
        HumanMessage(content="next question"),
    ]
    window, folded = cm.prepare(raw, "t2")
    types = [m.type for m in window]
    assert types.count("ai") == 1
    assert types.count("tool") == 1
    assert types.count("human") == 2


if __name__ == "__main__":
    test_tool_call_block_kept_together()
    test_tool_call_block_not_split()
    print("OK")
