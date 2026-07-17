"""System-prompt assembly via a pluggable contributor pipeline.

This mirrors QwenPaw's ``runtime/prompt_contributors.py`` + ``prompt_manager.py``:
a :class:`PromptManager` holds a list of :class:`PromptContributor` objects, each
responsible for exactly one fragment of the system prompt. They run in ascending
``priority`` order and their non-empty outputs are joined into the final prompt.

Why a pipeline instead of the old single-function concatenation in
``core_files.build_system_prompt``? It lets us add prompt blocks (environment
context, scroll-context guidance, driver hints, ...) without editing a central
function, and lets each block opt out per request. This is the B-lite alignment:
the skeleton + the two highest-value contributors (``EnvContext``, ``ScrollContext``)
plus the existing workspace-files / memory / mode hints wrapped as contributors.
The remaining slots (AgentIdentity / Multimodal / CodingMode / DriverPolicy) are
registered as explicit **placeholder** contributors returning ``None`` so the
extension points are visible and ready to fill in.
"""
from __future__ import annotations

import datetime
import inspect
import logging
import platform
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PROMPT_SEPARATOR = "\n\n"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class PromptConfig(BaseModel):
    """Toggles for the built-in prompt contributors.

    Defaults match QwenPaw's out-of-the-box behaviour; everything useful is on.
    """

    enable_workspace_files: bool = True
    enable_mode_hint: bool = True
    enable_auto_memory_search: bool = True
    enable_scroll_context: bool = True
    enable_env_context: bool = True


_CONFIG: Optional[PromptConfig] = None


def get_prompt_config() -> PromptConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = PromptConfig()
    return _CONFIG


def set_prompt_config(cfg: PromptConfig) -> None:
    global _CONFIG
    _CONFIG = cfg


# ---------------------------------------------------------------------------
# Context passed to every contributor for one request
# ---------------------------------------------------------------------------
@dataclass
class PromptContext:
    user_id: str = "default"
    thread_id: str = "default"
    messages: list = field(default_factory=list)
    last_user_text: str = ""
    core_files_manager: Any = None
    memory_manager: Any = None
    mode_hint: Optional[str] = None
    config: Optional[PromptConfig] = None

    def __post_init__(self) -> None:
        if self.config is None:
            self.config = get_prompt_config()


# ---------------------------------------------------------------------------
# Contributor base classes (parity with QwenPaw)
# ---------------------------------------------------------------------------
class PromptContributor:
    """Single-responsibility producer of one system-prompt fragment.

    Subclasses set ``name`` (unique within a manager, used for replacement /
    disable) and ``priority`` (ascending — lower runs first / appears earlier
    in the final prompt). ``contribute`` may return ``None`` or an empty string
    to opt out for the current request.
    """

    name: str
    priority: int = 100

    async def contribute(self, ctx: PromptContext) -> Optional[str]:  # noqa: D401
        raise NotImplementedError

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        raise NotImplementedError


class SyncPromptContributor(PromptContributor):
    """Convenience base for contributors that have no async work to do.

    ``context_node`` in the graph is a plain synchronous function, so all
    built-in contributors use the sync path.
    """

    async def contribute(self, ctx: PromptContext) -> Optional[str]:
        return self.contribute_sync(ctx)


# ---------------------------------------------------------------------------
# Real contributors
# ---------------------------------------------------------------------------
class WorkspacePromptFilesContributor(SyncPromptContributor):
    """Load AGENTS/SOUL/PROFILE (and other enabled core files).

    Replaces the previous ``core_files.build_system_prompt()`` call that was
    inlined in the graph's context node.
    """

    name = "workspace_prompt_files"
    priority = 20

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        if not ctx.config.enable_workspace_files:
            return None
        cfm = ctx.core_files_manager
        if cfm is None:
            return None
        try:
            prompt = cfm.build_system_prompt()
        except Exception:  # noqa: BLE001
            logger.exception("workspace prompt files failed; skipping")
            return None
        return prompt or None


class ModeHintContributor(SyncPromptContributor):
    """Append the mode-specific hint (e.g. agentmode guidance).

    Carries the previous ``system_hint`` argument through the pipeline so the
    graph does not need to special-case it.
    """

    name = "mode_hint"
    priority = 25

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        if not ctx.config.enable_mode_hint:
            return None
        hint = (ctx.mode_hint or "").strip()
        return hint or None


class MemoryContributor(SyncPromptContributor):
    """Inject retrieved long-term memory before the agent replies.

    Wraps the previous inline ``mm.memory_search(last_user_text, ...)`` block.
    Auto memory *writing* (``mm.auto_memory``) stays in the graph node — it is
    a persistence task, not prompt assembly.
    """

    name = "memory"
    priority = 80

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        if not ctx.config.enable_auto_memory_search:
            return None
        mm = ctx.memory_manager
        if mm is None or not getattr(mm, "cfg", None):
            return None
        ams = mm.cfg.reme_light_memory_config.auto_memory_search_config
        if not ams.enabled:
            return None
        last_user = (ctx.last_user_text or "").strip()
        if not last_user:
            return None
        try:
            hit = mm.memory_search(last_user, ams.max_results)
        except Exception:  # noqa: BLE001
            logger.exception("memory search failed; skipping")
            return None
        if not hit or hit.startswith(f"memory_search('{last_user}'): no"):
            return None
        return (
            "## Retrieved long-term memory\n"
            + hit
            + "\n(Use this to inform your response when relevant.)"
        )


