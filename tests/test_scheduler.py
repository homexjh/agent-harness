"""定时任务调度器测试。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger
from fastapi.testclient import TestClient

from src.server import plugins as plugins_mod
from src.server import scheduler as sched_mod
from src.server.app import app
from src.server.scheduler import (
    _parse_cron_trigger,
    _run_job,
    get_history,
    run_job_now,
    start_scheduler,
    stop_scheduler,
    sync_jobs,
)


@pytest.fixture
def clean_scheduler_db(monkeypatch):
    """把 cron 数据与历史记录重定向到临时文件，避免污染 ~/.workbuddy。"""
    with tempfile.TemporaryDirectory() as td:
        cron_path = Path(td) / "cron.json"
        hist_path = Path(td) / "cron_history.json"
        monkeypatch.setattr(sched_mod, "CRON_DB_PATH", cron_path)
        monkeypatch.setattr(plugins_mod, "CRON_DB_PATH", cron_path)
        monkeypatch.setattr(sched_mod, "HISTORY_PATH", hist_path)
        # 确保调度器已停止，避免测试间互相干扰
        stop_scheduler()
        yield
        stop_scheduler()


def test_parse_cron_trigger():
    assert _parse_cron_trigger("0 9 * * *") is not None
    assert _parse_cron_trigger("0 0 9 * * *") is not None
    assert _parse_cron_trigger("not a cron") is None
    assert _parse_cron_trigger("1 2 3") is None


def test_run_job_records_success(clean_scheduler_db):
    record = _run_job({"id": "j1", "name": "test", "command": "echo hello"})
    assert record["status"] == "success"
    assert "hello" in record["output"]
    runs = get_history("j1")["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "success"


def test_run_job_records_empty_command_error(clean_scheduler_db):
    record = _run_job({"id": "j2", "name": "test", "command": "  "})
    assert record["status"] == "error"
    assert "命令为空" in record["error"]


def test_run_job_records_failure(clean_scheduler_db):
    record = _run_job({"id": "j3", "name": "test", "command": "exit 42"})
    assert record["status"] == "failed"
    assert "exit 42" in record["output"]


def test_run_job_now_missing(clean_scheduler_db):
    result = run_job_now("nonexistent")
    assert result["ok"] is False


def test_sync_jobs_loads_into_scheduler(clean_scheduler_db):
    cron_path = sched_mod.CRON_DB_PATH
    cron_path.write_text(json.dumps({
        "jobs": [
            {"id": "j1", "name": "daily", "schedule": "0 9 * * *", "command": "echo hi", "enabled": True},
            {"id": "j2", "name": "disabled", "schedule": "0 10 * * *", "command": "echo hi", "enabled": False},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    start_scheduler()
    sync_jobs()

    s = sched_mod.get_scheduler()
    assert s is not None
    jobs = {j.id: j for j in s.get_jobs()}
    assert "ah-cron-j1" in jobs
    assert "ah-cron-j2" not in jobs
    assert isinstance(jobs["ah-cron-j1"].trigger, CronTrigger)

    stop_scheduler()


def test_api_cron_crud_and_run(clean_scheduler_db):
    """端到端测试：创建、启用/禁用、手动执行、删除任务。"""
    with TestClient(app) as client:
        # 创建任务
        r = client.post("/cron", json={"name": "apitest", "schedule": "0 9 * * *", "command": "echo api"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        jid = data["id"]

        # 列表
        r = client.get("/cron")
        assert r.status_code == 200
        jobs = r.json()["jobs"]
        assert any(j["id"] == jid for j in jobs)

        # 手动执行
        r = client.post(f"/cron/{jid}/run")
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # 历史
        r = client.get(f"/cron/{jid}/history")
        assert r.status_code == 200
        runs = r.json()["runs"]
        assert len(runs) >= 1
        assert runs[0]["status"] == "success"
        assert "api" in runs[0]["output"]

        # 禁用
        r = client.patch(f"/cron/{jid}?enabled=false")
        assert r.status_code == 200
        r = client.get("/cron")
        job = next(j for j in r.json()["jobs"] if j["id"] == jid)
        assert job["enabled"] is False

        # 删除
        r = client.delete(f"/cron/{jid}")
        assert r.status_code == 200
        r = client.get("/cron")
        assert not any(j["id"] == jid for j in r.json()["jobs"])
