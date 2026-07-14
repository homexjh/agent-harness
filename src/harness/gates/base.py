"""Loop Gates 的基础抽象（QwenPaw: StopGate / StopHandlerResult / StopHandler）。

这是 harness 的"大脑"，**零依赖**（不引入 langgraph / langchain），
所以能脱离任何框架单独单测——也是 QwenPaw 最该借鉴、最该先落地的部分。

QwenPaw 源码映射：
- StopGate          ≈ loop/gates/base.py 的 StopGate(ABC)
- StopHandlerResult ≈ loop/gates/base.py 的 StopHandlerResult(action, continuation_message, reason)
- run_stop_gates    ≈ loop/gates/runner.py 的 run_stop_handlers（priority 升序短路）
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class StopAction(str, Enum):
    CONTINUE = "continue"  # 反过早收工：强制再转一圈（可注入续跑提示）
    STOP = "stop"         # 终止整个循环 -> END
    ASK = "ask"           # 需要人工审批 -> interrupt()


@dataclass
class StopHandlerResult:
    action: StopAction
    continuation_message: Optional[str] = None
    reason: Optional[str] = None
    gate_name: Optional[str] = None  # 由 run_stop_gates 回填，便于可观测


class StopGate:
    """一个循环门。

    子类实现 `check(turn, state, gate_state)`：
    - turn        : 当前循环轮次（每经过一次 governor 自增）
    - state       : 完整 agent 状态（只读）
    - gate_state  : **本 gate 自己的、按会话隔离的可变状态字典**——把内部状态写这里，
                    它会随 graph state 被 checkpointer 按 thread_id 持久化
                    （等价于 QwenPaw 把状态存在 LoopGate._sessions[session_id]）。
    返回 StopHandlerResult 表示要干预，返回 None 表示不干预。
    """

    name: str = "base"
    priority: int = 100  # 数字越小越有话语权，越先被评估

    def check(
        self, turn: int, state: dict, gate_state: dict
    ) -> Optional[StopHandlerResult]:
        raise NotImplementedError


def run_stop_gates(
    gates: list[StopGate], turn: int, state: dict, gate_state: dict
) -> Optional[StopHandlerResult]:
    """QwenPaw 的 StopHandler：按 priority 升序遍历，第一个非 None 结果短路生效。

    gate_state 形如 {gate_name: { ... 该门内部状态 ... }}，按门名分桶 = 会话隔离。
    """
    for gate in sorted(gates, key=lambda g: g.priority):
        gslice = gate_state.setdefault(gate.name, {})
        result = gate.check(turn, state, gslice)
        if result is not None:
            result.gate_name = gate.name
            return result
    return None
