"""收件箱（inbox）测试：定时任务结果投递到收件箱，前端轮询渲染进会话窗口。

对齐 QwenPaw 的 /inbox/events 默认行为：cron 成功后结果进收件箱，而不是只进独立管理面板。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.server import inbox_store as inbox_mod
from src.server import plugins as plugins_mod
from src.server import scheduler as sched_mod
from src.server.app import app
from src.server.scheduler import _run_job, set_agent_runner


@pytest.fixture
def clean_inbox(monkeypatch):
    """把 inbox 存储重定向到临时文件，避免污染 ~/.workbuddy/inbox_events.json。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        monkeypatch.setattr(inbox_mod, "DATA_HOME", tmp)
        monkeypatch.setattr(inbox_mod, "_PATH", tmp / "inbox_events.json")
        yield tmp


@pytest.fixture
def clean_scheduler_db(monkeypatch):
    """把 cron 数据与历史记录重定向到临时文件。"""
    with tempfile.TemporaryDirectory() as td:
        cron_path = Path(td) / "cron.json"
        hist_path = Path(td) / "cron_history.json"
        monkeypatch.setattr(sched_mod, "CRON_DB_PATH", cron_path)
        monkeypatch.setattr(plugins_mod, "CRON_DB_PATH", cron_path)
        monkeypatch.setattr(sched_mod, "HISTORY_PATH", hist_path)
        sched_mod.stop_scheduler()
        yield
        sched_mod.stop_scheduler()


# ---------------------------------------------------------------------------
# 1) 存储层：append / list / mark / delete / unread_count
# ---------------------------------------------------------------------------
def test_append_and_list_newest_first(clean_inbox):
    e1 = inbox_mod.append_event(
        source_type="cron", source_id="j1", event_type="cron_result",
        status="success", title="任务A", body="结果A",
    )
    e2 = inbox_mod.append_event(
        source_type="cron", source_id="j2", event_type="cron_result",
        status="failed", title="任务B", body="结果B",
    )
    assert e1["id"] and e2["id"]
    assert e1["read"] is False and e2["read"] is False
    events = inbox_mod.list_events()
    assert len(events) == 2
    # 最新插入的在头部
    assert events[0]["id"] == e2["id"]
    assert events[1]["id"] == e1["id"]


def test_list_filters(clean_inbox):
    inbox_mod.append_event(source_type="cron", source_id="j1", event_type="cron_result",
                           status="success", title="c", body="1")
    inbox_mod.append_event(source_type="other", source_id="j2", event_type="x",
                           status="success", title="o", body="2")
    assert len(inbox_mod.list_events(source_type="cron")) == 1
    assert len(inbox_mod.list_events(status="success")) == 2
    assert len(inbox_mod.list_events(unread_only=True)) == 2
    assert len(inbox_mod.list_events(limit=1)) == 1


def test_mark_read_and_unread_count(clean_inbox):
    e1 = inbox_mod.append_event(source_type="cron", source_id="j1", event_type="cron_result",
                                status="success", title="c", body="1")
    e2 = inbox_mod.append_event(source_type="cron", source_id="j2", event_type="cron_result",
                                status="success", title="c", body="2")
    assert inbox_mod.unread_count() == 2
    n = inbox_mod.mark_read([e1["id"]])
    assert n == 1
    assert inbox_mod.unread_count() == 1
    reloaded = inbox_mod.list_events()
    by_id = {e["id"]: e for e in reloaded}
    assert by_id[e1["id"]]["read"] is True
    assert by_id[e2["id"]]["read"] is False
    # 全部已读
    assert inbox_mod.mark_all_read() == 1
    assert inbox_mod.unread_count() == 0


def test_delete_event(clean_inbox):
    e = inbox_mod.append_event(source_type="cron", source_id="j1", event_type="cron_result",
                               status="success", title="c", body="1")
    assert inbox_mod.delete_event(e["id"]) is True
    assert inbox_mod.delete_event("missing") is False
    assert inbox_mod.list_events() == []


def test_atomic_write_not_corrupted_on_reload(clean_inbox):
    for i in range(5):
        inbox_mod.append_event(source_type="cron", source_id=f"j{i}", event_type="cron_result",
                               status="success", title=f"t{i}", body=f"b{i}")
    # 直接读盘校验 JSON 合法
    raw = (clean_inbox / "inbox_events.json").read_text(encoding="utf-8")
    import json
    parsed = json.loads(raw)
    assert isinstance(parsed, list) and len(parsed) == 5


