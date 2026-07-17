"""ContextManager 对齐 QwenPaw Scroll Context 的测试：
1. reserve 保留区：最近窗口永不被折
2. hard_stop 硬停兜底
3. 地图桩含被折区段 seq 范围
4. 精确 seq recall 还原全文
5. 语义召回（mock embedding）top-k 命中
6. keyword recall 回退
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import HumanMessage, SystemMessage, message_to_dict

from harness.context.manager import ContextManager, FOLD_STUB_MARKER


def _msgs(n, start=1, size_tokens=200):
    """构造 n 条人类消息，每条约 size_tokens 字符，seq 从 start 递增。"""
    out = []
    for i in range(start, start + n):
        text = f"这是第 {i} 轮对话内容。" + "字" * (size_tokens * 4)
        out.append({"seq": i, "msg": message_to_dict(HumanMessage(content=text))})
    return out


class _FakeEmbed:
    """极简 embedding：把文本哈希成 4 维向量，便于语义召回测试。"""

    def __init__(self):
        self.calls = 0

    def is_enabled(self):
        return True

    def embed(self, texts):
        self.calls += 1
        vecs = []
        for t in texts:
            h = hash(t) % 1000
            vecs.append([float((h >> s) & 1) for s in range(4)])
        return vecs


def test_reserve_zone_not_folded():
    # 预算很小，但 reserve 区应保护最近的消息
    cm = ContextManager(budget_tokens=400, reserve_ratio=0.5, allow_unsandboxed_recall=True)
    # 每条 ~63 token，便于 reserve 区明显容纳多条最近消息
    items = _msgs(10, 1, 60)
    window, folded = cm._fold(items)
    # reserve_tokens = 400*0.5 = 200，应容纳最近约 3 条(seq 10/9/8)；软预算再扩到 seq 6。
    # 断言：最近的 2 条(seq 10/9)必在保留区（永不被折）。
    assert 10 not in folded and 9 not in folded, f"保留区未保护最近消息: folded={folded}"
    # 且最近窗口(6..11)中至少 2 条未被折
    kept_recent = {it["seq"] for it in items if it["seq"] not in folded and it["seq"] >= 6}
    assert len(kept_recent) >= 2, f"保留区/软预算未容纳足够最近消息: kept_recent={kept_recent}"


def test_hard_stop_enforced():
    # 极端配置：软预算巨大但硬停很小，验证硬停兜底
    cm = ContextManager(
        budget_tokens=10_000_000, reserve_ratio=0.0,
        hard_stop_tokens=500, allow_unsandboxed_recall=True,
    )
    items = _msgs(20)  # ~4000 token
    window, _ = cm._fold(items)
    total = sum(cm._count(m.content) if isinstance(m.content, str) else 0 for m in window)
    assert total <= 550, f"硬停未生效: window={total}"


def test_map_stub_lists_segments():
    cm = ContextManager(budget_tokens=300, reserve_ratio=0.0, allow_unsandboxed_recall=True)
    items = _msgs(6)
    window, folded = cm._fold(items)
    assert folded, "应有被折 seq"
    stub = [m for m in window if isinstance(m, SystemMessage) and FOLD_STUB_MARKER in m.content]
    assert stub, "应生成折叠地图桩"
    assert "turns" in stub[0].content
    assert "segment" in stub[0].content.lower() or "msgs" in stub[0].content


def test_exact_seq_recall():
    cm = ContextManager(budget_tokens=300, reserve_ratio=0.0, allow_unsandboxed_recall=True)
    cm.set_active_thread("t1")
    cm._store.append("t1", 1, HumanMessage(content="独特的上海天气追踪内容abcxyz"))
    cm._eviction_index = {1: {"headline": "上海", "tokens": 10, "type": "human"}}
    out = cm.recall(seq=1)
    assert "上海天气追踪内容abcxyz" in out, f"精确 seq recall 失败: {out}"


def test_semantic_recall_topk():
    fake = _FakeEmbed()
    cm = ContextManager(
        budget_tokens=300, reserve_ratio=0.0, embedding_client=fake,
        enable_semantic_recall=True, allow_unsandboxed_recall=True,
    )
    cm.set_active_thread("t2")
    cm._store.append("t2", 3, HumanMessage(content="苹果公司发布新产品"))
    cm._store.append("t2", 4, HumanMessage(content="篮球比赛昨晚结束"))
    # 模拟折叠发生：把两块登记进 eviction index（含 embedding）
    cm._eviction_index = {
        3: {"headline": "苹果公司发布新产品", "tokens": 12, "type": "human", "embedding": [1.0, 0.0, 0.0, 0.0]},
        4: {"headline": "篮球比赛昨晚结束", "tokens": 12, "type": "human", "embedding": [0.0, 1.0, 0.0, 0.0]},
    }
    out = cm.recall(query="iPhone 和库克")
    assert "苹果公司" in out, f"语义召回未命中苹果相关块: {out}"
    assert "[score=" in out, "语义召回应返回 score"


def test_keyword_recall_fallback():
    cm = ContextManager(
        budget_tokens=300, reserve_ratio=0.0, embedding_client=None,
        allow_unsandboxed_recall=True,
    )
    cm.set_active_thread("t3")
    cm._store.append("t3", 7, HumanMessage(content="深圳这座城市很有活力"))
    cm._eviction_index = {7: {"headline": "深圳", "tokens": 8, "type": "human"}}
    out = cm.recall(query="深圳")
    assert "深圳这座城市" in out, f"keyword recall 失败: {out}"
