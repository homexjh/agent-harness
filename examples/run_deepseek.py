"""真实 DeepSeek 运行（需要 API key）。

设置环境变量后运行：
    export DEEPSEEK_API_KEY=sk-xxxx
    export DEEPSEEK_BASE_URL=https://api.deepseek.com/v1   # 可选
    python examples/run_deepseek.py

模型默认 deepseek-v4-pro。无 key 时本脚本会直接报错提示，可用 run_stable.py 做确定性验证。
"""
from __future__ import annotations

import asyncio
import os
from langchain_core.messages import HumanMessage, SystemMessage

from src.harness.models import make_deepseek_model
from src.harness.context import ContextManager, make_recall_tool
from src.harness.security.guard import ToolGuardEngine
from src.harness.security.guarded_tool import make_guarded_tools
from src.harness.security.approval import ApprovalGate
from src.harness.stability.observability import Metrics
from src.harness.gates.iteration import IterationGate
from src.harness.gates.doom_loop import DoomLoopGate
from src.harness.graph import build_graph


def _calculator(a: float, b: float) -> str:
    return f"result={a + b}"


async def main():
    model = make_deepseek_model(os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"))
    metrics = Metrics()
    cm = ContextManager(budget_tokens=12000)
    from langchain_core.tools import StructuredTool

    calc = StructuredTool.from_function(
        func=_calculator, name="calculator", description="Add two numbers."
    )
    engine = ToolGuardEngine()
    tools = make_guarded_tools([calc], engine)
    tools["recall"] = make_recall_tool(cm)
    graph = build_graph(
        model,
        tools,
        gates=[IterationGate(30), DoomLoopGate()],
        approval_gate=ApprovalGate(),
        metrics=metrics,
    )
    cfg = {"configurable": {"thread_id": "deepseek-demo"}}
    out = await graph.ainvoke(
        {
            "messages": [
                SystemMessage(content="你是一个严谨的助手，必要时使用工具。"),
                HumanMessage(content="帮我算一下 1234 + 5678，并简要说明过程。"),
            ]
        },
        cfg,
    )
    print("决策:", out["decision"], "| 原因:", out["stop_reason"])
    print("最终消息:", out["messages"][-1].content)
    print("指标:", metrics.snapshot())


if __name__ == "__main__":
    asyncio.run(main())
