"""tools 节点（等价于 langgraph.prebuilt.ToolNode，自实现以零依赖）。

执行上一步 AIMessage 里的 tool_calls，把结果作为 ToolMessage 追加。
- 工具错误回传给模型（不中断循环）——健壮性要求；
- 记录可观测指标（tool_call / tool_denials）；
- PolicyGuardedTool 的拒绝已在工具内部抛 ToolException，这里捕获成错误结果回传。
"""
from __future__ import annotations

import os

from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException

from .stability.observability import log_event


def prune_tool_result(content: str, max_chars: int, *, metrics=None, tool: str = "") -> str:
    """ToolResultPruning（QwenPaw: ToolResultPruningMiddleware）：截断超大工具输出，
    防止上下文爆炸。保留头尾各一半 + 中间省略提示（含原始长度），信息可感知不误导。
    """
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    head = max_chars // 2
    tail = max_chars - head
    pruned = (
        content[:head]
        + f"\n\n... [工具输出过长，已截断：原始 {len(content)} 字符，仅保留首尾 {max_chars}] ...\n\n"
        + content[-tail:]
    )
    if metrics is not None:
        metrics.prune(tool, len(content), max_chars)
    return pruned


def make_tools_node(tools: dict, *, metrics=None, max_result_chars: int = None):
    if max_result_chars is None:
        max_result_chars = int(os.getenv("TOOL_RESULT_MAX", "8000"))

    def tools_node(state: dict) -> dict:
        last = state["messages"][-1]
        outputs = []
        for tc in getattr(last, "tool_calls", []):
            name = tc["name"]
            tool = tools.get(name)
            if metrics is not None:
                metrics.tool_call(name, allowed=tool is not None)
            if tool is None:
                content = f"Error: unknown tool {name}"
            else:
                try:
                    if hasattr(tool, "invoke"):
                        content = tool.invoke(tc["args"])
                    else:
                        content = tool(**tc["args"])
                except ToolException as e:
                    if metrics is not None:
                        metrics.tool_denials += 1
                    content = f"Blocked: {e}"
                except Exception as e:  # noqa: BLE001 - 工具错误回传模型
                    log_event("tool_error", tool=name, error=str(e))
                    content = f"Error: {e}"
            content = prune_tool_result(
                str(content), max_result_chars, metrics=metrics, tool=name
            )
            outputs.append(ToolMessage(content=content, tool_call_id=tc["id"]))
        return {"messages": outputs}

    return tools_node
