"""ContextManager —— fold-not-summarize 的上下文管理。

设计要点（对照 QwenPaw Scroll Context）：
- 零丢失：原始消息全文写穿到 TurnStore；上下文里被"折叠"的只是占位桩，召回即还原全文。
- 预算触发：当 raw 消息 token 总量超过 budget，从最旧往新折叠，保留最近窗口。
- 配对切分：按消息顺序折叠，单条最新消息永不被折（保底可见）。
- #5746 防御：折叠桩带 kind=fold_stub 标记，prepare 时永不二次折叠，避免把"真实召回请求"
  误判为可驱逐中段；同时显式跳过含 FOLD_STUB_MARKER 的内容，杜绝桩被当成中段再折。
- 召回沙箱门控：allow_unsandboxed_recall 双条件（env ALLOW_UNSANDBOXED_RECALL + 构造参数），
  agent 显式 recall 才还原全文，避免自动无差别召回泄露折叠区。
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Callable, Optional


def _cosine(a, b) -> float:
    """余弦相似度；维度不等或零向量返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed_client_enabled(client) -> bool:
    """鸭子检查 EmbeddingClient 是否可用（is_enabled() 为真）。"""
    if client is None:
        return False
    fn = getattr(client, "is_enabled", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return False

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)

from .store import TurnStore
from ..tool_result_store import (
    EXTERNALIZE_MARKER,
    extract_token,
    is_externalized_placeholder,
)

FOLD_STUB_MARKER = "[CONTEXT FOLD]"

# base64 data URI（图片/音视频），媒体剥离时替换为占位符省 token
_DATA_URI_RE = re.compile(r"data:(image|audio|video)/[^;]+;base64,[A-Za-z0-9+/=]+")


