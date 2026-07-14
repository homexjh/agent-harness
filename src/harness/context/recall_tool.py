"""recall 工具 —— 让 agent 在上下文被折叠后，按需还原早期全文。

多用户隔离：图在编译期按 (model,mode) 缓存为单例，不能把某个具体用户的
ContextManager 绑定进工具。故 ``make_recall_tool()`` 不绑定特定 cm，调用时在
请求上下文内按当前 user（ContextVar）现取该用户的 ContextManager，
保证 recall 还原的是「当前用户自己」的折叠历史，而非共享实例/他人历史。
"""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import StructuredTool

from .manager import ContextManager


def make_recall_tool(context_manager: Optional[ContextManager] = None) -> StructuredTool:
    def _recall(query: str) -> str:
        # 运行时按当前用户解析管理器（多用户隔离）。
        cm = context_manager
        if cm is None:
            from ...server.graph_provider import get_context_manager

            cm = get_context_manager()
        return cm.recall(query)

    return StructuredTool.from_function(
        func=_recall,
        name="recall",
        description=(
            "Recall full content of earlier (folded) conversation turns by keyword. "
            "Use when you need context that was folded to save tokens, e.g. an earlier "
            "user requirement or a previous tool result no longer in the active window."
        ),
    )
