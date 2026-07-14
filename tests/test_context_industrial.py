"""上下文工业化能力单测：媒体剥离 / 工具结果剪枝 / 检视 inspect / 受控召回。

零外部依赖（内存 store，不需网络）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import HumanMessage

from harness.context.manager import ContextManager
from harness.stability.observability import Metrics
from harness.tools_node import prune_tool_result


# --------------------------------------------------------------------------- #
# 媒体剥离
# --------------------------------------------------------------------------- #
def test_strip_base64_data_uri():
    m = Metrics()
    cm = ContextManager(db_path=":memory:", metrics=m)
    b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA"
    msgs = [HumanMessage(content=f"看这张图 {b64} 谢谢")]
    window, _ = cm.prepare(msgs, "t-media")
    joined = " ".join(x.content for x in window if isinstance(x.content, str))
    assert "base64" not in joined
    assert "[media stripped]" in joined
    assert m.media_stripped >= 1


def test_strip_multimodal_parts():
    m = Metrics()
    cm = ContextManager(db_path=":memory:", metrics=m)
    msgs = [
        HumanMessage(
            content=[
                {"type": "text", "text": "描述这张图"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ]
        )
    ]
    cm.prepare(msgs, "t-mm")
    assert m.media_stripped >= 1


# --------------------------------------------------------------------------- #
# 工具结果剪枝
# --------------------------------------------------------------------------- #
def test_prune_truncates_and_counts():
    m = Metrics()
    big = "A" * 10000
    out = prune_tool_result(big, 100, metrics=m, tool="exec")
    assert len(out) < len(big)
    assert "截断" in out
    assert "10000" in out  # 保留原始长度信息
    assert m.tool_pruned == 1


def test_prune_noop_when_small():
    m = Metrics()
    out = prune_tool_result("short", 100, metrics=m, tool="x")
    assert out == "short"
    assert m.tool_pruned == 0


# --------------------------------------------------------------------------- #
# 检视 inspect
# --------------------------------------------------------------------------- #
def test_inspect_reports_turns_and_fold():
    cm = ContextManager(db_path=":memory:", budget_tokens=20)  # 极小预算强制折叠
    msgs = [HumanMessage(content="x" * 200) for _ in range(5)]
    cm.prepare(msgs, "t-inspect")
    info = cm.inspect("t-inspect")
    assert info["total_turns"] == 5
    assert info["budget_tokens"] == 20
    assert len(info["turns"]) == 5
    # 极小预算下应有折叠
    assert len(info["folded_seqs"]) >= 1


# --------------------------------------------------------------------------- #
# 受控召回
# --------------------------------------------------------------------------- #
def test_recall_disabled_by_default():
    cm = ContextManager(db_path=":memory:", allow_unsandboxed_recall=False)
    cm.prepare([HumanMessage(content="secret keyword apple")], "t-r")
    out = cm.recall("apple", "t-r")
    assert "disabled" in out.lower()


def test_recall_restores_when_enabled():
    m = Metrics()
    cm = ContextManager(
        db_path=":memory:", allow_unsandboxed_recall=True, metrics=m
    )
    cm.prepare([HumanMessage(content="the magic keyword is banana here")], "t-r2")
    out = cm.recall("banana", "t-r2")
    assert "banana" in out
    assert m.recalls == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("ALL CONTEXT INDUSTRIAL TESTS PASSED")
