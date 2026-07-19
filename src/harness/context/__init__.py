"""上下文管理包（fold-not-summarize，零丢失）。

对照 QwenPaw Scroll Context 哲学：
- 原始消息全文写穿到 SQLite，永不摘要丢失；
- 上下文里被"折叠"的只是占位桩，召回即还原全文；
- 预算触发从最旧往新折叠，保留最近窗口，单条最新消息永不被折（保底可见）。
"""
from .store import TurnStore
from .manager import ContextManager, FOLD_STUB_MARKER
from .recall_tool import make_recall_tool
from ..tool_result_store import ToolResultStore, get_tool_result_store

__all__ = [
    "TurnStore",
    "ContextManager",
    "make_recall_tool",
    "FOLD_STUB_MARKER",
    "ToolResultStore",
    "get_tool_result_store",
]
