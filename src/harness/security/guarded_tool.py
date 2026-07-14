"""PolicyGuardedTool —— 把任意工具包一层守卫。"""
from __future__ import annotations

from langchain_core.tools import BaseTool, ToolException

from .guard import ToolGuardEngine


class PolicyGuardedTool(BaseTool):
    """包装一个底层工具：执行前先过 ToolGuardEngine，拒绝则不放行。"""

    name: str = "guarded"
    description: str = "guarded tool"
    underlying: BaseTool
    engine: ToolGuardEngine

    def __init__(self, tool: BaseTool, engine: ToolGuardEngine, **kw):
        super().__init__(underlying=tool, engine=engine, **kw)
        # 保持原名：底层工具已在 tools 字典里被本包装替换，无需加前缀（否则模型按原名调用会找不到）
        self.name = tool.name
        self.description = f"[guarded] {tool.description}"
        # 把底层工具的 schema 透传出来，否则模型只能看到 BaseTool 默认的 *args/**kwargs  schema
        self.args_schema = getattr(tool, "args_schema", None)

    def _run(self, *args, **kwargs):
        if kwargs:
            res = self.engine.evaluate(self.underlying.name, kwargs)
            if not res.allowed:
                raise ToolException(f"PolicyGuardedTool blocked '{self.underlying.name}': {res.reason}")
            return self.underlying.invoke(kwargs)
        res = self.engine.evaluate(self.underlying.name, {"__args__": list(args)})
        if not res.allowed:
            raise ToolException(f"PolicyGuardedTool blocked '{self.underlying.name}': {res.reason}")
        return self.underlying._run(*args, config=None)

    async def _arun(self, *args, **kwargs):
        if kwargs:
            res = self.engine.evaluate(self.underlying.name, kwargs)
            if not res.allowed:
                raise ToolException(f"PolicyGuardedTool blocked '{self.underlying.name}': {res.reason}")
            return await self.underlying.ainvoke(kwargs)
        res = self.engine.evaluate(self.underlying.name, {"__args__": list(args)})
        if not res.allowed:
            raise ToolException(f"PolicyGuardedTool blocked '{self.underlying.name}': {res.reason}")
        return await self.underlying._arun(*args, config=None)

    def invoke(self, input, config=None, **kwargs):
        # tools_node 以 dict 形式传入 tool_calls 的 args；在这里过守卫后委托给底层工具。
        if isinstance(input, dict):
            res = self.engine.evaluate(self.underlying.name, input)
            if not res.allowed:
                raise ToolException(
                    f"PolicyGuardedTool blocked '{self.underlying.name}': {res.reason}"
                )
        return self.underlying.invoke(input, config=config, **kwargs)


def make_guarded_tools(tools: list, engine: ToolGuardEngine) -> dict:
    """返回 {name: PolicyGuardedTool(...) }，键名带 guarded__ 前缀以免与底层冲突。"""
    out = {}
    for t in tools:
        gt = PolicyGuardedTool(tool=t, engine=engine)
        out[gt.name] = gt
    return out
