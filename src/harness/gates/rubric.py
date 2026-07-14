"""StandaloneRubricGate（QwenPaw: loop/gates/rubric.py，priority=90）。

防"过早收工"：当模型在任务进行中（已调用过工具）输出纯文本、没有工具调用，
且任务尚未达成验收标准时，返回 CONTINUE 催它继续动手；用 max_interventions 限流。

⚠️ 关键语义：本门**只在当前轮次已调用过工具时才介入**——即模型做了一半工具活儿
然后过早给文本总结想收工。纯问答（用户问"什么是递归"、模型直接文字回答）是正确行为，
**不应**被强制续跑，否则会导致"答完→停顿→又输出一堆"的卡顿体验。

⚠️ 语义注意：本门会覆盖 Governor 的"无工具调用即完成"默认逻辑，因此**只应在
任务型模式（mission/coding）里装载**，普通 chat 模式不装。

RubricStrategy 抽象"目标是否达成"的判定（可插拔验收）：
- DefaultRubric   —— 永远 SATISFIED（无验收要求）。
- MarkerRubric    —— 最近 assistant 文本命中任一完成标记即 SATISFIED。
- CallableRubric  —— 自定义函数判定。
"""
from __future__ import annotations

from typing import Callable, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .base import StopGate, StopHandlerResult, StopAction


# 闲聊 / 问候的语义判定：对齐 QwenPaw —— 不依赖「你好 / 谢谢」之类的关键词白名单。
# 在 QwenPaw 里，闲聊由 SOUL / agentmode 提示词中的行为约束
# （"问候、闲聊直接回答，不要调用工具"）交给模型自行判断，代码层不做关键词特判；
# 续跑门只在"本轮已调用过工具却过早以纯文本收工"时介入（见 _has_tool_use_in_turn）。
# 纯问答 / 问候因从未调用工具，_has_tool_use_in_turn 自然为 False，门不会打扰，
# 因此无需 _GREETING_RE 这类的脆弱正则。


def _has_tool_use_in_turn(msgs: list) -> bool:
    """检查当前轮次（自最近一条 HumanMessage 起）是否执行过工具。

    如果模型已经用过工具再给出纯文本回答，说明任务进行中可能过早收工，
    rubric 介入才有意义。纯问答（从未调用工具）直接给文本回答是正确行为，
    不应强制续跑——否则会导致"答完→停顿→又被逼着调工具→又输出一堆"的卡顿。
    """
    for m in reversed(msgs):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, ToolMessage):
            return True
    return False


class RubricStrategy:
    """验收策略接口：返回 True 表示目标已达成（可以停）。"""

    def satisfied(self, state: dict) -> bool:  # pragma: no cover - 抽象
        raise NotImplementedError


class DefaultRubric(RubricStrategy):
    def satisfied(self, state: dict) -> bool:
        return True


class MarkerRubric(RubricStrategy):
    def __init__(self, markers: list):
        self.markers = markers or []

    def satisfied(self, state: dict) -> bool:
        msgs = state.get("messages", [])
        for m in reversed(msgs):
            if isinstance(m, AIMessage):
                text = m.content if isinstance(m.content, str) else ""
                return any(mk and mk in text for mk in self.markers)
        return False


class CallableRubric(RubricStrategy):
    def __init__(self, fn: Callable[[dict], bool]):
        self.fn = fn

    def satisfied(self, state: dict) -> bool:
        return bool(self.fn(state))


class StandaloneRubricGate(StopGate):
    name = "rubric"
    priority = 90  # 软策略：只在没有硬停止时才有机会介入

    def __init__(
        self,
        rubric: Optional[RubricStrategy] = None,
        max_interventions: int = 3,
        continuation_message: Optional[str] = None,
    ):
        self.rubric = rubric or DefaultRubric()
        self.max_interventions = max_interventions
        self.continuation_message = continuation_message or (
            "任务似乎尚未真正完成。请不要只做口头总结，"
            "继续调用工具执行下一步，直到目标达成再给出最终结论。"
        )

    def check(self, turn: int, state: dict, gate_state: dict) -> StopHandlerResult | None:
        msgs = state.get("messages", [])
        last = msgs[-1] if msgs else None
        # 仅当模型给出"纯文本、无工具调用"（自认为答完）时才评估
        if not isinstance(last, AIMessage):
            return None
        if getattr(last, "tool_calls", None):
            return None
        # 关键：只在当前轮次已调用过工具时才介入（防任务进行中过早收工）。
        # 纯问答 / 问候（未用工具）因 _has_tool_use_in_turn 为 False 自然不触发，
        # 故不再做"你好 / 谢谢"之类关键词特判（对齐 QwenPaw：闲聊由提示词行为约束处理）。
        # 纯问答（从未用工具）直接给文本回答是正确行为，强制续跑只会造成卡顿。
        if not _has_tool_use_in_turn(msgs):
            return None
        # 目标已达成 -> 不干预，放行完成
        if self.rubric.satisfied(state):
            return None
        interventions = gate_state.get("interventions", 0)
        if interventions >= self.max_interventions:
            # 催促次数用尽，放行避免死循环（交给 IterationGate 兜底）
            return None
        gate_state["interventions"] = interventions + 1
        return StopHandlerResult(
            StopAction.CONTINUE,
            continuation_message=self.continuation_message,
            reason=f"rubric not satisfied, pushing to continue ({interventions + 1}/{self.max_interventions})",
        )