class ScrollContextContributor(SyncPromptContributor):
    """Teach the model about the scroll-context fold/recall discipline.

    Without this, the model sees a ``[CONTEXT FOLD]`` stub but does not know it
    can restore the folded turns via the ``recall`` tool.
    """

    name = "scroll_context"
    priority = 86

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        if not ctx.config.enable_scroll_context:
            return None
        return (
            "# Context Management\n"
            "This conversation uses scroll context: older turns beyond the "
            "context budget are folded into a compact map stub. You can restore "
            "them on demand with the recall tool:\n"
            "- recall(seq=N): restore turn N verbatim.\n"
            "- recall(\"keyword\"): search folded history by keyword (or by "
            "semantic similarity when available).\n"
            "When relevant history may have been folded, prefer recalling it "
            "before answering from assumptions."
        )


class EnvContextContributor(SyncPromptContributor):
    """Append environment context: current local time, OS, session id.

    This is the single biggest long-session win — the model otherwise only
    learns the time by calling a ``get_current_time`` tool. The timestamp is
    refreshed on every context-node pass (each loop iteration), so it stays
    accurate across a long session.
    """

    name = "env_context"
    priority = 90

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        if not ctx.config.enable_env_context:
            return None
        now = datetime.datetime.now().astimezone()
        try:
            plat = platform.platform()
        except Exception:  # noqa: BLE001
            plat = "unknown"
        lines = [
            "# Environment Context",
            f"- Current local time: {now.strftime('%Y-%m-%d %H:%M:%S %z')}",
            f"- Platform: {plat}",
            f"- Session/thread id: {ctx.thread_id}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Placeholder contributors (extension points; currently opt out)
# ---------------------------------------------------------------------------
class AgentIdentityContributor(SyncPromptContributor):
    """Reserved for multi-agent identity injection. No-op until wired up."""

    name = "agent_identity"
    priority = 10

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        return None


class MultimodalHintContributor(SyncPromptContributor):
    """Reserved for multimodal capability hints. No-op until wired up."""

    name = "multimodal_hint"
    priority = 40

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        return None


class CodingModeContributor(SyncPromptContributor):
    """Reserved for coding-mode hints. No-op until wired up."""

    name = "coding_mode"
    priority = 45

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        return None


class DriverPolicyHintContributor(SyncPromptContributor):
    """Reserved for request-time driver policy hints. No-op until wired up."""

    name = "driver_policy_hint"
    priority = 88

    def contribute_sync(self, ctx: PromptContext) -> Optional[str]:
        return None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
_ALL_CONTRIBUTORS = (
    AgentIdentityContributor,
    WorkspacePromptFilesContributor,
    ModeHintContributor,
    MultimodalHintContributor,
    CodingModeContributor,
    MemoryContributor,
    ScrollContextContributor,
    DriverPolicyHintContributor,
    EnvContextContributor,
)


class PromptManager:
    """Hold contributors and assemble the final system prompt per request."""

    def __init__(self) -> None:
        self._contributors: list[PromptContributor] = []

    # ---------------------------------------------------------------- register
    def register(self, contributor: PromptContributor) -> None:
        if not isinstance(contributor, PromptContributor):
            raise TypeError(
                "register() requires a PromptContributor, "
                f"got {type(contributor).__name__}"
            )
        if not getattr(contributor, "name", None):
            raise ValueError("PromptContributor.name must be a non-empty string")
        if any(c.name == contributor.name for c in self._contributors):
            raise ValueError(
                f"prompt contributor {contributor.name!r} already registered"
            )
        self._contributors.append(contributor)
        self._contributors.sort(key=lambda c: c.priority)

    def names(self) -> list[str]:
        return [c.name for c in self._contributors]

    def __len__(self) -> int:
        return len(self._contributors)

    # ------------------------------------------------------------------ build
    def build_sync(self, ctx: PromptContext) -> str:
        """Run every contributor in priority order, join non-empty pieces.

        A contributor raising is logged and skipped — one broken contributor
        must not take down prompt assembly for the whole request.
        """
        parts: list[str] = []
        for c in self._contributors:
            try:
                if isinstance(c, SyncPromptContributor):
                    fragment = c.contribute_sync(ctx)
                else:
                    raw = c.contribute(ctx)
                    fragment = raw if not inspect.isawaitable(raw) else None
            except Exception:  # noqa: BLE001
                logger.exception("prompt contributor %s failed; skipping", c.name)
                continue
            if fragment and fragment.strip():
                parts.append(fragment.strip())
        return PROMPT_SEPARATOR.join(parts)


def build_default_prompt_manager() -> PromptManager:
    """Create a :class:`PromptManager` with all built-in contributors."""
    pm = PromptManager()
    for cls in _ALL_CONTRIBUTORS:
        pm.register(cls())
    return pm


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_pm: Optional[PromptManager] = None


def get_prompt_manager() -> PromptManager:
    global _pm
    if _pm is None:
        _pm = build_default_prompt_manager()
    return _pm


def reset_prompt_manager() -> PromptManager:
    global _pm
    _pm = build_default_prompt_manager()
    return _pm


__all__ = [
    "PromptConfig",
    "get_prompt_config",
    "set_prompt_config",
    "PromptContext",
    "PromptContributor",
    "SyncPromptContributor",
    "WorkspacePromptFilesContributor",
    "ModeHintContributor",
    "MemoryContributor",
    "ScrollContextContributor",
    "EnvContextContributor",
    "AgentIdentityContributor",
    "MultimodalHintContributor",
    "CodingModeContributor",
    "DriverPolicyHintContributor",
    "PromptManager",
    "build_default_prompt_manager",
    "get_prompt_manager",
    "reset_prompt_manager",
    "PROMPT_SEPARATOR",
]
