"""Agent 状态定义。

对应 QwenPaw 的运行时状态，但会话隔离不再用手写的 `_sessions` dict，
而是把 gate 的内部状态放进 graph state 的 `gate_state`，由 LangGraph 的
checkpointer 按 `thread_id` 自动持久化（自带 crash recovery）。

QwenPaw 源码映射：
- `gate_state`  ≈ 各 LoopGate 实例上的 `_sessions[session_id]`（react 版手写字典）
- `turn`        ≈ IterationGate 的 `_IterState` 迭代计数
- 会话隔离       ≈ Runtime 里 agent_id/workspace 解析 → 这里直接用 thread_id
"""
from typing import TypedDict, Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    # messages 用 add_messages reducer：按 id 合并、默认 append-only（原始事件日志，永不折叠）
    messages: Annotated[list[BaseMessage], add_messages]
    # 本轮发送给模型的"上下文窗口"（由 ContextManager 折叠后产出，整列表替换）
    prompt_messages: list
    # 当前循环轮次（每经过一次 governor 自增 1）
    turn: int
    # 各 gate 的内部状态：{gate_name: {..."}}，随 thread_id 持久化
    gate_state: dict
    # 上下文管理器的内部状态（折叠序号等）
    context_state: dict
    # 最近一次 governor 决策：continue / stop / ask
    decision: str
    # 停止/中断原因（用于日志、ASK 提示）
    stop_reason: str
    # HITL 恢复后回到的节点（区分"工具前审批"与"工具后裁决"）
    pending_after_hitl: str
    # HITL 人工裁决结果：approved / rejected（None 表示尚未裁决）
    hitl_decision: str
    # 当前会话模式：chat / coding / mission（决定门集合、工具集、提示）
    mode: str
