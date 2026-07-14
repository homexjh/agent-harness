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
import os
import re
from typing import Callable, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)

from .store import TurnStore

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
    tc = msg.get("data", msg).get("additional_kwargs", {}).get("tool_calls")
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
        # QwenPaw ToolResultPruningMiddleware 等效：工具结果超长则在窗口里截断（store 仍留全文）。
        self._max_tool_result_chars = max(0, int(max_tool_result_chars))
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

    # ---- 折叠：保留最近窗口，折最旧 ----
    def _fold(self, items: list):
        system = [it for it in items if it["msg"].get("type") == "system"]
        body = [it for it in items if it["msg"].get("type") != "system"]
        budget = self.budget_tokens
        kept, folded, acc = [], [], 0
        for it in reversed(body):
            t = _msg_tokens(it["msg"], self._count)
            if not kept or acc + t <= budget:
                kept.append(it)
                acc += t
            else:
                folded.append(it)
        kept.reverse()
        folded.reverse()
        window = [messages_from_dict([s["msg"]])[0] for s in system]
        if folded:
            window.append(self._make_stub(folded))
            if self._metrics is not None:
                folded_tok = sum(_msg_tokens(f["msg"], self._count) for f in folded)
                self._metrics.fold(len(folded), folded_tok)
        window += [messages_from_dict([k["msg"]])[0] for k in kept]
        return window, [f["seq"] for f in folded]

    def _make_stub(self, folded: list) -> SystemMessage:
        seqs = [f["seq"] for f in folded]
        first = _content_of(folded[0]["msg"])[:120].replace("\n", " ")
        total_tok = sum(_msg_tokens(f["msg"], self._count) for f in folded)
        content = (
            f"{FOLD_STUB_MARKER} turns {seqs[0]}\u2013{seqs[-1]} "
            f"({len(folded)} msgs, ~{total_tok} tok) folded to save context.\n"
            f"Preview: {first}...\n"
            f"Call recall(\"keyword\") to restore full content (preserved in store)."
        )
        return SystemMessage(content=content, additional_kwargs={"kind": "fold_stub"})

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

    # ---- 工具结果裁剪：把窗口里超长的 ToolMessage 截断（QwenPaw ToolResultPruningMiddleware 等效） ----
    # 仅在发送给模型的窗口上截断；store 中的原文保持完整，recall 可还原。
    def _prune_tool_results(self, window: list) -> list:
        limit = self._max_tool_result_chars
        pruned = 0
        for m in window:
            if not isinstance(m, ToolMessage):
                continue
            content = getattr(m, "content", None)
            if isinstance(content, str):
                if len(content) > limit:
                    m.content = (
                        content[:limit]
                        + f"\n... [tool result truncated to {limit} chars; call recall to restore full]"
                    )
                    pruned += len(content) - limit
            elif isinstance(content, list):
                new_parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        t = p.get("text", "")
                        if len(t) > limit:
                            pruned += len(t) - limit
                            p = {**p, "text": t[:limit] + f"\n... [truncated to {limit} chars]"}
                    new_parts.append(p)
                m.content = new_parts
        if pruned and self._metrics is not None and hasattr(self._metrics, "tool_result_prune"):
            self._metrics.tool_result_prune(pruned)
        return window

    # ---- 对外：准备本轮发送给模型的窗口 ----
    def prepare(self, raw_messages: list, thread_id, system_hint: Optional[str] = None):
        self.set_active_thread(thread_id)
        self._persist_new(raw_messages, thread_id)
        items = self._store.all(thread_id)
        window, folded_seqs = self._fold(items)
        if self.strip_media:
            window = self._strip_media(window)
        if self._max_tool_result_chars > 0:
            window = self._prune_tool_results(window)
        if system_hint and window and not isinstance(window[0], SystemMessage):
            window.insert(0, SystemMessage(content=system_hint))
        elif system_hint:
            # 首条已是 system（来自 items），仅当确实无 system 时插入
            has_system = any(isinstance(m, SystemMessage) for m in window)
            if not has_system:
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

    # ---- 召回：agent 显式调用，还原折叠区全文 ----
    def recall(self, query: str, thread_id=None) -> str:
        tid = thread_id or self._active_thread
        if tid is None:
            return "recall: no active thread (call prepare first)."
        if not self.allow_unsandboxed_recall:
            return (
                "recall: unsandboxed recall disabled. Enable via "
                "allow_unsandboxed_recall=True or env ALLOW_UNSANDBOXED_RECALL=1."
            )
        rows = self._store.all(tid)
        q = (query or "").lower()
        matched = []
        for it in rows:
            content = _content_of(it["msg"])
            if q and q in (content or "").lower():
                matched.append((it["seq"], content))
        if not matched:
            return f"recall: no content matches '{query}'."
        out = [f"recall('{query}') restored {len(matched)} turn(s):"]
        for seq, content in matched:
            out.append(f"\n--- turn {seq} ---\n{content[:2000]}")
        if self._metrics is not None:
            self._metrics.recall(len(matched))
        return "\n".join(out)
