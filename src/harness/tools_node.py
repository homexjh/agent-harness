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
from .tool_result_store import (
    get_tool_result_store,
    make_placeholder,
)
from .security.guard import set_escalation_thread


def prune_tool_result(content: str, max_chars: int, *, metrics=None, tool: str = "") -> str:
    """ToolResultPruning（QwenPaw: ToolResultPruningMiddleware）：截断超大工具输出，
    防止上下文爆炸。保留头尾各一半 + 中间省略提示（含原始长度），信息可感知不误导。

    注意：此 head/tail 截断**会丢失中间内容**，仅作为显式 ``max_result_chars>0`` 时的
    可选 inline 兜底；主路径已由执行层外置（``externalize``）替代——超阈值结果全文落盘、
    上下文留占位符、``recall`` 可还原，绝不丢原文。
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


def _externalize_or_inline(
    content: str,
    store,
    thread_id: str,
    tool_name: str,
    threshold_chars: int,
) -> str:
    """执行层外置（对齐 QwenPaw ToolResultLimiter / WorkBuddy ToolResultBlobService）：

    - 超阈值：全文写盘，上下文返回占位符（含 token，recall 可还原全文）；
    - 否则：保持**完整 inline**，不做任何截断（与"粗暴截断"的本质区别）。
    """
    if store is None or threshold_chars <= 0:
        return content
    if len(content) <= threshold_chars:
        return content
    token = store.externalize(content, thread_id=thread_id, tool_name=tool_name)
    return make_placeholder(tool_name, token, len(content))


def make_tools_node(
    tools: dict,
    *,
    metrics=None,
    max_result_chars: int = None,
    tool_result_store=None,
    externalize_threshold: int = None,
):
    store = tool_result_store or get_tool_result_store()
    # 默认关闭显式 inline 截断（max_result_chars=0）；执行层外置才是主机制。
    if max_result_chars is None:
        max_result_chars = int(os.getenv("TOOL_RESULT_MAX", "0"))
    if externalize_threshold is None:
        kb = int(os.getenv("AGENT_TOOL_RESULT_THRESHOLD_KB", "50"))
        externalize_threshold = kb * 1024

    def tools_node(state: dict, config=None) -> dict:
        thread_id = "default"
        if config:
            thread_id = (config.get("configurable", {}) or {}).get("thread_id", "default") or "default"
        # 反应式升级：把当前线程 id 写入守卫可见的 per-request 上下文，
        # 使 ModeEscalationGuardian 能定位该请求的 mode/locked 状态并回写升级信号。
        set_escalation_thread(thread_id)
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
            content = str(content)
            # 第 1 层（执行层外置）：超阈值落盘，否则保持完整 inline。
            content = _externalize_or_inline(
                content, store, thread_id, name, externalize_threshold
            )
            # 可选 inline 兜底截断（仅当显式设置 max_result_chars>0）；store 仍留全文可 recall。
            if max_result_chars > 0 and len(content) > max_result_chars:
                content = prune_tool_result(
                    content, max_result_chars, metrics=metrics, tool=name
                )
            outputs.append(ToolMessage(content=content, tool_call_id=tc["id"]))
        return {"messages": outputs}

    return tools_node
