"""稳定版全链路确定性验证（无需 API key）。

用 ScriptedModel 模拟真实多轮行为，逐场景验证 harness 的全部稳定性能力：
  A. 失控循环 -> DoomLoopGate 拦截停止
  B. 长上下文 -> ContextManager 折叠 + recall 召回还原
  C. 敏感工具 -> ApprovalGate -> interrupt() 真人工裁决 -> resume 后执行
  D. 任务完成 -> MissionGate 按 prd 标记停止

注：agent 节点为 async（支持 token 流式），故统一用 async API（ainvoke/astream）。

运行：python examples/run_stable.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool

from src.harness.models import ScriptedModel
from src.harness.context import ContextManager, make_recall_tool
from src.harness.security.guard import ToolGuardEngine
from src.harness.security.guarded_tool import make_guarded_tools
from src.harness.security.approval import ApprovalGate
from src.harness.stability.observability import Metrics
from src.harness.stability.modes import MissionGate
from src.harness.gates.iteration import IterationGate
from src.harness.gates.doom_loop import DoomLoopGate
from src.harness.graph import build_graph

from langgraph.errors import GraphInterrupt
from langgraph.types import Command


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _calculator(a: float, b: float) -> str:
    return f"result={a + b}"


def _make_tools(context_manager: ContextManager, workdir: str):
    calc = StructuredTool.from_function(
        func=_calculator, name="calculator", description="Add two numbers."
    )

    def _write_file(path: str, content: str) -> str:
        p = os.path.join(workdir, os.path.basename(path))
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"written {p}"

    write_file = StructuredTool.from_function(
        func=_write_file, name="write_file", description="Write content to a file."
    )

    engine = ToolGuardEngine()  # 默认三守卫（FilePath/RuleBased/ShellEvasion）
    guarded = make_guarded_tools([calc, write_file], engine)
    guarded["recall"] = make_recall_tool(context_manager)
    return guarded


# ---------------------------------------------------------------------------
# 场景 A：失控循环
# ---------------------------------------------------------------------------
async def scenario_runaway(metrics: Metrics):
    print("\n=== 场景 A：失控循环被 Governor 拦下（DoomLoopGate）===")
    cid = [0]

    def loop_call(messages):
        cid[0] += 1
        return AIMessage(
            content="",
            tool_calls=[{"name": "calculator", "args": {"a": 1, "b": 1}, "id": f"c{cid[0]}"}],
        )

    model = ScriptedModel([loop_call] * 20)
    cm = ContextManager(budget_tokens=20000)
    tools = _make_tools(cm, tempfile.gettempdir())
    graph = build_graph(
        model, tools, gates=[IterationGate(30), DoomLoopGate()], metrics=metrics
    )
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content="loop forever")]},
        {"configurable": {"thread_id": "A"}},
    )
    print(f"  决策: {out['decision']}  停止原因: {out['stop_reason']}  轮次: {out['turn']}")


# ---------------------------------------------------------------------------
# 场景 B：长上下文折叠 + recall 召回
# ---------------------------------------------------------------------------
def scenario_context(metrics: Metrics):
    print("\n=== 场景 B：长上下文折叠 + recall 召回（零丢失）===")
    cm = ContextManager(budget_tokens=1500, allow_unsandboxed_recall=True)  # 极小预算，强制折叠
    # 造 6 条各 ~2000 字符的长消息（单条约 500 token，整体远超预算）
    long_msgs = []
    for i in range(6):
        text = f"用户需求第{i}条：" + ("这是一段很长的背景信息，" * 400)
        long_msgs.append(HumanMessage(content=text))
    raw = [HumanMessage(content="开始任务")] + long_msgs
    window, cstate = cm.prepare(raw, "B-thread")
    folded = cstate.get("fold_count", 0)
    has_stub = any(
        isinstance(m, SystemMessage) and "[CONTEXT FOLD]" in (m.content or "")
        for m in window
    )
    print(f"  原始消息数: {len(raw)}  折叠条数: {folded}  窗口含折叠桩: {has_stub}")
    print(f"  窗口大小: {len(window)}（小于原始 {len(raw)}，省 token）")
    # 召回：用第一条需求关键词
    restored = cm.recall("用户需求第0条")
    ok = ("用户需求第0条" in restored) and ("开始任务" not in restored)
    print(f"  recall('用户需求第0条') -> 零丢失还原成功: {ok}")
    metrics.fold(folded, 0)


# ---------------------------------------------------------------------------
# 场景 C：敏感工具 -> 审批 -> interrupt -> resume
# ---------------------------------------------------------------------------
async def scenario_approval(metrics: Metrics, workdir: str):
    print("\n=== 场景 C：敏感工具触发人工审批（interrupt + resume）===")
    cid = [0]

    def write_call(messages):
        cid[0] += 1
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "write_file", "args": {"path": "out.txt", "content": "hello"}, "id": f"w{cid[0]}"}
            ],
        )

    model = ScriptedModel(
        [write_call, AIMessage(content="文件已写好，任务完成")]
    )
    cm = ContextManager(budget_tokens=20000)
    tools = _make_tools(cm, workdir)
    graph = build_graph(
        model,
        tools,
        gates=[IterationGate(30), DoomLoopGate()],
        approval_gate=ApprovalGate(),
        metrics=metrics,
    )
    cfg = {"configurable": {"thread_id": "C"}}
    out = None
    try:
        out = await graph.ainvoke({"messages": [HumanMessage(content="写个文件")]}, cfg)
    except GraphInterrupt:
        print("  -> 触发 interrupt()（已暂停，等待人工审批）")
        out = await graph.ainvoke(Command(resume="approved"), cfg)
    if out is not None and out.get("__interrupt__"):
        print("  -> 触发 interrupt()（已暂停，等待人工审批）")
        out = await graph.ainvoke(Command(resume="approved"), cfg)
    target = os.path.join(workdir, "out.txt")
    wrote = os.path.exists(target)
    print(f"  最终决策: {out['decision']}  停止原因: {out['stop_reason']}")
    print(f"  文件已落盘: {wrote}")


# ---------------------------------------------------------------------------
# 场景 D：任务完成（MissionGate）
# ---------------------------------------------------------------------------
async def scenario_mission(metrics: Metrics):
    print("\n=== 场景 D：任务完成标记触发 MissionGate 停止 ===")
    # 模型会被调用两次：第 1 次返回工具调用，第 2 次返回完成任务（命中标记）
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "calculator", "args": {"a": 2, "b": 3}, "id": "m1"}],
            ),
            AIMessage(content="TASK COMPLETE: 全部需求已实现。"),
        ]
    )
    cm = ContextManager(budget_tokens=20000)
    tools = _make_tools(cm, tempfile.gettempdir())
    gates = [IterationGate(30), DoomLoopGate(), MissionGate(completion_markers=["TASK COMPLETE"])]
    graph = build_graph(model, tools, gates=gates, metrics=metrics)
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content="实现需求")]},
        {"configurable": {"thread_id": "D"}},
    )
    print(f"  决策: {out['decision']}  停止原因: {out['stop_reason']}  轮次: {out['turn']}")


async def main():
    print("############################################")
    print("# agent-harness 稳定版全链路验证（确定性） #")
    print("############################################")
    metrics = Metrics()
    workdir = tempfile.mkdtemp(prefix="harness_demo_")
    await scenario_runaway(metrics)
    scenario_context(metrics)
    await scenario_approval(metrics, workdir)
    await scenario_mission(metrics)
    print("\n================ 可观测快照 ================")
    import json
    print(json.dumps(metrics.snapshot(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
