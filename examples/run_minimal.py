"""Phase 0+1 演示：无需 API key，用确定性 fake 模型展示 Governor 如何阻止失控循环。

运行（需先装依赖）：
    pip install langgraph langchain-core
    python examples/run_minimal.py

演示 1（失控循环被拦）：模型每次都返回同一个工具调用 -> 不打断就死循环。
    Governor 的 DoomLoopGate（滑窗相似度）会先警告、再 STOP；若没触发，
    IterationGate 硬上限也会兜底 STOP。

演示 2（正常收敛）：模型先调工具、再给最终答案 -> Governor 在最终答案处自然 STOP。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from harness.graph import build_graph


def _echo(text: str) -> str:
    return f"echo: {text}"


async def demo_runaway(max_iterations: int = 100):
    """模型永远只调用同一个工具 -> 无 Governor 会死循环，有 Governor 必停。"""
    tool_call = AIMessage(
        content="",
        tool_calls=[{"name": "echo", "args": {"text": "loop"}, "id": "call_1"}],
    )
    model = FakeMessagesListChatModel(responses=[tool_call])

    graph = build_graph(
        model,
        tools={"echo": StructuredTool.from_function(func=_echo, name="echo", description="echo a string")},
        max_iterations=max_iterations,
        gates=None,  # 用默认门：IterationGate + DoomLoopGate
    )

    cfg = {"configurable": {"thread_id": "runaway-demo"}}
    result = await graph.ainvoke({"messages": [HumanMessage(content="go")]}, cfg)

    print("=== 演示1：失控循环被 Governor 拦下 ===")
    print("  决策:", result.get("decision"))
    print("  停止原因:", result.get("stop_reason"))
    print("  总轮次:", result.get("turn"))
    print()


async def demo_normal():
    """模型先调工具、再给最终答案 -> 自然收敛。"""
    tool_call = AIMessage(
        content="",
        tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "call_1"}],
    )
    final = AIMessage(content="任务完成：已 echo 一条消息。")
    model = FakeMessagesListChatModel(responses=[tool_call, final])

    graph = build_graph(
        model,
        tools={"echo": StructuredTool.from_function(func=_echo, name="echo", description="echo a string")},
        max_iterations=100,
    )

    cfg = {"configurable": {"thread_id": "normal-demo"}}
    result = await graph.ainvoke({"messages": [HumanMessage(content="echo hi")]}, cfg)

    print("=== 演示2：正常收敛（工具后给最终答案）===")
    print("  决策:", result.get("decision"))
    print("  停止原因:", result.get("stop_reason"))
    print("  总轮次:", result.get("turn"))
    print()


async def main():
    await demo_runaway()
    await demo_normal()


if __name__ == "__main__":
    asyncio.run(main())
