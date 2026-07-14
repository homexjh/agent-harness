"""Loop Gates 纯逻辑单测（零依赖，直接 `python tests/test_gates.py` 即可跑）。

验证 harness 的"大脑"：IterationGate / DoomLoopGate / StopHandler 调度，
不依赖 langgraph / langchain / 网络 / API key。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from harness.gates.base import StopAction, StopGate, StopHandlerResult, run_stop_gates
from harness.gates.doom_loop import DoomLoopGate
from harness.gates.iteration import IterationGate


class _Msg:
    """测试用的极简消息替身，只需有 content 属性。"""

    def __init__(self, content):
        self.content = content


def test_iteration_cap():
    gate = IterationGate(max_iterations=3)
    assert gate.check(2, {}, {}) is None
    r = gate.check(3, {}, {})
    assert r is not None and r.action == StopAction.STOP
    assert "3" in (r.reason or "")


def test_doom_detects_repetition():
    gate = DoomLoopGate(window=4, stop_threshold=0.8, warn_threshold=0.5, max_warns=1)
    state = {"messages": [_Msg("repeat")]}
    gstate = {}  # 必须跨轮复用同一个 gate_state，history 才累积（=会话隔离的持久化）
    assert gate.check(1, state, gstate) is None  # 仅 1 步，相似度 0
    # 第 2 步起完全重复 -> 相似度 1.0 -> 直接 STOP
    r = gate.check(2, state, gstate)
    assert r is not None and r.action == StopAction.STOP


def test_doom_warns_before_stop():
    # 用真实 state 驱动，让 gate 自己累积 history（=生产里随 thread_id 持久化的行为）。
    gate = DoomLoopGate(window=10, stop_threshold=0.95, warn_threshold=0.4, max_warns=2)
    gstate = {}
    sa = {"messages": [_Msg("a")]}
    sb = {"messages": [_Msg("b")]}
    # 交替不同内容：history=[fa,fb,fa] -> unique=2,total=3 -> sim=0.5 -> 落在 warn 区间
    gate.check(1, sa, gstate)
    gate.check(2, sb, gstate)
    r1 = gate.check(3, sa, gstate)
    assert r1 is not None and r1.action == StopAction.CONTINUE

    # 全重复内容 -> history=[fx,fx] -> sim=1.0 -> STOP（多级升级的最终档）
    g2 = {}
    sx = {"messages": [_Msg("x")]}
    gate2 = DoomLoopGate(window=10, stop_threshold=0.95, warn_threshold=0.4, max_warns=2)
    gate2.check(1, sx, g2)
    r2 = gate2.check(2, sx, g2)
    assert r2 is not None and r2.action == StopAction.STOP


def test_priority_short_circuit():
    # 低 priority（数字小）的门先被评估，第一个非 None 结果短路生效
    class Stopper(StopGate):
        name = "stopper"
        priority = 5

        def check(self, turn, state, gs):
            return StopHandlerResult(StopAction.STOP, reason="early")

    class PassThrough(StopGate):
        name = "pass"
        priority = 50

        def check(self, turn, state, gs):
            return StopHandlerResult(
                StopAction.CONTINUE, continuation_message="keep going"
            )

    # Stopper(5) 先于 PassThrough(50) -> 返回 STOP
    res = run_stop_gates([PassThrough(), Stopper()], turn=1, state={}, gate_state={})
    assert res is not None and res.action == StopAction.STOP and res.reason == "early"

    # 把 Stopper 放到 high priority 仍先触发（数字小）
    res2 = run_stop_gates([Stopper(), PassThrough()], turn=1, state={}, gate_state={})
    assert res2.action == StopAction.STOP

    # 没有门干预时返回 None
    res3 = run_stop_gates([IterationGate(100)], turn=1, state={}, gate_state={})
    assert res3 is None


if __name__ == "__main__":
    test_iteration_cap()
    test_doom_detects_repetition()
    test_doom_warns_before_stop()
    test_priority_short_circuit()
    print("ALL GATE TESTS PASSED")
