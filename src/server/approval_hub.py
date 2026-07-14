# -*- coding: utf-8 -*-
"""审批中枢：对齐 QwenPaw 的 ``PendingApproval`` + ``asyncio.Future`` 原地超时。

设计要点（对照 QwenPaw ``app/approvals/service.py`` 的 ``wait_for_approval``）：

- 审批发生在「工具执行前」的阻塞点（harness 的 hitl 节点）。节点 ``await hub.wait(pa)``，
  挂起的不是 LangGraph 的 ``interrupt()``（那需要从外部另起 ``graph.ainvoke(Command(...))`` 恢复，
  会重复消耗 token、可能触发新工具调用），而是一个**进程内 ``asyncio.Future``**。
- 超时（``running.approval_timeout_seconds``，默认对齐 QwenPaw 300s）自动以 ``"rejected"``
  决议返回，**同一执行流**继续——与 QwenPaw「阻塞点原地 Future 超时」语义一致。
- 前端批准/拒绝 → ``hub.resolve(request_id, decision)`` → ``future.set_result``，原图继续执行。
- 多 worker 共享：pending 决议持久化到共享 SQLite（``~/.workbuddy/approvals.sqlite``）。
  任一 worker 的后台 poller 都会扫描共享表：把跨 worker 的决议/超时落到本 worker 持有的
  future 上，从而多 worker 部署下审批也能正确解除（旧实现 ``_pending`` 是模块级 dict，
  完全不跨 worker）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 进程内 pending（挂在持有 future 的 worker 上）
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()
_PENDING: dict[str, "PendingApproval"] = {}

# 事件发射器：流式循环在启动图前按 thread_id 注册，hitl 节点内联推送 approval 事件给前端
_EMITTERS: dict[str, Callable[[str, dict], Awaitable[None]]] = {}
_EMIT_LOCK = threading.Lock()


def set_emitter(thread_id: str, fn: Callable[[str, dict], Awaitable[None]]) -> None:
    with _EMIT_LOCK:
        _EMITTERS[thread_id] = fn


def clear_emitter(thread_id: str) -> None:
    with _EMIT_LOCK:
        _EMITTERS.pop(thread_id, None)


async def emit_approval(thread_id: str, request_id: str, question: Any) -> None:
    """hitl 节点内联推送审批事件给前端（在 await future 之前）。"""
    fn = None
    with _EMIT_LOCK:
        fn = _EMITTERS.get(thread_id)
    if fn is None:
        logger.warning("approval emitter missing for thread=%s (event dropped)", thread_id)
        return
    try:
        await fn(request_id, question)
    except Exception:  # noqa: BLE001
        logger.exception("approval emit failed thread=%s", thread_id)


class PendingApproval:
    def __init__(self, request_id: str, thread_id: str, question: Any, timeout: float):
        self.request_id = request_id
        self.thread_id = thread_id
        self.question = question
        self.timeout = timeout
        self.created_at = time.time()
        self.future: asyncio.Future = asyncio.Future()
        self._resolved = False


# ---------------------------------------------------------------------------
# 共享 SQLite 存储（多 worker 安全网）
# ---------------------------------------------------------------------------
def _db_path() -> Path:
    p = Path.home() / ".workbuddy" / "approvals.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_db_path()), timeout=5)
    c.execute(
        "CREATE TABLE IF NOT EXISTS approval_pending ("
        "request_id TEXT PRIMARY KEY, thread_id TEXT, question TEXT, "
        "status TEXT, decision TEXT, timeout REAL, created_at REAL, updated_at REAL)"
    )
    return c


def _store_upsert(pa: "PendingApproval") -> None:
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO approval_pending "
                "(request_id, thread_id, question, status, decision, timeout, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    pa.request_id, pa.thread_id, json.dumps(pa.question, ensure_ascii=False),
                    "pending", None, pa.timeout, pa.created_at, time.time(),
                ),
            )
    except Exception:  # noqa: BLE001
        logger.exception("approval store upsert failed request=%s", pa.request_id)


def _store_resolve(request_id: str, decision: str) -> None:
    try:
        with _conn() as c:
            c.execute(
                "UPDATE approval_pending SET status='resolved', decision=?, updated_at=? "
                "WHERE request_id=?",
                (decision, time.time(), request_id),
            )
    except Exception:  # noqa: BLE001
        logger.exception("approval store resolve failed request=%s", request_id)


def _store_fetch(thread_id: Optional[str] = None) -> list[dict]:
    try:
        with _conn() as c:
            if thread_id:
                rows = c.execute(
                    "SELECT request_id, thread_id, status, decision, timeout, created_at "
                    "FROM approval_pending WHERE thread_id=?", (thread_id,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT request_id, thread_id, status, decision, timeout, created_at "
                    "FROM approval_pending"
                ).fetchall()
        return [
            {"request_id": r[0], "thread_id": r[1], "status": r[2], "decision": r[3],
             "timeout": r[4], "created_at": r[5]}
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        logger.exception("approval store fetch failed")
        return []


def _store_delete(request_id: str) -> None:
    try:
        with _conn() as c:
            c.execute("DELETE FROM approval_pending WHERE request_id=?", (request_id,))
    except Exception:  # noqa: BLE001
        logger.exception("approval store delete failed request=%s", request_id)


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
class ApprovalHub:
    def __init__(self) -> None:
        self._poller: Optional[asyncio.Task] = None

    # -- 创建待决审批（hitl 节点调用） --
    def request(self, thread_id: str, question: Any, timeout: float) -> PendingApproval:
        request_id = f"{thread_id}:{uuid.uuid4().hex[:12]}"
        pa = PendingApproval(request_id, thread_id, question, max(10.0, float(timeout)))
        with _LOCK:
            _PENDING[request_id] = pa
        # 持久化到共享表（多 worker 安全网）；失败不阻断主流程
        _store_upsert(pa)
        return pa

    # -- 原地等待决议（hitl 节点 await） --
    async def wait(self, pa: "PendingApproval") -> str:
        try:
            decision = await asyncio.wait_for(pa.future, timeout=pa.timeout)
        except asyncio.TimeoutError:
            # 超时按 rejected 决议返回，同一执行流继续（QwenPaw 语义）
            decision = "rejected"
            self._mark_resolved(pa.request_id, "rejected")
        # CancelledError 不捕获：让它自然上抛，由外层 graph 取消流处理。
        # 若在这里 catch 并返回，会导致 graph_task.cancel() 失效、图继续执行。
        return decision

    # -- 前端/看门狗 决议（可在任意 worker 调用） --
    def resolve(self, request_id: str, decision: str) -> bool:
        # 1) 更新共享表（跨 worker 可见）
        _store_resolve(request_id, decision)
        # 2) 若本 worker 持有该 future，立即 set
        self._set_local_future(request_id, decision)
        return True

    def _set_local_future(self, request_id: str, decision: str) -> None:
        pa = _PENDING.get(request_id)
        if pa is None:
            return
        if pa._resolved:
            return
        pa._resolved = True
        if not pa.future.done():
            try:
                pa.future.set_result(decision)
            except Exception:  # noqa: BLE001
                pass
        with _LOCK:
            _PENDING.pop(request_id, None)

    def _mark_resolved(self, request_id: str, decision: str) -> None:
        _store_resolve(request_id, decision)
        self._set_local_future(request_id, decision)

    def pending_for_thread(self, thread_id: str) -> Optional[str]:
        """返回该 thread 当前未决审批的 request_id（用于「用户发新消息时自动否决旧审批」）。"""
        with _LOCK:
            for rid, pa in _PENDING.items():
                if pa.thread_id == thread_id and not pa._resolved:
                    return rid
        return None

    def _store_fetch(self, thread_id: Optional[str] = None) -> list[dict]:
        """暴露给 app.py 的模块级 _store_fetch 包装（避免跨模块访问私有函数）。"""
        return _store_fetch(thread_id)

    def has_pending(self, thread_id: str) -> bool:
        return self.pending_for_thread(thread_id) is not None

    def clear_thread(self, thread_id: str) -> None:
        with _LOCK:
            to_pop = [rid for rid, pa in _PENDING.items() if pa.thread_id == thread_id]
            for rid in to_pop:
                pa = _PENDING.pop(rid)
                pa._resolved = True
                if not pa.future.done():
                    try:
                        pa.future.set_result("rejected")
                    except Exception:  # noqa: BLE001
                        pass

    # -- 后台 poller：跨 worker 决议 + 超时清理 --
    def start_poller(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            logger.warning("approval hub: no running loop, poller not started")
            return
        if self._poller is None or self._poller.done():
            self._poller = loop.create_task(self._poll_loop(), name="approval-hub-poller")

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("approval hub poll error")

    def _poll_once(self) -> None:
        # 1) 本 worker 持有的 pending：若共享表已决议（跨 worker 批准/拒绝），立即 set
        with _LOCK:
            local_ids = list(_PENDING.keys())
        if local_ids:
            rows = {r["request_id"]: r for r in _store_fetch()}
            for rid in local_ids:
                r = rows.get(rid)
                if r and r["status"] == "resolved":
                    self._set_local_future(rid, r["decision"] or "rejected")
                    _store_delete(rid)
        # 2) 共享表中超龄的 pending（任意 worker 都可清理）→ 决议为 rejected
        try:
            timeout_global = self._timeout_seconds()
        except Exception:  # noqa: BLE001
            timeout_global = 300.0
        now = time.time()
        for r in _store_fetch():
            if r["status"] != "pending":
                continue
            to = r["timeout"] or timeout_global
            if now - r["created_at"] > to:
                _store_resolve(r["request_id"], "rejected")
                # 若本 worker 恰好持有该 future，也直接 set
                self._set_local_future(r["request_id"], "rejected")

    def default_timeout(self) -> float:
        """读取 running.approval_timeout_seconds（默认对齐 QwenPaw 300s）。"""
        from .config import get_config

        try:
            cfg = get_config()
            running = cfg.running if isinstance(cfg.running, dict) else {}
            return max(10.0, float(running.get("approval_timeout_seconds", 300)))
        except Exception:  # noqa: BLE001
            return 300.0


_hub: Optional["ApprovalHub"] = None
_hub_lock = threading.Lock()


def get_hub() -> "ApprovalHub":
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = ApprovalHub()
    return _hub
