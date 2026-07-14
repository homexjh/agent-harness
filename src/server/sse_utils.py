"""SSE 序列化工具：把 LangGraph 的状态/消息/Interrupt 转成前端可解析的 JSON。

对应 LangGraph Platform 的 SSE 契约：
- 事件名：metadata / messages / values / updates / error / end
- 每行 `event: <name>` + `data: <json>` + 空行
- 心跳用注释行 `: ping` 保活（前端 SSE 解码器会忽略注释行并视为存活）
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import BaseMessage


def _model_dump_safe(o: Any):
    try:
        if hasattr(o, "model_dump"):
            return o.model_dump()
    except Exception:
        pass
    return str(o)


def jsonable(o: Any) -> Any:
    """递归把任意对象转成 JSON 友好结构。"""
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    if isinstance(o, BaseMessage):
        return _model_dump_safe(o)
    if hasattr(o, "model_dump"):
        try:
            return jsonable(o.model_dump())
        except Exception:
            return str(o)
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [jsonable(v) for v in o]
    return str(o)


def interrupt_to_dict(i: Any) -> dict:
    return {
        "value": jsonable(getattr(i, "value", None)),
        "id": getattr(i, "id", None),
        "resumable": getattr(i, "resumable", None),
        "ns": list(getattr(i, "ns", []) or []),
    }


def jsonable_state(state: dict) -> dict:
    """把图状态（values 事件）转成可序列化 dict，重点处理 messages / __interrupt__。"""
    out: dict = {}
    for k, v in state.items():
        if k == "__interrupt__":
            out[k] = [interrupt_to_dict(x) for x in (v or [])]
        else:
            out[k] = jsonable(v)
    return out


def sse(event: str, data: Any) -> str:
    """构造一条 SSE 消息。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def sse_comment(text: str = "ping") -> str:
    """SSE 心跳注释行（前端视为存活信号）。"""
    return f": {text}\n\n"
