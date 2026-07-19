"""工具结果外置存储（执行层落盘，对齐 QwenPaw ToolResultLimiter / 腾讯 WorkBuddy ToolResultBlobService）。

设计要点（对照两家实现，且**绝不丢原文**）：
- 工具结果超过阈值（默认 50KB）时，把**完整全文**写到磁盘
  ``~/.agent-harness/tool-results/<thread_id>/<tool>_<uuid>.txt``，
  上下文里只留一个占位符（含 token）；模型需要时经 ``recall`` 把全文取回。
- 阈值以下的结果**保持完整 inline**，不做任何头尾截断（这就是与"粗暴截断"的本质区别）。
- 按 thread_id 隔离目录，等价于 WorkBuddy 的 session/subagent 隔离；文件名做 sanitize 防注入。
- 全程只写本地磁盘，无网络依赖；``recall`` 幂等，文件缺失返回 None（绝不抛错中断主循环）。
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Optional

# 外置占位符标记：ContextManager.recall 据此识别并从磁盘还原。
EXTERNALIZE_MARKER = "[TOOL RESULT EXTERNALIZED]"

# token 形如 ``<thread_id>/<filename>.txt``（相对 store 根）。
_TOKEN_RE = re.compile(r"Token:\s*(\S+\.txt)")


def _default_root() -> Path:
    # 与 server.config.DATA_HOME 保持一致（默认 ~/.agent-harness）。
    env = os.environ.get("AGENT_DATA_HOME")
    base = Path(env) if env else Path.home() / ".agent-harness"
    return base / "tool-results"


def _sanitize(name: str, max_len: int = 48) -> str:
    """把任意字符串压成文件系统安全名（保留可读前缀）。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name or "tool")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned or "tool"


class ToolResultStore:
    def __init__(self, root: Optional[Path | str] = None, *, threshold_kb: int = 50):
        self.root = Path(root) if root is not None else _default_root()
        # 阈值按字符计（中文/英文混合，1 char ≈ 1 字节量级足够）。默认 50KB。
        self.threshold_chars = max(0, int(threshold_kb)) * 1024

    # ---- 是否应外置 ----
    def should_externalize(self, content: str) -> bool:
        return self.threshold_chars > 0 and len(content) > self.threshold_chars

    # ---- 外置：写全文到磁盘，返回 token（相对路径字符串） ----
    def externalize(self, content: str, *, thread_id: str, tool_name: str) -> str:
        thread_dir = self.root / _sanitize(thread_id, 64)
        thread_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{_sanitize(tool_name)}_{uuid.uuid4().hex[:12]}.txt"
        fpath = thread_dir / fname
        fpath.write_text(content, encoding="utf-8")
        # token = 相对 store 根的路径，便于 recall 跨进程/重启稳定解析。
        return f"{_sanitize(thread_id, 64)}/{fname}"

    # ---- 还原：给定 token 读回全文；不存在返回 None ----
    def recall(self, token: str) -> Optional[str]:
        if not token:
            return None
        # 防目录穿越：token 必须落在 root 内、且为普通文件。
        target = (self.root / token).resolve()
        try:
            if not self.root.resolve() in target.parents and target != self.root.resolve():
                # 允许 target 本身是 root 的子文件
                if target != self.root.resolve():
                    if self.root.resolve() not in target.parents:
                        return None
            if target.is_file():
                return target.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        return None

    # ---- 工具：扫描某 thread 的外置 blob 是否含关键词（供 recall(query) 回退） ----
    def search_thread(self, thread_id: str, keyword: str, limit: int = 3) -> list[tuple[str, str]]:
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        thread_dir = self.root / _sanitize(thread_id, 64)
        if not thread_dir.is_dir():
            return []
        hits = []
        for f in sorted(thread_dir.glob("*.txt")):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if kw in text.lower():
                snippet = text[:2000]
                hits.append((f.name, snippet))
                if len(hits) >= limit:
                    break
        return hits


# ---------------------------------------------------------------------------
# 模块级单例（对齐 core_files 的 get_core_files_manager 用法）
# ---------------------------------------------------------------------------
_store_singleton: Optional[ToolResultStore] = None


def get_tool_result_store() -> ToolResultStore:
    global _store_singleton
    if _store_singleton is None:
        kb = int(os.environ.get("AGENT_TOOL_RESULT_THRESHOLD_KB", "50"))
        _store_singleton = ToolResultStore(threshold_kb=kb)
    return _store_singleton


def reset_tool_result_store() -> None:
    global _store_singleton
    _store_singleton = None


def is_externalized_placeholder(text: str) -> bool:
    return isinstance(text, str) and EXTERNALIZE_MARKER in text


def extract_token(text: str) -> Optional[str]:
    """从外置占位符里抠出 token；非占位符返回 None。"""
    if not is_externalized_placeholder(text):
        return None
    m = _TOKEN_RE.search(text)
    return m.group(1) if m else None


def make_placeholder(tool_name: str, token: str, length: int) -> str:
    return (
        f"{EXTERNALIZE_MARKER}\n"
        f"Tool: {tool_name}\n"
        f"Original length: {length} chars\n"
        f"This output exceeded the inline limit and was saved to local disk to save context.\n"
        f"To read the full output, call recall(\"{token}\") — the original is fully preserved.\n"
        f"Token: {token}\n"
    )
