# -*- coding: utf-8 -*-
"""MemoryVault 增量索引与 EmbeddingClient 缓存测试。

验证：
- 写入 daily/dream 时只 embed 变更文件（非全量重建）。
- 相同文本命中本地缓存，不再发 embedding 请求。
- YAML frontmatter 被剥离，不切成噪声 chunk。
- 未变更文件再次写入时跳过 embed。
- 新启用 embedding 后，旧 chunk 会自动补齐 embedding。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("AGENT_CONFIG_DIR", tempfile.mkdtemp(prefix="agent_mem_test_"))
os.environ.setdefault("AGENT_DATA_HOME", tempfile.mkdtemp(prefix="agent_mem_test_"))

from src.server.memory import (  # noqa: E402
    EmbeddingClient,
    EmbeddingModelConfig,
    MemoryVault,
)


class FakeEmbeddingClient(EmbeddingClient):
    """不真正发 HTTP，返回固定向量并记录调用。"""

    def __init__(self, cfg: EmbeddingModelConfig):
        super().__init__(cfg)
        self.call_count = 0
        self.call_texts: list[list[str]] = []

    def _fetch(self, texts):
        self.call_count += 1
        self.call_texts.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.fixture
def tmp_root() -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def test_frontmatter_stripped_from_chunks(tmp_root: Path):
    cfg = EmbeddingModelConfig(backend="openai", api_key="sk", model_name="test", dimensions=3)
    emb = FakeEmbeddingClient(cfg)
    vault = MemoryVault(tmp_root, emb)
    text = "---\nname: 2026-07-16\ndescription: daily note\n---\n\nReal content goes here."
    chunks = vault._chunk_text(text)
    assert len(chunks) > 0
    assert all("name:" not in c for c in chunks)
    assert all("description:" not in c for c in chunks)
    assert any("Real content" in c for c in chunks)


def test_embedding_cache_avoids_repeated_calls():
    cfg = EmbeddingModelConfig(
        backend="openai",
        api_key="sk",
        model_name="test",
        dimensions=3,
        enable_cache=True,
        max_cache_size=10,
    )
    emb = FakeEmbeddingClient(cfg)
    emb.embed(["hello"])
    assert emb.call_count == 1
    emb.embed(["hello"])  # 命中缓存
    assert emb.call_count == 1
    emb.embed(["hello", "world"])  # 只缺 world
    assert emb.call_count == 2
    # 关闭缓存后重新发请求
    cfg.enable_cache = False
    emb.embed(["hello"])
    assert emb.call_count == 3


def test_incremental_update_only_embeds_changed_file(tmp_root: Path):
    cfg = EmbeddingModelConfig(backend="openai", api_key="sk", model_name="test", dimensions=3)
    emb = FakeEmbeddingClient(cfg)
    vault = MemoryVault(tmp_root, emb)

    vault.add_daily_note("2026-07-16", "User likes apples.")
    assert emb.call_count == 1
    emb.call_count = 0

    # 写 dream 时只应 embed dream 文件，不应重 embed daily 文件
    vault.write_dream("User likes apples and bananas.")
    assert emb.call_count == 1
    assert all("apples and bananas" in t for batch in emb.call_texts[-1:] for t in batch)
    emb.call_count = 0

    # 用完全相同的内容再次更新同一个文件时应跳过 embed
    vault._update_index_for_rel(
        "dream/interests.md",
        vault.dream_dir.joinpath("interests.md").read_text(encoding="utf-8"),
    )
    assert emb.call_count == 0

    # 写新日期的 daily 笔记时应 embed 新文件
    vault.add_daily_note("2026-07-17", "User likes oranges.")
    assert emb.call_count == 1

    # 索引应包含两个文件的 chunk
    index = json.loads((tmp_root / "index.json").read_text(encoding="utf-8"))
    paths = {c["path"] for c in index["chunks"]}
    assert any("daily/2026-07-16" in p for p in paths)
    assert any("daily/2026-07-17" in p for p in paths)
    assert any("dream/interests" in p for p in paths)


def test_incremental_update_replaces_changed_file_chunks(tmp_root: Path):
    cfg = EmbeddingModelConfig(backend="openai", api_key="sk", model_name="test", dimensions=3)
    emb = FakeEmbeddingClient(cfg)
    vault = MemoryVault(tmp_root, emb)

    vault.add_daily_note("2026-07-16", "User likes apples.")
    first_chunks = len(json.loads((tmp_root / "index.json").read_text(encoding="utf-8"))["chunks"])
    emb.call_count = 0

    # 追加内容到同一文件，应替换旧 chunk 而不是叠加
    vault.add_daily_note("2026-07-16", "User likes bananas.")
    index = json.loads((tmp_root / "index.json").read_text(encoding="utf-8"))
    daily_chunks = [c for c in index["chunks"] if c["path"].startswith("daily/")]
    # 内容很短，不会被切成多段，所以 daily 文件应仍只有 1 个 chunk
    assert len(daily_chunks) == 1
    assert "bananas" in daily_chunks[0]["text"]
    assert emb.call_count == 1  # 只 embed 变更后的 daily 文件


def test_rebuild_index_adds_source_hash_and_incremental_skips_unchanged(tmp_root: Path):
    cfg = EmbeddingModelConfig(backend="openai", api_key="sk", model_name="test", dimensions=3)
    emb = FakeEmbeddingClient(cfg)
    vault = MemoryVault(tmp_root, emb)

    vault.add_daily_note("2026-07-16", "User likes apples.")
    emb.call_count = 0

    vault.rebuild_index()
    index = json.loads((tmp_root / "index.json").read_text(encoding="utf-8"))
    for c in index["chunks"]:
        assert c.get("source_hash")

    # rebuild 后再做完全相同的增量更新应跳过 embed
    vault._update_index_for_rel(
        "daily/2026-07-16.md",
        vault.daily_dir.joinpath("2026-07-16.md").read_text(encoding="utf-8"),
    )
    assert emb.call_count == 0


def test_embedding_backfill_when_newly_enabled(tmp_root: Path):
    cfg = EmbeddingModelConfig(backend="openai", api_key="", model_name="", dimensions=3)
    emb = FakeEmbeddingClient(cfg)
    vault = MemoryVault(tmp_root, emb)

    vault.add_daily_note("2026-07-16", "User likes apples.")
    index = json.loads((tmp_root / "index.json").read_text(encoding="utf-8"))
    assert all(c.get("embedding") is None for c in index["chunks"])

    # 启用 embedding 后，同一文件再次增量更新应补齐 embedding
    emb.cfg.model_name = "test"
    emb.cfg.api_key = "sk"
    emb.cfg.backend = "openai"
    vault._update_index_for_rel(
        "daily/2026-07-16.md",
        vault.daily_dir.joinpath("2026-07-16.md").read_text(encoding="utf-8"),
    )
    index = json.loads((tmp_root / "index.json").read_text(encoding="utf-8"))
    assert all(c.get("embedding") is not None for c in index["chunks"])
    assert emb.call_count == 1