def _default_count(text: str) -> int:
    # 中文/英文混合的稳妥启发式：字符数 / 4 ≈ token 数
    return max(1, len(text or "") // 4)


def _content_of(msg: dict) -> str:
    data = msg.get("data", msg)
    content = data.get("content") or ""
    if isinstance(content, list):  # multimodal content parts
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content


def _msg_tokens(msg: dict, count: Callable[[str], int]) -> int:
    content = _content_of(msg)
    t = count(content)
    data = msg.get("data", msg)
    tc = data.get("tool_calls") or data.get("additional_kwargs", {}).get("tool_calls")
    if tc:
        t += count(json.dumps(tc, ensure_ascii=False)) // 4 + 50
    return t


class ContextManager:
    def __init__(
        self,
        *,
        budget_tokens: int = 8000,
        count_tokens: Optional[Callable[[str], int]] = None,
        store: Optional[TurnStore] = None,
        db_path: str = ":memory:",
        allow_unsandboxed_recall: Optional[bool] = None,
        strip_media: bool = True,
        max_tool_result_chars: int = 0,
        recent_tool_result_chars: int = 8000,
        old_tool_result_chars: int = 2000,
        recent_tool_window: int = 4,
        tool_result_store=None,
        reserve_ratio: float = 0.2,
        hard_stop_tokens: int = 300000,
        embedding_client=None,
        enable_semantic_recall: bool = True,
        metrics=None,
    ):
        self.budget_tokens = budget_tokens
        self._count = count_tokens or _default_count
        self._store = store or TurnStore(db_path)
        env = os.getenv("ALLOW_UNSANDBOXED_RECALL", "0") == "1"
        self.allow_unsandboxed_recall = (
            allow_unsandboxed_recall if allow_unsandboxed_recall is not None else env
        )
        # QwenPaw 媒体降级等效：历史 base64 媒体从上下文剥离省 token。
        self.strip_media = strip_media
        # QwenPaw ToolResultPruningMiddleware 等效：上下文层分层裁剪。
        # 旧字段 max_tool_result_chars>0 走扁平兜底；否则按 recent/old 分层（均<=0 关闭）。
        self._max_tool_result_chars = max(0, int(max_tool_result_chars))
        self._recent_tool_result_chars = max(0, int(recent_tool_result_chars))
        self._old_tool_result_chars = max(0, int(old_tool_result_chars))
        self._recent_tool_window = max(0, int(recent_tool_window))
        # 执行层外置存储（工具结果超阈值落盘），供 recall 还原全文。
        self._tr_store = tool_result_store
        # 对齐 QwenPaw 保留区 + BudgetGate 硬停
        self.reserve_ratio = max(0.0, min(1.0, float(reserve_ratio)))
        self.hard_stop_tokens = max(0, int(hard_stop_tokens))
        # 对齐 QwenPaw eviction index：被折 block 登记 seq->headline/tokens/embedding，
        # 供 recall 精确还原（简化为按 seq 的扁平索引，非多层 carry 树）。
        self._eviction_index: dict = {}
        # 语义召回：可选 EmbeddingClient（鸭子类型 is_enabled()/embed()），复用 MemoryVault 实例。
        self.embedding_client = embedding_client
        self.enable_semantic_recall = enable_semantic_recall
        self._active_thread = None
        self._metrics = metrics

    # ---- 线程上下文（供 recall 工具读取当前 thread） ----
    def set_active_thread(self, tid) -> None:
        self._active_thread = tid

    # ---- 写穿新消息（幂等：只追加 store 里还没有的） ----
    def _persist_new(self, raw_messages: list, thread_id) -> None:
        existing = self._store.count(thread_id)
        for i, msg in enumerate(raw_messages[existing:], start=existing + 1):
            self._store.append(thread_id, i, msg)

    # ---- 折叠：保留最近窗口，折最旧；必须把 assistant tool_calls 及其后续所有 ToolMessage
    # 作为一个 block 整体折叠，避免破坏配对导致模型 400。
    # 对齐 QwenPaw：① reserve_ratio 保留区（最近窗口永折不动）② hard_stop 硬停兜底
    # ③ 被折 block 登记进 _eviction_index，供 recall 精确/语义还原 ----
    def _fold(self, items: list):
        system = [it for it in items if it["msg"].get("type") == "system"]
        body = [it for it in items if it["msg"].get("type") != "system"]

        # 1. 把 body 按 "tool call block" 分组：一个 assistant tool_calls + 紧随其后的所有 ToolMessage
        blocks: list[list[dict]] = []
        i = 0
        while i < len(body):
            it = body[i]
            msg = it["msg"]
            data = msg.get("data", msg)
            tool_calls = data.get("tool_calls") or data.get("additional_kwargs", {}).get("tool_calls")
            is_tool_call = msg.get("type") == "ai" and tool_calls
            if is_tool_call:
                block = [it]
                j = i + 1
                while j < len(body) and body[j]["msg"].get("type") == "tool":
                    block.append(body[j])
                    j += 1
                blocks.append(block)
                i = j
            else:
                blocks.append([it])
                i += 1

        block_tokens = [sum(_msg_tokens(it["msg"], self._count) for it in b) for b in blocks]

        # 2. 保留区：从最新 block 往前累计到 reserve_tokens，这些 block 永折不动
        reserve_tokens = int(self.budget_tokens * self.reserve_ratio)
        kept, acc = [], 0
        for b, t in zip(reversed(blocks), reversed(block_tokens)):
            kept.append(b)
            acc += t
            if acc >= reserve_tokens:
                break
        keep_ids = {id(b) for b in kept}

        # 3. 软预算内，从新往旧继续容纳旧 block（保留区已含的不重复计）
        for b, t in zip(reversed(blocks), reversed(block_tokens)):
            if id(b) in keep_ids:
                continue
            if acc + t <= self.budget_tokens:
                kept.append(b)
                acc += t
            else:
                break  # 更旧的 block 超出软预算，停止

        # 4. 硬停兜底：若窗口仍超 hard_stop，继续折最旧直到 <= 硬停（极端配置才触发）
        if self.hard_stop_tokens > 0 and acc > self.hard_stop_tokens:
            new_kept, new_acc = [], 0
            for b, t in zip(reversed(blocks), reversed(block_tokens)):
                if new_acc + t <= self.hard_stop_tokens:
                    new_kept.append(b)
                    new_acc += t
            kept, acc = new_kept, new_acc
            keep_ids = {id(b) for b in kept}

        # 5. 分类 kept/folded（按 block 原顺序）
        kept_blocks = [b for b in blocks if id(b) in keep_ids]
        folded_blocks = [b for b in blocks if id(b) not in keep_ids]

        # 6. 登记被折 block 进 eviction index（含可选 embed 缓存）
        self._register_eviction(folded_blocks)

        kept = [it for block in kept_blocks for it in block]
        folded = [it for block in folded_blocks for it in block]

        window = [messages_from_dict([s["msg"]])[0] for s in system]
        if folded_blocks:
            window.append(self._make_map_stub(folded_blocks))
            if self._metrics is not None:
                folded_tok = sum(_msg_tokens(f["msg"], self._count) for f in folded)
                self._metrics.fold(len(folded), folded_tok)
        window += [messages_from_dict([k["msg"]])[0] for k in kept]
        return window, [f["seq"] for f in folded]

    # ---- 登记被折 block 进 eviction index（分层索引简化版：按 seq 精确记录 + 可选 embed 缓存）----
    def _register_eviction(self, folded_blocks: list) -> None:
        if not folded_blocks:
            self._eviction_index.clear()
            return
        current = {it["seq"] for block in folded_blocks for it in block}
        # 移除已不再被折的陈旧登记（仅在 current 内复用 embedding 缓存）
        for seq in list(self._eviction_index.keys()):
            if seq not in current:
                del self._eviction_index[seq]
        texts_to_embed, seq_for_text = [], []
        for block in folded_blocks:
            for it in block:
                seq = it["seq"]
                content = _content_of(it["msg"]).replace("\n", " ")
                headline = content[:120] or f"(empty {it['msg'].get('type')})"
                entry = self._eviction_index.get(seq, {})
                entry["headline"] = headline
                entry["tokens"] = _msg_tokens(it["msg"], self._count)
                entry["type"] = it["msg"].get("type")
                self._eviction_index[seq] = entry
                # 仅首次 embed（命中缓存则跳过，不重复发请求）
                if (
                    self.enable_semantic_recall
                    and self.embedding_client is not None
                    and _embed_client_enabled(self.embedding_client)
                    and entry.get("embedding") is None
                ):
                    texts_to_embed.append(content or headline)
                    seq_for_text.append(seq)
        if texts_to_embed:
            try:
                vecs = self.embedding_client.embed(texts_to_embed)
                if vecs:
                    for seq, vec in zip(seq_for_text, vecs):
                        if vec:
                            self._eviction_index[seq]["embedding"] = vec
            except Exception:
                pass  # 语义召回降级为 keyword，不影响折叠

    def _make_map_stub(self, folded_blocks: list) -> SystemMessage:
        # 把被折 block 合并成连续区段（对齐 QwenPaw EvictionIndex 的区段地图）
        segments = []
        for block in folded_blocks:
            seqs = [it["seq"] for it in block]
            first = _content_of(block[0]["msg"])[:80].replace("\n", " ")
            segments.append((seqs[0], seqs[-1], len(block), first))
        merged = []
        for s0, s1, cnt, head in segments:
            if merged and merged[-1][1] + 1 == s0:
                merged[-1] = (merged[-1][0], s1, merged[-1][2] + cnt, merged[-1][3])
            else:
                merged.append((s0, s1, cnt, head))
        total_msgs = sum(c for _, _, c, _ in merged)
        total_tok = sum(
            _msg_tokens(it["msg"], self._count)
            for block in folded_blocks for it in block
        )
        lines = [
            f"{FOLD_STUB_MARKER} {len(merged)} segment(s), {total_msgs} msgs, "
            f"~{total_tok} tok folded to save context.",
        ]
        for s0, s1, cnt, head in merged:
            rng = f"turns {s0}" if s0 == s1 else f"turns {s0}-{s1}"
            lines.append(f"  - [{rng}] ({cnt}) {head}...")
        lines.append(
            'Call recall(seq=N) to restore a specific turn, or recall("keyword") to search.'
        )
        return SystemMessage(content="\n".join(lines), additional_kwargs={"kind": "fold_stub"})

    # ---- 媒体剥离：把历史里的 base64 图片/音视频从上下文摘掉省 token ----
    def _strip_media(self, window: list) -> list:
        stripped = 0
        for m in window:
            content = getattr(m, "content", None)
            if isinstance(content, str):
                new, n = _DATA_URI_RE.subn("[media stripped]", content)
                if n:
                    m.content = new
                    stripped += n
            elif isinstance(content, list):
                # multimodal parts：丢弃 image_url/audio 块，仅保留文本
                new_parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") in (
                        "image_url",
                        "image",
                        "audio",
                        "input_audio",
                        "video",
                    ):
                        stripped += 1
                        continue
                    new_parts.append(p)
                if stripped:
                    m.content = new_parts or ""
        if stripped and self._metrics is not None:
            self._metrics.media_strip(stripped)
        return window

    # ---- 工具结果分层裁剪（QwenPaw ToolResultPruningMiddleware 等效） ----
    # 仅在发送给模型的**窗口**上裁剪；store 中的原文保持完整，recall 可还原。
    # 分层：最近 recent_tool_window 个工具结果保留 recent 上限，更早的只留 old 上限。
    # 外置占位符（[TOOL RESULT EXTERNALIZED]）本身很短，无需裁剪，直接跳过。
    def _tool_prune_enabled(self) -> bool:
        return self._max_tool_result_chars > 0 or (
            self._recent_tool_result_chars > 0 or self._old_tool_result_chars > 0
        )

    def _prune_tool_results(self, window: list) -> list:
        if not self._tool_prune_enabled():
            return window
        # 扁平兜底（历史字段）：单一上限，覆盖分层。
        if self._max_tool_result_chars > 0:
            return self._prune_flat(window, self._max_tool_result_chars)
        # 分层（recent/old）
        tool_idx = [i for i, m in enumerate(window) if isinstance(m, ToolMessage)]
        recent_set = set(tool_idx[-self._recent_tool_window:]) if self._recent_tool_window > 0 else set(tool_idx)
        pruned = 0
        for i, m in enumerate(window):
            if not isinstance(m, ToolMessage):
                continue
            if is_externalized_placeholder(getattr(m, "content", "")):
                continue  # 占位符已是极短引用，跳过
            cap = self._recent_tool_result_chars if i in recent_set else self._old_tool_result_chars
            if cap <= 0:
                continue
            pruned += self._truncate_tool_message(m, cap)
        if pruned and self._metrics is not None and hasattr(self._metrics, "tool_result_prune"):
            self._metrics.tool_result_prune(pruned)
        return window

    def _prune_flat(self, window: list, limit: int) -> list:
        pruned = 0
        for m in window:
            if not isinstance(m, ToolMessage):
                continue
            if is_externalized_placeholder(getattr(m, "content", "")):
                continue
            pruned += self._truncate_tool_message(m, limit)
        if pruned and self._metrics is not None and hasattr(self._metrics, "tool_result_prune"):
            self._metrics.tool_result_prune(pruned)
        return window

    @staticmethod
    def _truncate_tool_message(m: ToolMessage, limit: int) -> int:
        """在窗口上把工具结果截断到 limit（store 仍留全文 → recall 可还原）。返回裁掉字符数。"""
        content = getattr(m, "content", None)
        removed = 0
        if isinstance(content, str):
            if len(content) > limit:
                m.content = (
                    content[:limit]
                    + f"\n... [tool result truncated to {limit} chars; call recall(seq) to restore full]"
                )
                removed = len(content) - limit
        elif isinstance(content, list):
            new_parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    t = p.get("text", "")
                    if len(t) > limit:
                        removed += len(t) - limit
                        p = {**p, "text": t[:limit] + f"\n... [truncated to {limit} chars; recall to restore]"}
                new_parts.append(p)
            m.content = new_parts
        return removed

    # ---- 对外：准备本轮发送给模型的窗口 ----
    def prepare(self, raw_messages: list, thread_id, system_hint: Optional[str] = None):
        self.set_active_thread(thread_id)
        self._persist_new(raw_messages, thread_id)
        items = self._store.all(thread_id)
        window, folded_seqs = self._fold(items)
        if self.strip_media:
            window = self._strip_media(window)
        if self._tool_prune_enabled():
            window = self._prune_tool_results(window)
        if system_hint:
            # 即便 window[0] 是折叠桩（SystemMessage），也要把真正的系统提示插到最前面；
            # 否则 system_hint 里包含的 workspace 文件 / 当前时间 / mode hint 会一起丢失。
            if window and isinstance(window[0], SystemMessage):
                if window[0].content != system_hint:
                    window.insert(0, SystemMessage(content=system_hint))
            else:
                window.insert(0, SystemMessage(content=system_hint))
        if self._metrics is not None:
            sent_tok = sum(
                self._count(m.content) if isinstance(m.content, str) else 0
                for m in window
            )
            self._metrics.context_window(len(window), sent_tok, len(folded_seqs))
        return window, {"folded_seqs": folded_seqs, "fold_count": len(folded_seqs)}

    # ---- 检视：供前端上下文面板查看已存全文与折叠情况 ----
    def inspect(self, thread_id, preview_chars: int = 200) -> dict:
        rows = self._store.all(thread_id)
        turns = []
        for it in rows:
            content = _content_of(it["msg"])
            turns.append(
                {
                    "seq": it["seq"],
                    "type": it["msg"].get("type", "?"),
                    "preview": (content or "")[:preview_chars],
                    "chars": len(content or ""),
                }
            )
        # 用当前预算重算一次折叠，报告哪些 seq 会被折
        window, folded_seqs = self._fold(rows) if rows else ([], [])
        return {
            "thread_id": str(thread_id),
            "total_turns": len(rows),
            "budget_tokens": self.budget_tokens,
            "strip_media": self.strip_media,
            "max_tool_result_chars": self._max_tool_result_chars,
            "folded_seqs": folded_seqs,
            "window_size": len(window),
            "recall_enabled": self.allow_unsandboxed_recall,
            "turns": turns,
        }

    # ---- 枚举所有 thread（供前端 inspect 下拉） ----
    def thread_ids(self) -> list:
        return self._store.thread_ids()

    # ---- 清空某个 thread 的存储（折叠区原文一并丢弃，无法再 recall） ----
    def clear(self, thread_id) -> int:
        return self._store.clear(thread_id)

    # ---- 召回：agent 显式调用，还原折叠区 / 外置工具结果全文 ----
    # 支持三种入口：
    #  - seq=          精确还原某 seq（含被外置落盘的工具结果，从磁盘取回全文）
    #  - query="seq=N"  同上（recall 工具只传单字符串，故解析 seq= 形式）
    #  - query=token   外置占位符里的 Token，直接还原落盘全文
    #  - query=keyword  语义 top-k（embedding）或 keyword 子串；无命中时回退搜外置 blob
    def recall(self, query: str = None, thread_id=None, seq=None, top_k: int = 3) -> str:
        tid = thread_id or self._active_thread
        if tid is None:
            return "recall: no active thread (call prepare first)."
        if not self.allow_unsandboxed_recall:
            return (
                "recall: unsandboxed recall disabled. Enable via "
                "allow_unsandboxed_recall=True or env ALLOW_UNSANDBOXED_RECALL=1."
            )
        rows = self._store.all(tid)

        # 0) query 里可能带 "seq=N" 或本身就是 token，先归一。
        resolved_seq = seq
        q_raw = (query or "").strip()
        if resolved_seq is None and q_raw.lower().startswith("seq="):
            try:
                resolved_seq = int(q_raw[4:].strip())
            except ValueError:
                resolved_seq = None
        # token 直取（外置占位符 Token 形如 <thread>/<file>.txt）
        if (
            resolved_seq is None
            and self._tr_store is not None
            and q_raw
            and q_raw.lower().endswith(".txt")
            and ("/" in q_raw or "\\" in q_raw)
        ):
            full = self._tr_store.recall(q_raw)
            if full is not None:
                if self._metrics is not None:
                    self._metrics.recall(1)
                return f"recall(token={q_raw}) restored:\n\n{full[:4000]}"

        # 1) 精确 seq 还原（对齐 QwenPaw recall_history 的精确 seq 召回；外置结果从磁盘取回）
        if resolved_seq is not None:
            target = int(resolved_seq) if isinstance(resolved_seq, str) and str(resolved_seq).isdigit() else resolved_seq
            for it in rows:
                if it["seq"] == target:
                    content = _content_of(it["msg"])
                    # 若该 turn 是被外置落盘的工具结果，从磁盘取回全文。
                    if is_externalized_placeholder(content) and self._tr_store is not None:
                        tok = extract_token(content)
                        full = self._tr_store.recall(tok) if tok else None
                        if full is not None:
                            if self._metrics is not None:
                                self._metrics.recall(1)
                            return f"recall(seq={resolved_seq}) restored (from disk):\n\n{full[:4000]}"
                    if self._metrics is not None:
                        self._metrics.recall(1)
                    return f"recall(seq={resolved_seq}) restored:\n\n{content[:4000]}"
            return f"recall: seq={resolved_seq} not found in store."

        q = q_raw.lower()
        if not q:
            return "recall: provide a seq or a query."

        # 2) 语义召回：embedding 可用时走向量余弦 top-k（对齐 QwenPaw 语义 recall）
        if (
            self.enable_semantic_recall
            and self.embedding_client is not None
            and _embed_client_enabled(self.embedding_client)
        ):
            try:
                qvec = self.embedding_client.embed([query])
                if qvec and qvec[0]:
                    scored = []
                    for s, e in self._eviction_index.items():
                        vec = e.get("embedding")
                        if vec:
                            scored.append((_cosine(qvec[0], vec), s))
                    if scored:
                        scored.sort(reverse=True)
                        top = scored[: max(1, top_k)]
                        out = [f"recall('{query}') semantic top-{len(top)}:"]
                        for score, s in top:
                            content = _content_of(
                                next((it["msg"] for it in rows if it["seq"] == s), {})
                            ) or self._eviction_index[s].get("headline", "")
                            out.append(f"\n[score={score:.3f} seq={s}] {content[:2000]}")
                        if self._metrics is not None:
                            self._metrics.recall(len(top))
                        return "\n".join(out)
            except Exception:
                pass  # 降级 keyword

        # 3) keyword 子串回退（保持原有行为）
        matched = []
        for it in rows:
            content = _content_of(it["msg"])
            if q and q in (content or "").lower():
                matched.append((it["seq"], content))
        if matched:
            out = [f"recall('{query}') restored {len(matched)} turn(s):"]
            for s, content in matched:
                out.append(f"\n--- turn {s} ---\n{content[:2000]}")
            if self._metrics is not None:
                self._metrics.recall(len(matched))
            return "\n".join(out)

        # 4) 外置 blob 关键词回退：store 里没有命中时，搜落盘的工具结果全文。
        if self._tr_store is not None:
            hits = self._tr_store.search_thread(str(tid), q, limit=top_k)
            if hits:
                out = [f"recall('{query}') restored {len(hits)} externalized tool result(s):"]
                for fname, snippet in hits:
                    out.append(f"\n--- {fname} ---\n{snippet}")
                if self._metrics is not None:
                    self._metrics.recall(len(hits))
                return "\n".join(out)

        return f"recall: no content matches '{query}'."
