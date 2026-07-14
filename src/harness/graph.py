"""图装配（路线 A 的核心）：context -> agent -> (approval) -> tools -> governor -> 条件边。

对照 QwenPaw：
- 这是把"ReAct 主循环"从 AgentScope 换成 LangGraph StateGraph 的地方。
- Governor 是真·图节点（不是 hook），跑在"模型调用 + 工具执行"整步的干净边界上，
  等价于 QwenPaw 在 `_reasoning()` 每轮末尾插的 StopHandler，但更干净（无需延迟落槌）。
- 上下文管理：ContextManager 节点在每次 agent 调用前折叠/召回，产出 prompt_messages 窗口。
- 治理：approval 节点在"工具执行前"拦截敏感工具，触发 ASK -> hitl 节点内联 await
  asyncio.Future 真人工裁决（对齐 QwenPaw 的阻塞点原地 Future 超时，而非外部 resume）。
- 会话隔离 = checkpointer 的 thread_id（自带崩溃恢复，比 QwenPaw 手写 _sessions 更稳）。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("agent_harness.graph")

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from .agent import make_call_model
from .context.manager import ContextManager
from .governor import make_governor, route_after_governor
from .gates.doom_loop import DoomLoopGate
from .gates.iteration import IterationGate
from .state import AgentState
from .tools_node import make_tools_node

# 把 auto_memory 放到独立后台线程执行：它会调用 LLM 提取事实并重建索引，
# 同步执行会阻塞 context_node 数十秒（响应冻结）。# QwenPaw 的 auto_memory 是响应路径外的后台维护任务，这里用单线程 executor 对齐。
_MEMORY_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctx-memory")


def _log_memory_exception(fut):
    try:
        fut.result()
    except Exception as e:  # noqa: BLE001
        print(f"[memory] context hook failed: {e}")
from .security.approval import ApprovalGate, SENSITIVE_TOOLS
from .stability.observability import Metrics


# ---------------------------------------------------------------------------
# 节点工厂
# ---------------------------------------------------------------------------
def _last_human_text(messages: list) -> str:
    """取最近一条用户消息的正文（截断），用于自动记忆检索。"""
    for m in reversed(messages):
        role = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
        if role not in ("human", "user"):
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, dict):
            content = content.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        return (content or "")[:200]
    return ""


def make_context_node(context_manager: ContextManager, system_hint: str = None, memory_manager=None, core_files_manager=None):
    def context_node(state: dict, config: RunnableConfig) -> dict:
        _t = time.perf_counter()
        conf = (config or {}).get("configurable", {})
        thread_id = conf.get("thread_id", "default")
        user_id = conf.get("user_id", "default")
        raw = state.get("messages", [])
        # 多用户隔离：运行时按当前 user 解析管理器。图在编译期按 (model,mode) 缓存，
        # 锁定的全局单例不能跨用户共享记忆/上下文，故此处按 config 里的 user_id 现取。
        # user_id 由 app 层在请求入口注入到 config["configurable"]。
        # 注意：本项目以 src 为包根（start.sh 的 PYTHONPATH=项目根，uvicorn 跑 src.server.app），
        # 故必须用相对导入 ..server，绝对 import server 在运行时解析不到（No module named 'server'）。
        from ..server.graph_provider import get_context_manager, get_memory_manager

        cm = get_context_manager(user_id)
        mm = get_memory_manager(user_id)

        # Build final system hint: core files (layer-1) first, then mode-specific hint.
        final_hint_parts = []
        _t0 = time.perf_counter()
        if core_files_manager is not None:
            try:
                core_prompt = core_files_manager.build_system_prompt()
                if core_prompt:
                    final_hint_parts.append(core_prompt)
            except Exception as e:  # noqa: BLE001
                print(f"[core-files] build_system_prompt failed: {e}")
        if system_hint:
            final_hint_parts.append(system_hint)
        final_hint = "\n\n".join(final_hint_parts) if final_hint_parts else None
        logger.info("CTX_CORE thread=%s dt=%.3fs", thread_id, time.perf_counter() - _t0)

        _t1 = time.perf_counter()
        window, cstate = cm.prepare(raw, thread_id, system_hint=final_hint)
        logger.info("CTX_PREPARE thread=%s dt=%.3fs msgs=%d", thread_id, time.perf_counter() - _t1, len(raw))

        # 长期记忆（对齐 QwenPaw 的 ReMeLight）：
        # - auto_memory：按 auto_memory_interval 把对话事实写入 vault；
        # - auto_memory_search：回复前用最新用户消息做混合检索，命中则注入系统提示。
        if mm is not None:
            try:
                _t2 = time.perf_counter()
                fut = _MEMORY_EXECUTOR.submit(mm.auto_memory, raw, thread_id=thread_id)
                fut.add_done_callback(_log_memory_exception)
                logger.info("CTX_AUTOMEM thread=%s dt=%.3fs", thread_id, time.perf_counter() - _t2)
                ams = mm.cfg.reme_light_memory_config.auto_memory_search_config
                if ams.enabled:
                    _t3 = time.perf_counter()
                    last_user = _last_human_text(raw)
                    if last_user:
                        hit = mm.memory_search(last_user, ams.max_results)
                        if not hit.startswith(f"memory_search('{last_user}'): no"):
                            window = [
                                SystemMessage(
                                    content=(
                                        "## Retrieved long-term memory\n"
                                        + hit
                                        + "\n(Use this to inform your response when relevant.)"
                                    )
                                )
                            ] + window
                    logger.info("CTX_MEMSEARCH thread=%s dt=%.3fs", thread_id, time.perf_counter() - _t3)
            except Exception as e:  # noqa: BLE001
                print(f"[memory] context hook failed: {e}")

        # 把折叠后的窗口放到 prompt_messages，不要覆盖 messages。
        # 这样 agent 节点生成的 AIMessage 会被 add_messages reducer 追加到 messages，
        # 前端 values 事件才能看到完整对话历史。
        logger.info("CTX_NODE_DONE thread=%s dt=%.2fs msgs=%d", thread_id, time.perf_counter() - _t, len(raw))
        return {"prompt_messages": window, "context_state": cstate}

    return context_node


def make_agent_node(agent_model):
    """把 ChatModel 包装成普通图节点：内部走流式以被 stream_mode='messages' 捕获，
    最终把合并后的 AIMessage 返回给 add_messages reducer。
    """
    async def agent_node(state: dict, config: RunnableConfig) -> dict:
        logger.info("AGENT_NODE_ENTER thread=%s", (config or {}).get("configurable", {}).get("thread_id", "default"))
        prompt_messages = state.get("prompt_messages") or state.get("messages", [])
        chunks = []
        async for chunk in agent_model.astream(prompt_messages):
            chunks.append(chunk)
        if not chunks:
            return {"messages": []}
        final_chunk = chunks[0]
        for c in chunks[1:]:
            final_chunk += c
        # 转成 AIMessage，让前端 SDK 在 values 事件里能识别（type='ai'）
        final = AIMessage(
            content=final_chunk.content,
            additional_kwargs=final_chunk.additional_kwargs,
            response_metadata=final_chunk.response_metadata,
            id=final_chunk.id,
            tool_calls=getattr(final_chunk, "tool_calls", []) or [],
        )
        return {"messages": [final]}

    return agent_node


def make_approval_node(approval_gate: ApprovalGate):
    def approval_node(state: dict) -> dict:
        res = approval_gate.check(state)
        if res is not None and res.action.value == "ask":
            return {
                "decision": "ask",
                "stop_reason": res.reason,
                "pending_after_hitl": "tools",  # 工具前审批：恢复后去执行工具
            }
        return {"decision": "continue"}

    return approval_node


def _is_approved(decision) -> bool:
    """把裁决值归一化为"是否批准"。

    前端发送 "approved" / "rejected" / "timeout"。为安全起见：显式拒绝词 -> False；
    显式批准词或 True -> True；None 默认按批准处理（兼容旧客户端）。
    其它未知值按拒绝处理，避免拼写错误/脏数据导致越权放行。
    """
    if decision is None:
        return True
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, dict):
        decision = decision.get("decision") or decision.get("action") or ""
    s = str(decision).strip().lower()
    if s in ("reject", "rejected", "deny", "denied", "no", "n", "false", "cancel", "abort", "timeout"):
        return False
    if s in ("approve", "approved", "yes", "y", "true", "ok", "accept", "accepted", "allow", "continue"):
        return True
    # 未知值：保守拒绝（安全兜底）
    return False


def make_hitl_node():
    """人工审批节点（QwenPaw 的 GovernancePolicy.ASK）。

    对齐 QwenPaw 的「阻塞点原地 Future 超时」：节点在"工具执行前"**原地 await 一个
    asyncio.Future**，前端批准/拒绝经 hub.resolve(...) 设 future 结果，超时（running.
    approval_timeout_seconds）自动以 "rejected" 决议返回——**同一执行流**继续，不似旧实现
    那样从外部另起 graph.ainvoke(Command(resume=...)) 恢复（会重复消耗 token、可能触发新工具）。

    - 批准(approved) -> decision=continue，沿 pending_after_hitl 回目标节点继续；
    - 拒绝(rejected/timeout) -> decision=rejected：
        · 工具前审批(pending=tools)：为每个被拒的 tool_call 合成一条"已拒绝"ToolMessage
          （补全悬空的 tool_calls，否则真实模型下轮会因缺工具结果而报错），路由回 context，
          让模型基于"用户拒绝了该操作"重新决策，工具**不会执行**。
        · 工具后裁决(pending=agent)：直接停止本次运行。
    """
    async def hitl_node(state: dict, config: RunnableConfig) -> dict:
        thread_id = (config or {}).get("configurable", {}).get("thread_id", "default")
        reason = state.get("stop_reason", "approval required")
        pending_target = state.get("pending_after_hitl", "context")

        # 内联审批：创建 pending -> 推送事件给前端 -> 原地 await future
        try:
            from ..server.approval_hub import get_hub, emit_approval

            hub = get_hub()
            timeout = hub.default_timeout()
            pa = hub.request(thread_id, {"question": reason, "pending": pending_target}, timeout)
            # 在 await 之前把审批事件发给前端（流式循环并发抽取 out_q 时 yield）
            await emit_approval(thread_id, pa.request_id, pa.question)
            decision = await hub.wait(pa)
        except Exception as exc:  # noqa: BLE001
            logger.warning("approval hub unavailable, auto-approving thread=%s: %s", thread_id, exc)
            decision = "approved"

        approved = _is_approved(decision)

        if approved:
            return {"decision": "continue", "hitl_decision": "approved"}

        # 被拒绝
        updates: dict = {"decision": "rejected", "hitl_decision": "rejected"}
        if pending_target == "tools":
            last = state["messages"][-1] if state.get("messages") else None
            rej_msgs = []
            for tc in (getattr(last, "tool_calls", None) or []):
                rej_msgs.append(
                    ToolMessage(
                        content=(
                            f"Rejected by user: tool '{tc.get('name')}' was NOT executed "
                            f"(human declined the approval request). "
                            f"Do not retry the same action; acknowledge and adjust."
                        ),
                        tool_call_id=tc.get("id"),
                    )
                )
            if rej_msgs:
                updates["messages"] = rej_msgs
        return updates

    return hitl_node


def route_after_approval(state: dict) -> str:
    return "hitl" if state.get("decision") == "ask" else "tools"


def route_after_hitl(state: dict) -> str:
    # 拒绝：工具前审批已注入"已拒绝"ToolMessage，回 context 让模型重新决策；
    #       工具后裁决则直接结束本次运行。
    if state.get("hitl_decision") == "rejected":
        pending = state.get("pending_after_hitl", "context")
        return "context" if pending == "tools" else END
    # 批准：沿 pending 回目标节点（tools=去执行工具；agent/context=重新评估）
    pending = state.get("pending_after_hitl", "context")
    return "tools" if pending == "tools" else "context"


def should_continue(state: dict) -> str:
    """有未决 tool_calls -> 去 approval（再决定是否执行）；否则先过 Governor。"""
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "approval"
    return "governor"


# ---------------------------------------------------------------------------
# 图装配
# ---------------------------------------------------------------------------
def build_graph(
    model,
    tools: dict,
    *,
    checkpointer=None,
    max_iterations: int = 30,
    gates=None,
    context_manager: ContextManager = None,
    approval_gate: ApprovalGate = None,
    metrics: Metrics = None,
    system_hint: str = None,
    breaker=None,
    memory_manager=None,
    core_files_manager=None,
):
    gates = gates or [IterationGate(max_iterations=max_iterations), DoomLoopGate()]
    context_manager = context_manager or ContextManager()
    approval_gate = approval_gate or ApprovalGate()

    governor = make_governor(gates, metrics=metrics)
    agent_model = make_call_model(model, tools=list(tools.values()), metrics=metrics, breaker=breaker)
    tools_node = make_tools_node(tools, metrics=metrics)
    context_node = make_context_node(context_manager, system_hint=system_hint, memory_manager=memory_manager, core_files_manager=core_files_manager)
    approval_node = make_approval_node(approval_gate)
    hitl_node = make_hitl_node()
    agent_node = make_agent_node(agent_model)

    g = StateGraph(AgentState)
    g.add_node("context", context_node)
    g.add_node("agent", agent_node)
    g.add_node("approval", approval_node)
    g.add_node("tools", tools_node)
    g.add_node("governor", governor)
    g.add_node("hitl", hitl_node)

    g.set_entry_point("context")
    g.add_edge("context", "agent")
    g.add_conditional_edges(
        "agent", should_continue, {"approval": "approval", "governor": "governor"}
    )
    g.add_conditional_edges(
        "approval", route_after_approval, {"tools": "tools", "hitl": "hitl"}
    )
    g.add_edge("tools", "governor")
    g.add_conditional_edges(
        "governor",
        route_after_governor,
        {"context": "context", "hitl": "hitl", END: END},
    )
    g.add_conditional_edges(
        "hitl", route_after_hitl, {"tools": "tools", "context": "context", END: END}
    )

    return g.compile(checkpointer=checkpointer or MemorySaver())
