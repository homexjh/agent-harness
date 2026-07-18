import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from server.prompt_contributors import (
    PromptContributor,
    SyncPromptContributor,
    PromptManager,
    PromptContext,
    PromptConfig,
    get_prompt_config,
    set_prompt_config,
    WorkspacePromptFilesContributor,
    ModeHintContributor,
    MemoryContributor,
    ScrollContextContributor,
    EnvContextContributor,
    AgentIdentityContributor,
    MultimodalHintContributor,
    is_multimodal_model,
    CodingModeContributor,
    DriverPolicyHintContributor,
    build_default_prompt_manager,
    get_prompt_manager,
    reset_prompt_manager,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeCoreFiles:
    def __init__(self, prompt="# AGENTS\ncontent"):
        self._prompt = prompt
        self.called = 0

    def build_system_prompt(self):
        self.called += 1
        return self._prompt


class FakeCoreFilesRaise:
    def build_system_prompt(self):
        raise RuntimeError("boom")


class _FakeAMS:
    enabled = True
    max_results = 5


class _FakeRLC:
    auto_memory_search_config = _FakeAMS()


class FakeMemoryConfig:
    reme_light_memory_config = _FakeRLC()


class FakeMemory:
    def __init__(self, hits=True):
        self.cfg = FakeMemoryConfig()
        self._hits = hits

    def memory_search(self, query, max_results=5):
        if not self._hits or "miss" in query:
            return f"memory_search('{query}'): no relevant memory found."
        return f"memory about {query}"


class FakeMemoryRaise:
    cfg = FakeMemoryConfig()

    def memory_search(self, query, max_results=5):
        raise RuntimeError("search boom")


class _RaisingContributor(SyncPromptContributor):
    name = "raiser"
    priority = 1

    def contribute_sync(self, ctx):
        raise RuntimeError("contrib boom")


class _StaticContributor(SyncPromptContributor):
    def __init__(self, name, priority, text):
        self.name = name
        self.priority = priority
        self._text = text

    def contribute_sync(self, ctx):
        return self._text


def _ctx(**kw):
    base = dict(
        user_id="default",
        thread_id="t1",
        messages=[],
        last_user_text="上海天气",
        core_files_manager=None,
        memory_manager=None,
        mode_hint=None,
        config=PromptConfig(),
    )
    base.update(kw)
    return PromptContext(**base)


# ---------------------------------------------------------------------------
# PromptManager registration
# ---------------------------------------------------------------------------
def test_register_rejects_non_contributor():
    pm = PromptManager()
    with pytest.raises(TypeError):
        pm.register(object())


def test_register_rejects_duplicate_name():
    pm = PromptManager()
    pm.register(_StaticContributor("dup", 1, "a"))
    with pytest.raises(ValueError):
        pm.register(_StaticContributor("dup", 2, "b"))


def test_register_rejects_empty_name():
    class NoName(SyncPromptContributor):
        priority = 1

        def contribute_sync(self, ctx):
            return None

    pm = PromptManager()
    with pytest.raises(ValueError):
        pm.register(NoName())


def test_register_sorts_by_priority():
    pm = PromptManager()
    pm.register(_StaticContributor("c", 30, "C"))
    pm.register(_StaticContributor("a", 10, "A"))
    pm.register(_StaticContributor("b", 20, "B"))
    assert pm.names() == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# PromptManager.build_sync
# ---------------------------------------------------------------------------
def test_build_sync_joins_non_empty():
    pm = PromptManager()
    pm.register(_StaticContributor("a", 10, "AAA"))
    pm.register(_StaticContributor("b", 20, "BBB"))
    out = pm.build_sync(_ctx())
    assert out == "AAA\n\nBBB"


def test_build_sync_skips_none_and_empty():
    pm = PromptManager()
    pm.register(_StaticContributor("a", 10, "AAA"))
    pm.register(_StaticContributor("none", 20, None))
    pm.register(_StaticContributor("empty", 30, "   "))
    out = pm.build_sync(_ctx())
    assert out == "AAA"


def test_build_sync_skips_raising_contributor():
    pm = PromptManager()
    pm.register(_RaisingContributor())
    pm.register(_StaticContributor("ok", 10, "OK"))
    out = pm.build_sync(_ctx())
    assert out == "OK"


# ---------------------------------------------------------------------------
# Real contributors
# ---------------------------------------------------------------------------
def test_workspace_contributor_returns_prompt():
    c = WorkspacePromptFilesContributor()
    ctx = _ctx(core_files_manager=FakeCoreFiles("# AGENTS\nhi"))
    out = c.contribute_sync(ctx)
    assert out == "# AGENTS\nhi"
    assert ctx.core_files_manager.called == 1


def test_workspace_contributor_disabled_returns_none():
    c = WorkspacePromptFilesContributor()
    ctx = _ctx(core_files_manager=FakeCoreFiles(), config=PromptConfig(enable_workspace_files=False))
    assert c.contribute_sync(ctx) is None


def test_workspace_contributor_handles_raise():
    c = WorkspacePromptFilesContributor()
    ctx = _ctx(core_files_manager=FakeCoreFilesRaise())
    assert c.contribute_sync(ctx) is None


def test_mode_hint_contributor():
    c = ModeHintContributor()
    assert c.contribute_sync(_ctx(mode_hint="be terse")) == "be terse"
    assert c.contribute_sync(_ctx(mode_hint=None)) is None
    assert (
        c.contribute_sync(_ctx(mode_hint="x", config=PromptConfig(enable_mode_hint=False)))
        is None
    )


def test_memory_contributor_hit():
    c = MemoryContributor()
    ctx = _ctx(memory_manager=FakeMemory(hits=True))
    out = c.contribute_sync(ctx)
    assert out is not None
    assert out.startswith("## Retrieved long-term memory")
    assert "memory about 上海天气" in out


def test_memory_contributor_miss_returns_none():
    c = MemoryContributor()
    ctx = _ctx(memory_manager=FakeMemory(hits=False))
    assert c.contribute_sync(ctx) is None


def test_memory_contributor_no_last_user():
    c = MemoryContributor()
    ctx = _ctx(memory_manager=FakeMemory(hits=True), last_user_text="")
    assert c.contribute_sync(ctx) is None


def test_memory_contributor_no_manager():
    c = MemoryContributor()
    assert c.contribute_sync(_ctx(memory_manager=None)) is None


def test_memory_contributor_disabled():
    c = MemoryContributor()
    ctx = _ctx(memory_manager=FakeMemory(hits=True), config=PromptConfig(enable_auto_memory_search=False))
    assert c.contribute_sync(ctx) is None


def test_memory_contributor_handles_raise():
    c = MemoryContributor()
    ctx = _ctx(memory_manager=FakeMemoryRaise())
    assert c.contribute_sync(ctx) is None


def test_scroll_contributor_mentions_recall():
    c = ScrollContextContributor()
    out = c.contribute_sync(_ctx())
    assert "recall(seq=" in out and 'recall("keyword"' in out


def test_scroll_contributor_disabled():
    c = ScrollContextContributor()
    ctx = _ctx(config=PromptConfig(enable_scroll_context=False))
    assert c.contribute_sync(ctx) is None


def test_env_contributor_has_time_and_platform():
    c = EnvContextContributor()
    out = c.contribute_sync(_ctx(thread_id="t-xyz"))
    assert "Current local time:" in out
    assert "Platform:" in out
    assert "t-xyz" in out


def test_env_contributor_disabled():
    c = EnvContextContributor()
    ctx = _ctx(config=PromptConfig(enable_env_context=False))
    assert c.contribute_sync(ctx) is None


# ---------------------------------------------------------------------------
# Placeholder contributors
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls",
    [
        AgentIdentityContributor,
        CodingModeContributor,
        DriverPolicyHintContributor,
    ],
)
def test_placeholder_contributors_opt_out(cls):
    assert cls().contribute_sync(_ctx()) is None