# ---------------------------------------------------------------------------
# 2) 调度执行器：cron 成功后默认投递 inbox（save_result_to_inbox 默认 True）
# ---------------------------------------------------------------------------
def test_run_job_command_pushes_inbox(clean_inbox, clean_scheduler_db):
    rec = _run_job({"id": "j1", "name": "t", "command": "echo hello"})
    assert rec["status"] == "success"
    events = inbox_mod.list_events(source_type="cron")
    assert len(events) == 1
    ev = events[0]
    assert ev["status"] == "success"
    assert "hello" in ev["body"]
    assert "定时任务完成" in ev["title"]


def test_run_job_failure_pushes_inbox_with_error(clean_inbox, clean_scheduler_db):
    rec = _run_job({"id": "j2", "name": "t", "command": "exit 42"})
    assert rec["status"] == "failed"
    events = inbox_mod.list_events(source_type="cron")
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert "定时任务失败" in events[0]["title"]
    assert "exit 42" in events[0]["body"]


def test_run_job_save_result_to_inbox_false_skips(clean_inbox, clean_scheduler_db):
    _run_job({"id": "j3", "name": "t", "command": "echo x", "save_result_to_inbox": False})
    assert inbox_mod.list_events() == []


def test_run_job_agent_branch_pushes_inbox(clean_inbox, clean_scheduler_db):
    saved = sched_mod._agent_runner
    set_agent_runner(lambda prompt, job: f"AGENT_OUT:{prompt}")
    try:
        rec = _run_job({"id": "j4", "name": "t", "task_type": "agent", "prompt": "查新闻"})
        assert rec["status"] == "success"
        events = inbox_mod.list_events(source_type="cron")
        assert len(events) == 1
        assert "AGENT_OUT:查新闻" in events[0]["body"]
    finally:
        set_agent_runner(saved)


# ---------------------------------------------------------------------------
# 3) REST 端点：/inbox/events /unread /read /read/all /events/{id}
# ---------------------------------------------------------------------------
def test_inbox_endpoints_e2e(clean_inbox):
    with TestClient(app) as client:
        # 先制造一条事件（走调度执行器）
        _run_job({"id": "j1", "name": "t", "command": "echo hi"})
        # 拉取
        r = client.get("/inbox/events?source_type=cron")
        assert r.status_code == 200
        data = r.json()
        assert data["unread"] == 1
        assert len(data["events"]) == 1
        eid = data["events"][0]["id"]
        # 未读角标
        r = client.get("/inbox/unread?source_type=cron")
        assert r.status_code == 200 and r.json()["count"] == 1
        # 标记已读
        r = client.post("/inbox/read", json={"ids": [eid]})
        assert r.status_code == 200 and r.json()["updated"] == 1
        r = client.get("/inbox/unread?source_type=cron")
        assert r.json()["count"] == 0
        # 删除
        r = client.delete(f"/inbox/events/{eid}")
        assert r.status_code == 200 and r.json()["ok"] is True
        r = client.get("/inbox/events?source_type=cron")
        assert r.json()["events"] == []


def test_inbox_read_all_endpoint(clean_inbox):
    with TestClient(app) as client:
        _run_job({"id": "j1", "name": "t", "command": "echo a"})
        _run_job({"id": "j2", "name": "t", "command": "echo b"})
        r = client.post("/inbox/read/all")
        assert r.status_code == 200 and r.json()["updated"] == 2
        assert client.get("/inbox/unread?source_type=cron").json()["count"] == 0


def test_cron_create_defaults_save_result_to_inbox_true(clean_inbox, clean_scheduler_db):
    with TestClient(app) as client:
        r = client.post("/cron", json={
            "name": "news", "command": "echo hi", "schedule_type": "scheduled",
            "run_at": "2099-01-01T00:00:00Z",
        })
        assert r.status_code == 200
        jid = r.json()["id"]
        job = next(j for j in client.get("/cron").json()["jobs"] if j["id"] == jid)
        assert job["save_result_to_inbox"] is True
