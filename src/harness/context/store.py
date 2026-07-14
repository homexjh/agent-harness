"""SQLite 写穿式 turn store（零丢失）。

对照 QwenPaw Scroll Context：
- write-through 到 SQLite，每一轮原始消息全文落库，永不摘要丢失。
- 每个 thread_id 用列分区；fold 只是"上下文里替换成桩"，原始数据始终可召回。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional

from langchain_core.messages import message_to_dict


class TurnStore:
    """线程安全、可文件持久化的原始消息存储。

    - 默认内存库（同一进程内跨 turn 复用即可持久化）；
    - 传入 db_path 则用文件库，获得跨重启的崩溃恢复能力。
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_thread_seq
                    ON turns(thread_id, seq);
                """
            )
            self._conn.commit()

    def append(self, thread_id: str, seq: int, message) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO turns(thread_id, seq, data, created_at) VALUES (?,?,?,?)",
                (
                    thread_id,
                    seq,
                    json.dumps(message_to_dict(message), ensure_ascii=False),
                    time.time(),
                ),
            )
            self._conn.commit()

    def count(self, thread_id: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM turns WHERE thread_id=?", (thread_id,)
            ).fetchone()[0]

    def all(self, thread_id: str) -> list:
        """返回 [{"seq": int, "msg": message_to_dict(dict)}, ...]，按 seq 升序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, data FROM turns WHERE thread_id=? ORDER BY seq",
                (thread_id,),
            ).fetchall()
        return [{"seq": r["seq"], "msg": json.loads(r["data"])} for r in rows]

    def close(self) -> None:
        self._conn.close()

    def thread_ids(self) -> list:
        """返回所有出现过 turn 的 thread_id 列表（按最近活动倒序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT thread_id FROM turns ORDER BY id DESC"
            ).fetchall()
        return [r["thread_id"] for r in rows]

    def clear(self, thread_id: str) -> int:
        """删除指定 thread 的全部 turn，返回删除条数。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM turns WHERE thread_id=?", (thread_id,)
            )
            self._conn.commit()
            return cur.rowcount