# ---------------------------------------------------------------------------
# MultimodalHint (capability-gated, no longer a pure placeholder)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-vision-preview",
        "gpt-4-turbo",
        "gpt-4.1",
        "gpt-4.5",
        "qwen2.5-vl",
        "qwen-vl-plus",
        "qvq-vl",
        "gemini-1.5-pro",
        "gemini-2.0-flash",
        "claude-3-opus",
        "claude-sonnet-4",
        "pixtral-12b",
        "llama-3.2-vision",
        "llama-4-scout",
        "glm-4v",
        "kimi-vl",
        "internvl2",
        "deepseek-vl",
        "moondream",
    ],
)
def test_is_multimodal_model_detects_known(model):
    assert is_multimodal_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "",
        "deepseek-chat",
        "deepseek-v3",
        "gpt-4",            # base 4.x without vision suffix
        "gpt-3.5-turbo",
        "text-embedding-3-small",
        "qwen-plus",
        "qwen-max",
        "qwq-32b",
        "llama-3.1-8b",
    ],
)
def test_is_multimodal_model_rejects_unknown(model):
    assert is_multimodal_model(model) is False


def test_multimodal_contributor_injects_when_vision():
    c = MultimodalHintContributor()
    out = c.contribute_sync(_ctx(model_name="gpt-4o"))
    assert out is not None
    assert "desktop_screenshot" in out
    assert "view_image" in out
    assert "Multimodal / Vision" in out


def test_multimodal_contributor_skips_when_text_only():
    c = MultimodalHintContributor()
    assert c.contribute_sync(_ctx(model_name="deepseek-chat")) is None
    assert c.contribute_sync(_ctx(model_name="")) is None
    assert c.contribute_sync(_ctx(model_name=None)) is None


def test_multimodal_contributor_disabled():
    c = MultimodalHintContributor()
    ctx = _ctx(model_name="gpt-4o", config=PromptConfig(enable_multimodal_hint=False))
    assert c.contribute_sync(ctx) is None


# ---------------------------------------------------------------------------
# Default manager + singleton
# ---------------------------------------------------------------------------
def test_default_manager_has_all_contributors():
    pm = build_default_prompt_manager()
    assert pm.names() == [
        "agent_identity",
        "workspace_prompt_files",
        "mode_hint",
        "multimodal_hint",
        "coding_mode",
        "memory",
        "scroll_context",
        "driver_policy_hint",
        "env_context",
    ]


def test_singleton_returns_same_instance():
    a = get_prompt_manager()
    b = get_prompt_manager()
    assert a is b
    reset_prompt_manager()
    c = get_prompt_manager()
    assert c is not a
    # restore
    reset_prompt_manager()


def test_build_default_produces_non_empty_prompt():
    pm = build_default_prompt_manager()
    ctx = _ctx(
        core_files_manager=FakeCoreFiles("# AGENTS\nhi"),
        memory_manager=FakeMemory(hits=True),
        mode_hint="mode hint here",
    )
    out = pm.build_sync(ctx)
    # workspace + mode hint + memory + scroll + env all present
    assert "# AGENTS" in out
    assert "mode hint here" in out
    assert "Retrieved long-term memory" in out
    assert "Context Management" in out
    assert "Environment Context" in out
    # agent_identity / multimodal / coding / driver placeholders opt out
    assert "agent_identity" not in out
