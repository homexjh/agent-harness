"""ContextManager 分层裁剪 + 外置结果 recall 单测。

验证：
- 近期/早期工具结果按 recent/old 分层截断（窗口截断但 store 留全文可 recall）；
- 被外置落盘的工具结果，recall(seq=) / recall(token) 能从磁盘取回完整全文；
- recall(keyword) 在 store 无命中时回退搜索外置 blob。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from harness.context.manager import ContextManager
from harness.tool_result_store import (
    ToolResultStore,
    extract_token,
    make_placeholder,
)


def _cm(tmp_path, **kw):
    store = ToolResultStore(root=tmp_path / "tr", threshold_kb=1)
    opts = dict(
        db_path=":memory:",
        allow_unsandboxed_recall=True,
        tool_result_store=store,
        budget_tokens=1_000_000,  # 预算足够，不折叠，专测工具结果裁剪
        strip_media=False,
    )
    opts.update(kw)
    return ContextManager(**opts), store


def test_layered_recent_vs_old():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cm, _ = _cm(Path(d), recent_tool_result_chars=50, old_tool_result_chars=10, recent_tool_window=1)
        # 3 个工具结果（inline，未外置），内容均 100 字符
        blocks = []
        for ch in ("A", "B", "C"):
            blocks += [
                HumanMessage(content=f"q{ch}"),
                AIMessage(content="", tool_calls=[{"id": f"c{ch}", "name": "t", "args": {}}]),
                ToolMessage(content=ch * 100, tool_call_id=f"c{ch}"),
            ]
        window, _ = cm.prepare(blocks, "t1")
        tool_msgs = [m for m in window if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 3
        # 仅最后一个（recent）保留 50 原文，前两个（old）只留 10 原文；
        # 截断处追加提示后缀，故实际长度 = 上限 + 后缀。
        assert tool_msgs[-1].content.startswith("C" * 50)
        assert "truncated to 50" in tool_msgs[-1].content
        assert tool_msgs[0].content.startswith("A" * 10)
        assert tool_msgs[1].content.startswith("B" * 10)
        assert "truncated to 10" in tool_msgs[0].content
        # store 仍留全文 → recall(seq) 还原
        out = cm.recall(seq=3, thread_id="t1")
        assert "AAA" in out  # seq=3 是第一个工具（human=1, ai=2, tool=3）


def test_externalized_recall_by_seq():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cm, store = _cm(Path(d))
        big = "EXTERNALIZED-CONTENT-" + "z" * 2000
        token = store.externalize(big, thread_id="t1", tool_name="web_search")
        placeholder = make_placeholder("web_search", token, len(big))
        raw = [
            HumanMessage(content="search something"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "web_search", "args": {}}]),
            ToolMessage(content=placeholder, tool_call_id="c1"),
        ]
        cm.prepare(raw, "t1")
        # seq=3 是 tool 消息（human=1, ai=2, tool=3）
        out = cm.recall(seq=3, thread_id="t1")
        assert "EXTERNALIZED-CONTENT-" in out
        assert "zzz" in out  # 全文还原，未截断


def test_externalized_recall_by_token():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cm, store = _cm(Path(d))
        big = "TOKEN-RESTORE-" + "q" * 1500
        token = store.externalize(big, thread_id="t1", tool_name="exec")
        out = cm.recall(token, thread_id="t1")
        assert out is not None and "TOKEN-RESTORE-" in out


def test_externalized_recall_by_keyword_fallback():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cm, store = _cm(Path(d))
        big = "UNIQUE_KEYWORD_XYZ " + "w" * 1500
        token = store.externalize(big, thread_id="t1", tool_name="web_search")
        placeholder = make_placeholder("web_search", token, len(big))
        raw = [
            HumanMessage(content="search"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "web_search", "args": {}}]),
            ToolMessage(content=placeholder, tool_call_id="c1"),
        ]
        cm.prepare(raw, "t1")
        # store 占位符不含 "UNIQUE_KEYWORD_XYZ" 正文，但落盘文件含 → 走外置 blob 回退
        out = cm.recall("UNIQUE_KEYWORD_XYZ", thread_id="t1")
        assert "UNIQUE_KEYWORD_XYZ" in out


def test_externalized_placeholder_not_double_pruned():
    import tempfile
    from harness.tool_result_store import is_externalized_placeholder
    with tempfile.TemporaryDirectory() as d:
        cm, store = _cm(Path(d), recent_tool_result_chars=10, old_tool_result_chars=5, recent_tool_window=1)
        big = "PH-" + "x" * 2000
        token = store.externalize(big, thread_id="t1", tool_name="web_search")
        placeholder = make_placeholder("web_search", token, len(big))
        raw = [
            HumanMessage(content="s"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "web_search", "args": {}}]),
            ToolMessage(content=placeholder, tool_call_id="c1"),
        ]
        window, _ = cm.prepare(raw, "t1")
        tool = [m for m in window if isinstance(m, ToolMessage)][0]
        # 占位符本身很短，不应被分层裁剪动到
        assert is_externalized_placeholder(tool.content)
        assert extract_token(tool.content) == token
