"""Governor 节点 + 路由（QwenPaw: loop/gates/runner.py 的 StopHandler + _filter_by_scope）。

Governor 是 LangGraph 图里一个独立节点，跑在每次"模型调用 + 工具执行"之后的
干净边界上。它调用 run_stop_gates（=QwenPaw 的 StopHandler），按决策更新状态：
- STOP        -> decision="stop"        -> 条件边路由到 END
- CONTINUE    -> decision="continue"      -> 路由回 agent（可注入续跑提示，反过早收工）
- ASK         -> decision="ask"          -> 路由到 hitl 节点（interrupt 等人审批）

与 QwenPaw 的关键差异：QwenPaw 在 `_reasoning()` 每轮中途插门，工具调用轮无法当场
STOP，所以要发明 `_gate_pending_stop` 延迟落槌的 hack。LangGraph 的 Governor 是独立
节点，天然站在 step 边界，STOP 在干净边界直接生效——hack 免了。
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END

from .gates.base import StopAction, run_stop_gates
from .gates.iteration import IterationGate
from .gates.doom_loop import DoomLoopGate
from .stability.observability import log_event

# Phase 1 默认门（后续 Phase 2+ 再加 MissionGate / BudgetGate / RubricGate）
DEFAULT_GATES = [IterationGate(max_iterations=30), DoomLoopGate()]


def make_governor(gates=None, *, metrics=None):
    gates = gates or DEFAULT_GATES

    def governor(state: dict) -> dict:
        turn = state.get("turn", 0) + 1
        # 复制一份，避免就地改 state（LangGraph 要求返回增量更新）
        gate_state = dict(state.get("gate_state", {}))
        result = run_stop_gates(gates, turn, state, gate_state)

        last = state["messages"][-1] if state.get("messages") else None
        updates = {"turn": turn, "gate_state": gate_state}

        if result is None:
            # 没有门干预时：模型给出"最终答案"（无工具调用）即视为完成 -> 停；
            # 否则（刚跑完工具）继续回 agent 消化结果。
            if isinstance(last, AIMessage) and not getattr(last, "tool_calls", None):
                updates["decision"] = "stop"
                updates["stop_reason"] = "model produced final answer"
            else:
                updates["decision"] = "continue"
            return updates

        if metrics is not None:
            metrics.gate(result.gate_name or "unknown")

        if result.action == StopAction.CONTINUE:
            updates["decision"] = "continue"
            if result.continuation_message:
                # 反过早收工：注入一条续跑提示，逼模型换个思路继续推进
                updates["messages"] = [
                    SystemMessage(content=result.continuation_message)
                ]
            return updates

        if result.action == StopAction.STOP:
            updates["decision"] = "stop"
            updates["stop_reason"] = result.reason or "gate stop"
            log_event("governor_stop", reason=updates["stop_reason"], gate=result.gate_name)
            return updates

        if result.action == StopAction.ASK:
            updates["decision"] = "ask"
            updates["stop_reason"] = result.reason or "human approval required"
            updates["pending_after_hitl"] = "agent"  # 工具后裁决：恢复后重新评估
            log_event("governor_ask", reason=updates["stop_reason"], gate=result.gate_name)
            return updates

        return updates

    return governor


def route_after_governor(state: dict) -> str:
    decision = state.get("decision")
    if decision == "stop":
        return END
    if decision == "ask":
        return "hitl"
    # 继续：先回 context 节点重新折叠/召回窗口，再进 agent
    return "context"
