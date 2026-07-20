# -*- coding: utf-8 -*-
"""Inbox 事件存储：定时任务 / 后台任务的结果投递到收件箱，由前端轮询渲染进会话窗口。

对齐 QwenPaw 的 app/inbox_store.py：cron 任务成功后默认 append_event
（source_type="cron", event_type="cron_result"），前端 GET /inbox/events
拉取并渲染进会话窗口——这正是 QwenPaw 的「定时任务结果默认弹出在会话」行为。

采用同步实现：scheduler 在 APScheduler 后台线程中调用，文件 IO 用 threading.Lock
串行化，避免多任务并发写坏 inbox_events.json。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Optional

from .config import DATA_HOME

_PATH = DATA_HOME / "inbox_events.json"
_LOCK = threading.Lock()
_MAX_EVENTS = 5000


def _load_events() -> list[dict[str, Any]]:
    if not _PATH.exists():
        return []
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 文件损坏/不可读时当作空，下一次 append 会用原子写覆盖为合法内容
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _save_events(events: list[dict[str, Any]]) -> None:
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(events, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(_PATH)


def append_event(
    *,
    source_type: str,
    source_id: Optional[str],
    event_type: str,
    status: str,
    title: str,
    body: str,
    agent_id: str = "default",
    severity: str = "info",
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """写入一条收件箱事件，返回该事件。最新插入到列表头部。"""
    event = {
        "id": uuid.uuid4().hex,
        "agent_id": agent_id or "default",
        "source_type": source_type,
        "source_id": source_id or "",
        "event_type": event_type,
        "status": status,
        "severity": severity,
        "title": title,
        "body": body,
        "payload": payload or {},
        "read": False,
        "created_at": time.time(),
    }
    with _LOCK:
        events = _load_events()
        events.insert(0, event)
        del events[_MAX_EVENTS:]
        _save_events(events)
    return event


def list_events(
    *,
    limit: int = 50,
    offset: int = 0,
    source_type: Optional[str] = None,
    status: Optional[str] = None,
    agent_id: Optional[str] = None,
    unread_only: bool = False,
) -> list[dict[str, Any]]:
    with _LOCK:
        events = _load_events()
    if source_type:
        events = [e for e in events if e.get("source_type") == source_type]
    if status:
        events = [e for e in events if e.get("status") == status]
    if agent_id:
        events = [e for e in events if e.get("agent_id") == agent_id]
    if unread_only:
        events = [e for e in events if not bool(e.get("read"))]
    return events[offset : offset + max(limit, 0)]


def mark_read(event_ids: list[str]) -> int:
    if not event_ids:
        return 0
    ids = set(event_ids)
    updated = 0
    with _LOCK:
        events = _load_events()
        for e in events:
            if e.get("id") in ids and not bool(e.get("read")):
                e["read"] = True
                updated += 1
        if updated:
            _save_events(events)
    return updated


def mark_all_read() -> int:
    updated = 0
    with _LOCK:
        events = _load_events()
        for e in events:
            if not bool(e.get("read")):
                e["read"] = True
                updated += 1
        if updated:
            _save_events(events)
    return updated


def delete_event(event_id: str) -> bool:
    if not event_id:
        return False
    with _LOCK:
        events = _load_events()
        kept = [e for e in events if e.get("id") != event_id]
        if len(kept) != len(events):
            _save_events(kept)
            return True
    return False


def unread_count(source_type: Optional[str] = None) -> int:
    with _LOCK:
        events = _load_events()
    if source_type:
        events = [e for e in events if e.get("source_type") == source_type]
    return sum(1 for e in events if not bool(e.get("read")))
