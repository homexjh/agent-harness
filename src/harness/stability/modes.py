"""MissionGate —— 任务级门控（多 Mode 的核心闸门）。

对照 QwenPaw：mission/session 级别的"做到哪了"判定。
- 从 prd.json 读取完成标记（completion_markers）与硬上限（max_turns）；
- 最近一条 assistant 消息命中任一标记 -> STOP（mission complete）；
- 超过 max_turns -> STOP（mission timeout，避免无限打转）。
"""
from __future__ import annotations

import json
import os
from typing import Optional

from ..gates.base import StopAction, StopHandlerResult, StopGate


class MissionGate(StopGate):
    def __init__(
        self,
        prd_path: Optional[str] = None,
        completion_markers: Optional[list] = None,
        max_turns: int = 50,
        priority: int = 90,
    ):
        # StopGate 无 __init__，直接设实例属性（覆盖类默认值）
        self.name = "MissionGate"
        self.priority = priority
        self.prd_path = prd_path
        self.completion_markers = completion_markers or []
        self.max_turns = max_turns
        if prd_path and os.path.exists(prd_path):
            try:
                with open(prd_path, "r", encoding="utf-8") as f:
                    prd = json.load(f)
                self.completion_markers = prd.get("completion_markers", self.completion_markers)
                self.max_turns = prd.get("max_turns", self.max_turns)
            except Exception:
                pass

    def check(self, turn: int, state, gate_state: dict) -> Optional[StopHandlerResult]:
        from langchain_core.messages import AIMessage

        if turn >= self.max_turns:
            return StopHandlerResult(
                action=StopAction.STOP, reason=f"mission timeout (max_turns={self.max_turns})"
            )
        msgs = state.get("messages", [])
        if msgs:
            last = msgs[-1]
            if isinstance(last, AIMessage):
                text = last.content if isinstance(last.content, str) else ""
                for m in self.completion_markers:
                    if m and m in text:
                        return StopHandlerResult(
                            action=StopAction.STOP, reason=f"mission complete (marker: {m})"
                        )
        return None
