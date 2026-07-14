# -*- coding: utf-8 -*-
"""agent-harness 的 Context Manager 配置（QwenPaw LightContextCard 等效）。

对齐 QwenPaw 三层记忆架构中的第二层——上下文管理（Scroll Context / LightContextCard）：

- ``budget_tokens``：上下文预算阈值。超过后从最旧往新折叠（fold-not-summarize），
  与 QwenPaw 的"阈值压缩"一致。
- ``enable_recall``：是否允许 agent 在**同一 thread 内**显式 recall 还原被折叠的历史
  （QwenPaw 的 recall_history / 召回沙箱门控）。
- ``strip_media``：把历史里的 base64 图片/音视频从上下文剥离省 token（QwenPaw 媒体降级）。
- ``max_tool_result_chars``：工具结果裁剪上限（QwenPaw 的 ToolResultPruningMiddleware 等效）；
  0 = 不裁剪。超出部分在上下文里截断，但原始全文仍保留在 store，recall 可还原。

配置持久化到 ``DATA_HOME/context_config.json``（即 ~/.agent-harness），与 memory_config.json 同目录。
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from .config import DATA_HOME
from .user_ctx import get_user


def _workbuddy_dir() -> Path:
    # agent-harness 独立数据目录（不再使用 ~/.workbuddy，那是 WorkBuddy IDE 的数据目录）
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    return DATA_HOME


def _config_path(user_id: str | None = None) -> Path:
    """按用户隔离的 context 配置路径：DATA_HOME/{user_id}/context_config.json。"""
    uid = user_id or get_user()
    d = _workbuddy_dir() / uid
    d.mkdir(parents=True, exist_ok=True)
    return d / "context_config.json"


CONFIG_PATH = _config_path()  # 兼容引用（默认用户）；实际读写走 _config_path()


# ---------------------------------------------------------------------------
# 配置模型（镜像 QwenPaw LightContextCard）
# ---------------------------------------------------------------------------
class ContextManagerConfig(BaseModel):
    # 上下文预算阈值（token）。超过后最旧 turns 折叠。
    budget_tokens: int = 8000
    # 允许 thread 内显式 recall 还原折叠历史。
    enable_recall: bool = True
    # 历史里的 base64 媒体从上下文剥离（省 token）。
    strip_media: bool = True
    # 工具结果裁剪上限（字符）。0 = 不裁剪。
    max_tool_result_chars: int = 0


def default_config() -> ContextManagerConfig:
    return ContextManagerConfig()


def load_config(user_id: str | None = None) -> ContextManagerConfig:
    p = _config_path(user_id)
    if p.exists():
        try:
            return ContextManagerConfig(
                **json.loads(p.read_text(encoding="utf-8"))
            )
        except Exception:
            return ContextManagerConfig()
    return ContextManagerConfig()


def save_config(cfg: ContextManagerConfig, user_id: str | None = None) -> None:
    p = _config_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
