# -*- coding: utf-8 -*-
"""请求级用户上下文（user isolation 的核心载体）。

设计：
- 用 ``contextvars.ContextVar`` 在单次请求/图执行周期内携带当前 ``user_id``。
- 所有需要按用户隔离的存储（会话复合键、context 配置、memory vault）都从本模块读取
  当前 user，从而在不改变全局函数签名的情况下实现多用户隔离。
- 默认 user 为 ``"default"``：当 ``enable_auth=False``（单用户开箱即用）时，所有请求
  都归属 default，行为与改造前完全一致（向后兼容，时延不变）。

注意：LangGraph 的 async 图执行发生在同一 asyncio 任务内，``ContextVar`` 会随任务
自动传播，因此节点 / 工具在运行时都能正确取到当前 user。跨线程调度（cron）不会传播
contextvar，此时回退到 default（cron 属系统级任务，无特定用户）。
"""
from __future__ import annotations

from contextvars import ContextVar

# 当前请求的用户标识。默认 "default"（单用户 / 未开启鉴权时）。
_user_ctx: ContextVar[str] = ContextVar("agent_harness_user", default="default")

# 共享的"系统"用户（cron 等后端调度任务使用，不归属任何终端用户）。
SYSTEM_USER = "system"


def get_user() -> str:
    """返回当前上下文的用户标识。"""
    return _user_ctx.get()


def set_user(user_id: str) -> "object":
    """设置当前上下文的用户标识，返回可用于 ``reset`` 的 token。

    用法::

        token = set_user(uid)
        try:
            ... 调用图 ...
        finally:
            reset_user(token)
    """
    return _user_ctx.set(user_id)


def reset_user(token: "object") -> None:
    """恢复设置前的用户上下文。"""
    _user_ctx.reset(token)
