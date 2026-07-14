"""BudgetGate（QwenPaw: loop/gates/budget.py 的 BudgetGate，priority=20）。

Token 预算硬闸门——排在 IterationGate 之后、任务/软策略之前，确保"烧钱"永远兜得住。
- 估算累计发送/产出 token（对 state.messages 全量做启发式计数）；
- 超过 max_tokens -> STOP（预算耗尽）。

零依赖纯函数，可脱离框架单测。
"""
from __future__ import annotations

import json

from .base import StopGate, StopHandlerResult, StopAction


def _estimate_tokens(messages: list) -> int:
    """字符数/4 的稳妥启发式（中英混合）。含 tool_calls 的 JSON 也计入。"""
    total = 0
    for m in messages:
        content = getattr(m, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        total += max(1, len(str(content)) // 4)
        tcs = getattr(m, "tool_calls", None) or []
        if tcs:
            total += len(json.dumps(tcs, ensure_ascii=False, default=str)) // 4 + 20
    return total


class BudgetGate(StopGate):
    name = "budget"
    priority = 20  # 仅次于 IterationGate 的硬安全阀

    def __init__(self, max_tokens: int = 300_000, warn_ratio: float = 0.8):
        self.max_tokens = max_tokens
        self.warn_ratio = warn_ratio

    def check(self, turn: int, state: dict, gate_state: dict) -> StopHandlerResult | None:
        used = _estimate_tokens(state.get("messages", []))
        gate_state["tokens_used"] = used
        if used >= self.max_tokens:
            return StopHandlerResult(
                StopAction.STOP,
                reason=f"token budget exhausted ({used}/{self.max_tokens})",
            )
        # 软警告：接近预算时提醒模型收敛（仅一次）
        if used >= self.max_tokens * self.warn_ratio and not gate_state.get("warned"):
            gate_state["warned"] = True
            return StopHandlerResult(
                StopAction.CONTINUE,
                continuation_message=(
                    f"NOTE: 已使用约 {used} tokens，接近预算上限 {self.max_tokens}。"
                    "请尽快收敛，优先产出最终结论，避免不必要的额外步骤。"
                ),
                reason=f"budget warning ({used}/{self.max_tokens})",
            )
        return None
