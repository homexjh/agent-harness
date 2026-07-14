"""IterationGate（QwenPaw: loop/gates/iteration.py 的 IterationGate，priority=10）。

硬迭代上限——整个 harness 的"最后安全网"。无论模型怎么跑，轮次到顶必须停。
"""
from __future__ import annotations

from .base import StopGate, StopHandlerResult, StopAction


class IterationGate(StopGate):
    name = "iteration"
    priority = 10  # 最高优先级（数字最小），任何门都先过它

    def __init__(self, max_iterations: int = 30):
        self.max_iterations = max_iterations

    def check(self, turn: int, state: dict, gate_state: dict) -> StopHandlerResult | None:
        if turn >= self.max_iterations:
            return StopHandlerResult(
                StopAction.STOP,
                reason=f"iteration cap {self.max_iterations} reached",
            )
        return None
