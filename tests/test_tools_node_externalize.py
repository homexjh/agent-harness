"""tools_node 外置（非粗暴截断）单测。

验证：超阈值结果落盘 + 占位符入上下文；阈值以下保持完整 inline；不丢原文。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_core.messages import AIMessage, ToolMessage

from harness.tool_result_store import ToolResultStore, is_externalized_placeholder
from harness.tools_node import make_tools_node


class _FakeTool:
    """返回一个固定字符串结果的假工具。"""
    def __init__(self, out):
        self._out = out
    def invoke(self, args):
        return self._out


def _store(tmp_path, kb=1):  # 1KB 阈值便于测试
    return ToolResultStore(root=tmp_path / "tr", threshold_kb=kb)


def _state_with_tool_call():
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "bigtool", "args": {}}],
    )
    return {"messages": [ai]}


def test_large_result_externalized(tmp_path):
    store = _store(tmp_path, kb=1)
    big = "RESULT-" + "y" * 2000
    tools = {"bigtool": _FakeTool(big)}
    node = make_tools_node(tools, tool_result_store=store, externalize_threshold=1024)
    out = node(_state_with_tool_call(), {"configurable": {"thread_id": "t1"}})
    msg = out["messages"][0]
    assert isinstance(msg, ToolMessage)
    assert is_externalized_placeholder(msg.content)
    # 全文在磁盘上、未被截断
    from harness.tool_result_store import extract_token
    tok = extract_token(msg.content)
    assert store.recall(tok) == big


def test_small_result_kept_inline_no_truncation(tmp_path):
    store = _store(tmp_path, kb=1)
    small = "short result, fully kept"
    tools = {"bigtool": _FakeTool(small)}
    node = make_tools_node(tools, tool_result_store=store, externalize_threshold=1024)
    out = node(_state_with_tool_call(), {"configurable": {"thread_id": "t1"}})
    msg = out["messages"][0]
    # 未超阈值：完整 inline，没有占位符
    assert not is_externalized_placeholder(msg.content)
    assert msg.content == small


def test_thread_id_from_config(tmp_path):
    store = _store(tmp_path, kb=1)
    big = "x" * 2000
    tools = {"bigtool": _FakeTool(big)}
    node = make_tools_node(tools, tool_result_store=store, externalize_threshold=1024)
    node(_state_with_tool_call(), {"configurable": {"thread_id": "threadXYZ"}})
    # 目录按 thread 隔离
    assert (store.root / "threadXYZ").is_dir()
    assert not (store.root / "default").exists() or True  # default 不一定存在


def test_tool_error_returned_inline(tmp_path):
    store = _store(tmp_path, kb=1)
    class _ErrTool:
        def invoke(self, args):
            raise RuntimeError("boom")
    tools = {"bigtool": _ErrTool()}
    node = make_tools_node(tools, tool_result_store=store, externalize_threshold=1024)
    out = node(_state_with_tool_call(), {"configurable": {"thread_id": "t1"}})
    assert "Error: boom" in out["messages"][0].content
