# -*- coding: utf-8 -*-
"""agent-harness 的 Memory Manager（QwenPaw ReMeLight 的 LangGraph 原生等效实现）。

设计目标：对齐 QwenPaw 的三层记忆架构中的第三层——长期语义记忆。

- 配置面：完全镜像 QwenPaw 的 ``ReMeLightMemoryCard`` / ``reme_config.py``：
  ``summarize_when_compact`` / ``auto_memory_interval`` / ``dream_cron`` /
  ``rebuild_memory_index_on_start`` / ``enable_search_raw_log`` /
  ``auto_memory_search{enabled,max_results}`` / ``embedding_model_config{...}``。
- 存储：本地 markdown vault（``daily/`` 每日笔记、``digest/`` 摘要、``dream/`` 兴趣目录），
  索引持久化到 ``index.json``（分块 + 嵌入向量 + BM25 词项）。
- 检索：向量余弦 + BM25 的 RRF 混合检索（与 QwenPaw 的 vector_weight 0.7 一致）。
- 工具：向 agent 暴露 ``memory_search`` 工具；``auto_memory_search`` 在回复前自动注入相关记忆；
  ``auto_memory`` 按间隔把对话事实写入每日笔记；``dream`` 周期性整合/去重记忆。

依赖：仅标准库 + pydantic + langchain_core（嵌入走 OpenAI 兼容 HTTP，零额外依赖）。
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from langchain_core.tools import StructuredTool

from .config import DATA_HOME


# ---------------------------------------------------------------------------
# 路径约定（与 plugins.py / graph_provider.py 保持一致）
# ---------------------------------------------------------------------------
def _workbuddy_dir() -> Path:
    # agent-harness 独立数据目录（不再使用 ~/.workbuddy，那是 WorkBuddy IDE 的数据目录）
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    return DATA_HOME


def _workspace_dir() -> Path:
    p = _workbuddy_dir() / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


VAULT_DIR = _workbuddy_dir() / "memory_vault"
CONFIG_PATH = _workbuddy_dir() / "memory_config.json"


# ---------------------------------------------------------------------------
# 配置模型（镜像 QwenPaw ReMeLight）
# ---------------------------------------------------------------------------
_OPENAI_COMPAT_EMBEDDING_BACKENDS = {"openai", "dashscope", "dashscope_multimodal", "gemini"}


class EmbeddingModelConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    backend: str = "openai"
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    dimensions: int = 1024
    enable_cache: bool = True
    max_cache_size: int = 3000
    max_input_length: int = 8192
    max_batch_size: int = 10


class AutoMemorySearchConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    max_results: int = 5


class ReMeLightMemoryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summarize_when_compact: bool = True
    auto_memory_interval: int = 10
    dream_cron: str = ""
    rebuild_memory_index_on_start: bool = False
    enable_search_raw_log: bool = False
    auto_memory_search_config: AutoMemorySearchConfig = Field(
        default_factory=AutoMemorySearchConfig
    )
    embedding_model_config: EmbeddingModelConfig = Field(
        default_factory=EmbeddingModelConfig
    )


class MemoryManagerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # 与 QwenPaw 的 memory_manager_backend 对齐：remelight | none
    backend: str = "remelight"
    reme_light_memory_config: ReMeLightMemoryConfig = Field(
        default_factory=ReMeLightMemoryConfig
    )


def default_config() -> MemoryManagerConfig:
    return MemoryManagerConfig()


def load_config() -> MemoryManagerConfig:
    if CONFIG_PATH.exists():
        try:
            return MemoryManagerConfig(**json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            return MemoryManagerConfig()
    return MemoryManagerConfig()


def save_config(cfg: MemoryManagerConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def runtime_memory_config() -> MemoryManagerConfig:
    """从 runtime.json 的 ``running`` 段构造 MemoryManagerConfig（单一事实源）。

    与 QwenPaw 对齐：``running.memory_manager_backend``（remelight|none）+
    ``running.reme_light_memory_config``。``memory_config.json`` 仅作为向后兼容
    镜像，本函数不再依赖它——这样 Long-term Memory UI / Plugins 面板对
    runtime.json 的修改会真实反映到引擎，修复“配置写了却未真正接入”的问题。
    """
    try:
        from .config import get_config

        cfg = get_config()
        running = (cfg.running or {}) if cfg is not None else {}
    except Exception:
        return default_config()
    if not running:
        return default_config()
    backend = str(running.get("memory_manager_backend") or "remelight").strip().lower()
    reme_raw = running.get("reme_light_memory_config") or {}
    if not isinstance(reme_raw, dict):
        reme_raw = {}
    try:
        reme = ReMeLightMemoryConfig(**reme_raw)
    except Exception:
        reme = ReMeLightMemoryConfig()
    return MemoryManagerConfig(backend=backend, reme_light_memory_config=reme)


# ---------------------------------------------------------------------------
# 嵌入客户端（OpenAI 兼容，仅标准库）
# ---------------------------------------------------------------------------
class EmbeddingClient:
    def __init__(self, cfg: EmbeddingModelConfig):
        self.cfg = cfg

    def is_enabled(self) -> bool:
        c = self.cfg
        if not c.model_name.strip():
            return False
        if c.backend in _OPENAI_COMPAT_EMBEDDING_BACKENDS:
            return bool(c.api_key.strip())
        if c.backend == "gemini":
            return bool(c.api_key.strip())
        if c.backend == "ollama":
            return bool(c.base_url.strip())
        return False

    def _base_url(self) -> str:
        base = (self.cfg.base_url or "").strip().rstrip("/")
        if not base:
            base = "https://api.openai.com/v1"
        return base + "/embeddings"

    def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        if not self.is_enabled() or not texts:
            return None
        import urllib.request

        c = self.cfg
        headers = {"Content-Type": "application/json"}
        if c.backend == "ollama":
            # ollama 兼容模式下也走 Bearer（空 key 亦可）
            headers["Authorization"] = "Bearer " + (c.api_key or "ollama")
        else:
            headers["Authorization"] = "Bearer " + (c.api_key or "")
        payload = json.dumps(
            {"input": texts, "model": c.model_name}
        ).encode("utf-8")
        req = urllib.request.Request(self._base_url(), data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [d["embedding"] for d in data["data"]]
        except Exception as e:  # noqa: BLE001
            # 嵌入失败时降级为 None（纯 BM25 检索仍可用）
            print(f"[memory] embedding failed: {e}")
            return None


# ---------------------------------------------------------------------------
# 记忆 vault + 混合检索
# ---------------------------------------------------------------------------
_STOP = set(
    "the a an and or of to in is are was were be been being for on at by with as it its this that "
    "these those from we you they he she i my your our their his her not no yes do does did have has "
    "will would can could should may might must i'm you're we're they're it's that's what when where "
    "who why how which but if then so because about into out up down over under again more most other "
    "some such only own same than too very s t can t just don t now".split()
)


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", text.lower()) if t not in _STOP]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class MemoryVault:
    def __init__(self, root: Path, embedding: EmbeddingClient):
        self.root = root
        self.daily_dir = root / "daily"
        self.digest_dir = root / "digest"
        self.dream_dir = root / "dream"
        for d in (self.daily_dir, self.digest_dir, self.dream_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.index_path = root / "index.json"
        self.embedding = embedding
        self._lock = threading.RLock()
        self._index: list[dict] = []
        self._load_index()

    # ---- 索引持久化 ----
    def _load_index(self) -> None:
        if self.index_path.exists():
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8")).get("chunks", [])
            except Exception:
                self._index = []

    def _save_index(self) -> None:
        self.index_path.write_text(
            json.dumps({"chunks": self._index}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _chunk_text(self, text: str, size: int = 600, overlap: int = 80) -> list[str]:
        paras = [p.strip() for p in re.split(r"\n{1,}|\n", text) if p.strip()]
        chunks: list[str] = []
        buf = ""
        for p in paras:
            if len(buf) + len(p) + 1 <= size:
                buf = (buf + "\n" + p).strip()
            else:
                if buf:
                    chunks.append(buf)
                buf = p
            while len(buf) > size:
                chunks.append(buf[:size])
                buf = buf[size - overlap :]
        if buf:
            chunks.append(buf)
        return chunks or [text]

    def _scan_files(self) -> list[Path]:
        out: list[Path] = []
        for d in (self.daily_dir, self.digest_dir, self.dream_dir):
            if d.exists():
                out.extend(sorted(d.rglob("*.md")))
        return out

    def rebuild_index(self) -> int:
        with self._lock:
            self._index = []
            files = self._scan_files()
            texts: list[str] = []
            meta: list[tuple[str, str]] = []  # (relpath, chunk_text)
            for f in files:
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(f.relative_to(self.root))
                for ch in self._chunk_text(content):
                    texts.append(ch)
                    meta.append((rel, ch))
            # 批量嵌入（受 max_batch_size 限制）
            embeds: list[Optional[list[float]]] = [None] * len(texts)
            if self.embedding.is_enabled() and texts:
                bs = max(1, self.embedding.cfg.max_batch_size)
                for i in range(0, len(texts), bs):
                    batch = texts[i : i + bs]
                    res = self.embedding.embed(batch)
                    if res:
                        for j, e in enumerate(res):
                            embeds[i + j] = e
            for (rel, ch), e in zip(meta, embeds):
                self._index.append(
                    {
                        "path": rel,
                        "text": ch,
                        "tokens": _tokenize(ch),
                        "embedding": e,
                    }
                )
            self._save_index()
            return len(self._index)

    # ---- 写入每日笔记 ----
    def add_daily_note(self, date: str, section: str) -> None:
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        path = self.daily_dir / f"{date}.md"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        block = f"\n## {ts}\n\n{section}\n"
        with self._lock:
            if path.exists():
                path.write_text(path.read_text(encoding="utf-8") + block, encoding="utf-8")
            else:
                path.write_text(
                    f"---\nname: {date}\ndescription: daily memory note\ndate: {date}\n---\n"
                    + block,
                    encoding="utf-8",
                )

    def write_dream(self, content: str) -> None:
        self.dream_dir.mkdir(parents=True, exist_ok=True)
        path = self.dream_dir / "interests.md"
        with self._lock:
            path.write_text(
                f"---\nname: interests\ndescription: consolidated memory (dream)\n---\n\n{content}\n",
                encoding="utf-8",
            )

    # ---- BM25 ----
    def _bm25(self, query_tokens: list[str]) -> list[float]:
        n = len(self._index)
        if n == 0:
            return []
        df: dict[str, int] = {}
        for c in self._index:
            for t in set(c["tokens"]):
                df[t] = df.get(t, 0) + 1
        idf = {t: math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1) for t in df}
        k1, b, avgdl = 1.5, 0.75, max(1, sum(len(c["tokens"]) for c in self._index) / n)
        scores = []
        for c in self._index:
            dl = len(c["tokens"])
            score = 0.0
            tf_counts: dict[str, int] = {}
            for t in c["tokens"]:
                tf_counts[t] = tf_counts.get(t, 0) + 1
            for qt in query_tokens:
                if qt in tf_counts:
                    f = tf_counts[qt]
                    score += idf.get(qt, 0) * (
                        (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
                    )
            scores.append(score)
        return scores

    # ---- 混合检索（RRF） ----
    def hybrid_search(self, query: str, max_results: int = 5) -> list[dict]:
        with self._lock:
            if not self._index:
                self.rebuild_index()
            if not self._index:
                return []
            q_tokens = _tokenize(query)
            # BM25 排名
            bm25 = self._bm25(q_tokens)
            bm25_rank = self._rank(bm25)
            # 向量排名
            vec_rank: list[int] = [0] * len(self._index)
            if self.embedding.is_enabled() and q_tokens:
                q_emb = self.embedding.embed([query])
                if q_emb:
                    sims = [_cosine(q_emb[0], c["embedding"]) if c["embedding"] else 0.0 for c in self._index]
                    vec_rank = self._rank(sims)
            # RRF 融合
            k = 60
            fused = []
            for i in range(len(self._index)):
                s = (1.0 / (k + bm25_rank[i] + 1)) + (1.0 / (k + vec_rank[i] + 1))
                fused.append(s)
            order = sorted(range(len(self._index)), key=lambda i: fused[i], reverse=True)
            out = []
            for i in order[:max_results]:
                c = self._index[i]
                out.append(
                    {
                        "path": c["path"],
                        "score": round(fused[i], 4),
                        "text": c["text"][:400],
                    }
                )
            return out

    @staticmethod
    def _rank(scores: list[float]) -> list[int]:
        """返回每个元素的排名（0 = 最高分）。"""
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        rank = [0] * len(scores)
        for r, i in enumerate(order):
            rank[i] = r
        return rank

    def stats(self) -> dict:
        with self._lock:
            return {
                "notes": sum(1 for _ in self._scan_files()),
                "chunks": len(self._index),
                "embedding_enabled": self.embedding.is_enabled(),
            }


# ---------------------------------------------------------------------------
# Memory Manager
# ---------------------------------------------------------------------------
class MemoryManager:
    def __init__(self, cfg: MemoryManagerConfig, working_dir: Optional[str] = None):
        self.cfg = cfg
        self.working_dir = working_dir or str(_workspace_dir())
        self.vault = MemoryVault(VAULT_DIR, EmbeddingClient(cfg.reme_light_memory_config.embedding_model_config))
        self._started = False
        self._turn_count: dict[str, int] = {}
        self._last_dream: Optional[str] = None
        self._lock = threading.RLock()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self.cfg.reme_light_memory_config.rebuild_memory_index_on_start:
            try:
                self.vault.rebuild_index()
            except Exception as e:  # noqa: BLE001
                print(f"[memory] rebuild index on start failed: {e}")

    # ---- 检索工具 ----
    def memory_search(self, query: str, max_results: int = 5) -> str:
        if not query or not query.strip():
            return "memory_search: empty query."
        k = max_results or self.cfg.reme_light_memory_config.auto_memory_search_config.max_results
        results = self.vault.hybrid_search(query, k)
        if not results:
            return f"memory_search('{query}'): no relevant memory found."
        lines = [f"memory_search('{query}') returned {len(results)} result(s):"]
        for i, r in enumerate(results, 1):
            snippet = r["text"].replace("\n", " ")
            lines.append(f"\n[{i}] {r['path']} (score={r['score']})\n{snippet}")
        return "\n".join(lines)

    # ---- 自动记忆（按间隔把对话事实写入每日笔记） ----
    def auto_memory(self, messages: list, force: bool = False, thread_id: str = None) -> None:
        interval = self.cfg.reme_light_memory_config.auto_memory_interval or 0
        if interval <= 0 and not force:
            return
        tid = thread_id or self._active_thread or "default"
        if not force:
            with self._lock:
                self._turn_count[tid] = self._turn_count.get(tid, 0) + 1
                if self._turn_count[tid] < interval:
                    return
                self._turn_count[tid] = 0
        try:
            text = self._extract_facts(messages)
            if text.strip():
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                self.vault.add_daily_note(today, text)
                # 写入后增量重建索引
                self.vault.rebuild_index()
        except Exception as e:  # noqa: BLE001
            print(f"[memory] auto_memory failed: {e}")

    def _extract_facts(self, messages: list) -> str:
        """从对话抽取可持久事实。有 LLM key 走模型，否则启发式回退。"""
        convo = []
        for m in messages[-12:]:
            role = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
            content = ""
            if isinstance(m, dict):
                content = m.get("content") or ""
            else:
                content = getattr(m, "content", "") or ""
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            if role in ("human", "user") and content:
                convo.append(f"User: {content[:300]}")
            elif role in ("ai", "assistant") and content:
                convo.append(f"Assistant: {content[:300]}")
        if not convo:
            return ""
        text = "\n".join(convo)
        llm_out = self._llm_extract(text)
        if llm_out:
            return llm_out
        # 回退：直接把用户陈述作为要点
        bullets = []
        for line in convo:
            if line.startswith("User:"):
                bullets.append(f"- {line[6:].strip()}")
        return "### Extracted facts\n" + ("\n".join(bullets) if bullets else "(no durable facts detected)")

    def _llm_extract(self, text: str) -> Optional[str]:
        try:
            from ..harness.models import make_deepseek_model
            from .config import get_config

            cfg = get_config()
            key = (cfg.llm.get("api_key") or "").strip()
            if not key:
                return None
            model = make_deepseek_model(
                model=(cfg.llm.get("model") or "deepseek-chat").strip(),
                api_key=key,
                base_url=(cfg.llm.get("base_url") or None),
                streaming=False,
                reasoning=False,
                provider=(cfg.llm.get("provider") or "").strip(),
            )
            prompt = (
                "You are a memory extractor. From the conversation below, extract "
                "durable facts, user preferences, decisions, and project context as "
                "concise bullet lines (prefix each with '- '). Ignore ephemeral chit-chat. "
                "Output only the bullet lines.\n\n"
                f"{text}"
            )
            resp = model.invoke(prompt)
            content = getattr(resp, "content", "") or ""
            return "### Extracted facts\n" + content.strip() if content.strip() else None
        except Exception as e:  # noqa: BLE001
            print(f"[memory] llm extract failed: {e}")
            return None

    # ---- dream：整合/去重记忆 ----
    def dream(self) -> dict:
        try:
            files = sorted(self.vault.daily_dir.rglob("*.md"))
            text = ""
            for f in files[-7:]:
                try:
                    text += f.read_text(encoding="utf-8", errors="replace") + "\n"
                except Exception:
                    pass
            if not text.strip():
                return {"ok": True, "skipped": "no daily notes"}
            content = self._llm_dream(text) or ("# Dream (auto-consolidated)\n\n" + text[:2000])
            self.vault.write_dream(content)
            self._last_dream = datetime.now(timezone.utc).isoformat()
            self.vault.rebuild_index()
            return {"ok": True, "dreamed_at": self._last_dream}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def _llm_dream(self, text: str) -> Optional[str]:
        try:
            from ..harness.models import make_deepseek_model
            from .config import get_config

            cfg = get_config()
            key = (cfg.llm.get("api_key") or "").strip()
            if not key:
                return None
            model = make_deepseek_model(
                model=(cfg.llm.get("model") or "deepseek-chat").strip(),
                api_key=key,
                base_url=(cfg.llm.get("base_url") or None),
                streaming=False,
                reasoning=False,
                provider=(cfg.llm.get("provider") or "").strip(),
            )
            prompt = (
                "Consolidate the following daily memory notes into a concise "
                "consolidated memory: merge redundant entries, surface durable topics "
                "and user interests. Output markdown with headers.\n\n" + text[:6000]
            )
            resp = model.invoke(prompt)
            return getattr(resp, "content", "") or None
        except Exception as e:  # noqa: BLE001
            print(f"[memory] llm dream failed: {e}")
            return None

    # ---- 系统提示（注入 agent） ----
    def get_memory_prompt(self) -> str:
        return (
            "You have a long-term memory store. Use the `memory_search` tool to recall "
            "relevant past facts, user preferences, decisions, and project context when "
            "the current task may benefit from prior context. Durable facts and preferences "
            "shared by the user are automatically recorded to memory over time."
        )

    def list_memory_tools(self) -> list:
        def _search(query: str, max_results: int = 5) -> str:
            return self.memory_search(query, max_results)

        return [
            StructuredTool.from_function(
                func=_search,
                name="memory_search",
                description=(
                    "Hybrid (vector + BM25) semantic search over the agent's long-term "
                    "memory vault (daily notes, digests, dreams). Use to recall prior facts, "
                    "user preferences, or decisions. Input: query (string), max_results (int, default 5)."
                ),
            )
        ]

    def stats(self) -> dict:
        s = self.vault.stats()
        s.update(
            {
                "enabled": True,
                "backend": self.cfg.backend,
                "last_dream": self._last_dream,
                "embedding_enabled": s.pop("embedding_enabled", False),
                "auto_memory_interval": self.cfg.reme_light_memory_config.auto_memory_interval,
                "dream_cron": self.cfg.reme_light_memory_config.dream_cron,
            }
        )
        return s


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_mm: Optional[MemoryManager] = None
_mm_lock = threading.Lock()


def get_memory_manager() -> Optional[MemoryManager]:
    global _mm
    with _mm_lock:
        if _mm is not None:
            return _mm
        # 单一事实源：runtime.json 的 running 段（与 QwenPaw 对齐）
        cfg = runtime_memory_config()
        if cfg.backend == "none":
            return None
        _mm = MemoryManager(cfg)
        _mm.start()
        return _mm


def reset_memory_manager() -> None:
    global _mm
    with _mm_lock:
        _mm = None
