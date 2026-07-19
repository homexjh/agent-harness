"""ToolResultStore 单测：执行层外置（对齐 WorkBuddy ToolResultBlobService / QwenPaw ToolResultLimiter）。

零外部依赖：使用临时目录，不触碰真实 ~/.agent-harness。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from harness.tool_result_store import (
    EXTERNALIZE_MARKER,
    ToolResultStore,
    extract_token,
    is_externalized_placeholder,
    make_placeholder,
)

_ROOT = Path(tempfile.mkdtemp(prefix="trs-test-"))


def _store(kb=50):
    return ToolResultStore(root=_ROOT / "tr", threshold_kb=kb)


def test_should_externalize_threshold():
    s = _store(kb=1)  # 1KB = 1024 chars
    assert s.should_externalize("x" * 2000) is True
    assert s.should_externalize("x" * 500) is False


def test_externalize_roundtrip():
    s = _store(kb=1)
    big = "RESULT-" + "y" * 2000
    token = s.externalize(big, thread_id="t1", tool_name="web_search")
    assert token.startswith("t1/")
    assert token.endswith(".txt")
    # 文件确实落在 <root>/t1/ 下
    assert (s.root / "t1").is_dir()
    # recall 还原全文，零丢失
    assert s.recall(token) == big


def test_externalize_keeps_full_no_truncation():
    s = _store(kb=1)
    content = "BEGIN" + "z" * 3000 + "END"
    token = s.externalize(content, thread_id="t1", tool_name="exec")
    restored = s.recall(token)
    assert restored.startswith("BEGIN") and restored.endswith("END")
    assert "zzz" in restored


def test_thread_isolation():
    s = _store(kb=1)
    s.externalize("aaa", thread_id="threadA", tool_name="x")
    s.externalize("bbb", thread_id="threadB", tool_name="x")
    a_hits = s.search_thread("threadA", "aaa")
    b_hits = s.search_thread("threadB", "aaa")
    assert len(a_hits) == 1
    assert len(b_hits) == 0  # 不同 thread 互不可见


def test_search_thread_keyword():
    s = _store(kb=1)
    s.externalize("the quick brown fox", thread_id="t1", tool_name="x")
    hits = s.search_thread("t1", "brown")
    assert len(hits) == 1
    assert "brown" in hits[0][1]


def test_recall_missing_token_safe():
    s = _store(kb=1)
    assert s.recall("nope/nope.txt") is None  # 缺失不抛错


def test_recall_path_traversal_blocked():
    s = _store(kb=1)
    # 尝试用 ../ 逃逸 root
    evil = s.recall("../../etc/passwd")
    assert evil is None


def test_placeholder_roundtrip_token():
    token = "t1/abc_web_search_123.txt"
    ph = make_placeholder("web_search", token, 12345)
    assert EXTERNALIZE_MARKER in ph
    assert is_externalized_placeholder(ph) is True
    assert extract_token(ph) == token
    assert is_externalized_placeholder("normal text") is False
    assert extract_token("normal text") is None
