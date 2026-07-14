"""DoomLoopGate（QwenPaw: loop/gates/doom_loop.py 的 DoomLoopGate，旁挂）。

检测"鬼打墙"——模型在重复高度相似的步骤。用滑窗相似度：
    similarity = 1 - (unique - 1) / (total - 1)
多级升级：先 CONTINUE 警告（注入续跑提示逼模型换个思路），再 STOP。

QwenPaw 用的是词级去重启发式；这里用"最近若干步消息指纹"的相似度，
更贴合 LangGraph 的消息流，且同样零依赖。
"""
from __future__ import annotations

import hashlib

from .base import StopGate, StopHandlerResult, StopAction


class DoomLoopGate(StopGate):
    name = "doom_loop"
    priority = 70  # 旁挂：低于 MissionGate(50)? 实际在 QwenPaw 是旁挂门，这里取 70

    def __init__(
        self,
        window: int = 6,
        stop_threshold: float = 0.85,
        warn_threshold: float = 0.6,
        max_warns: int = 2,
    ):
        self.window = window
        self.stop_threshold = stop_threshold
        self.warn_threshold = warn_threshold
        self.max_warns = max_warns

    def _fingerprint(self, state: dict) -> str:
        msgs = state.get("messages", [])
        sig = ""
        for m in msgs[-self.window :]:
            sig += repr(getattr(m, "content", ""))
        return hashlib.md5(sig.encode()).hexdigest()

    @staticmethod
    def _similarity(items: list) -> float:
        total = len(items)
        unique = len(set(items))
        if total <= 1:
            return 0.0
        return 1 - (unique - 1) / (total - 1)

    def check(self, turn: int, state: dict, gate_state: dict) -> StopHandlerResult | None:
        fp = self._fingerprint(state)
        history = gate_state.setdefault("history", [])
        history.append(fp)
        if len(history) > self.window:
            del history[: -self.window]

        # 关键：相似度基于"跨轮累积的最近若干步指纹"，不是本地 messages 条数。
        # 只有 >=2 步才有比较意义（QwenPaw: 1-(unique-1)/(total-1)，total<=1 返回 0）。
        sim = self._similarity(history)
        warns = gate_state.get("warns", 0)

        if sim >= self.stop_threshold:
            return StopHandlerResult(
                StopAction.STOP,
                reason=f"doom loop detected (similarity={sim:.2f})",
            )
        if sim >= self.warn_threshold and warns < self.max_warns:
            gate_state["warns"] = warns + 1
            return StopHandlerResult(
                StopAction.CONTINUE,
                continuation_message=(
                    "WARNING: 你似乎在重复高度相似的步骤（鬼打墙）。"
                    "请换一种能实质性推进任务的新动作，不要重复已有操作。"
                ),
                reason=f"doom loop warning (similarity={sim:.2f})",
            )
        return None
