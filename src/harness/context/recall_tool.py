"""recall 工具 —— 让 agent 在上下文被折叠后，按需还原早期全文。"""
from __future__ import annotations

from langchain_core.tools import StructuredTool

from .manager import ContextManager


def make_recall_tool(context_manager: ContextManager) -> StructuredTool:
    def _recall(query: str) -> str:
        return context_manager.recall(query)

    return StructuredTool.from_function(
        func=_recall,
        name="recall",
        description=(
            "Recall full content of earlier (folded) conversation turns by keyword. "
            "Use when you need context that was folded to save tokens, e.g. an earlier "
            "user requirement or a previous tool result no longer in the active window."
        ),
    )
