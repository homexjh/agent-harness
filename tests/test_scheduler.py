"""定时任务调度器测试。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
import pytest
from apscheduler.triggers.cron import CronTrigger
from fastapi.testclient import TestClient

from src.server import graph_provider
from src.server import plugins as plugins_mod
from src.server import scheduler as sched_mod
from src.server.app import app
from src.server.graph_provider import _parse_tavily
from src.server.scheduler import (
    _notification_command,
    _parse_cron_trigger,
    _parse_run_at,
    _run_job,
    get_history,
    run_job_now,
    set_agent_runner,
    start_scheduler,
    stop_scheduler,
    sync_jobs,
)


@pytest.fixture
def no_agent_runner():
    """测试前清掉全局 agent runner，避免相互污染，结束后恢复。"""
    saved = sched_mod._agent_runner
    set_agent_runner(None)
    yield
    set_agent_runner(saved)


# ---------------------------------------------------------------------------
# 1) 通知命令转义（修复 osascript 中文/引号语法错误 -2741 的回归测试）
# ---------------------------------------------------------------------------
def test_notification_command_escapes_quotes():
    cmd = _notification_command('say "hi" 你好', "标题")
    # 旧实现用 shlex.quote 把消息用单引号包住（'display notification 'say "hi"'...），
    # 导致 osascript 字符串提前闭合报 -2741。新实现改用双引号嵌入，消息内容前后不应出现单引号包裹。
    assert "'display notification '" not in cmd
    assert "'say" not in cmd
    # 双引号被转义，可安全嵌入 osascript 双引号字符串
    assert 'display notification "say \\"hi\\" 你好"' in cmd
    assert 'with title "标题"' in cmd


def test_notification_command_escapes_backslash():
    cmd = _notification_command("a\\b", "t")
    # 反斜杠先被转义，避免吞掉后续转义
    assert "a\\\\b" in cmd


# ---------------------------------------------------------------------------
# 2) run_at 解析（对齐 qwenpaw 的 once：支持 Z / 微秒 / 过期判定）
# ---------------------------------------------------------------------------
def test_parse_run_at_accepts_z_and_microseconds():
    # 用远未来时间，避免“当前时间已过期”导致返回 None
    assert _parse_run_at("2099-07-14T06:00:00Z") is not None
    assert _parse_run_at("2099-07-12T17:50:00.123456+00:00") is not None


def test_parse_run_at_rejects_past_and_empty():
    assert _parse_run_at("") is None
    assert _parse_run_at("   ") is None
    # 已过去的时间应返回 None（不排期）
    assert _parse_run_at("2000-01-01T00:00:00Z") is None
    # 非法格式
    assert _parse_run_at("not-a-time") is None


# ---------------------------------------------------------------------------
# 3) agent 任务分支（到点重跑 agent：对齐 QwenPaw cron task_type=agent）
# ---------------------------------------------------------------------------
def test_run_job_agent_branch_success(clean_scheduler_db, no_agent_runner):
    set_agent_runner(lambda prompt, job: f"AGENT_DONE:{prompt}")
    rec = _run_job({"id": "aj", "name": "t", "task_type": "agent", "prompt": "查新闻"})
    assert rec["status"] == "success"
    assert "AGENT_DONE:查新闻" in rec["output"]
    runs = get_history("aj")["runs"]
    assert runs and runs[0]["status"] == "success"


def test_run_job_agent_branch_missing_prompt(clean_scheduler_db, no_agent_runner):
    rec = _run_job({"id": "aj2", "name": "t", "task_type": "agent", "prompt": ""})
    assert rec["status"] == "error"
    assert "prompt" in rec["error"]


def test_run_job_agent_branch_runner_not_registered(clean_scheduler_db, no_agent_runner):
    rec = _run_job({"id": "aj3", "name": "t", "task_type": "agent", "prompt": "x"})
    assert rec["status"] == "error"
    assert "agent runner 未注册" in rec["error"]


# ---------------------------------------------------------------------------
# 4) web_search 解析（Tavily keyless，无网单测）
# ---------------------------------------------------------------------------
def test_parse_tavily_normal():
    data = {
        "results": [
            {"title": "A", "url": "http://a", "content": "摘要A"},
            {"title": "B", "url": "http://b"},
        ]
    }
    out = _parse_tavily(data)
    assert "[1] A" in out and "http://a" in out and "摘要A" in out
    assert "[2] B" in out


def test_parse_tavily_empty_fallback():
    assert "未找到相关结果" in _parse_tavily({"results": []})
    assert "未找到相关结果" in _parse_tavily({})


def test_web_search_via_tool_mocked(monkeypatch):
    """mock 掉 httpx，验证 web_search 工具把 Tavily 响应收敛成文本。"""

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"title": "热点", "url": "http://x", "content": "内容"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    tools = graph_provider._build_tools(tempfile.mkdtemp(), graph_provider.get_context_manager())
    ws = tools["web_search"]
    out = ws.underlying.func("国内热点新闻")
    assert "热点" in out and "http://x" in out


# ---------------------------------------------------------------------------
# 5) API：agent 任务创建后 prompt 持久化
# ---------------------------------------------------------------------------
def test_api_create_agent_job_persists_prompt(clean_scheduler_db):
    with TestClient(app) as client:
        r = client.post("/cron", json={
            "name": "news", "task_type": "agent",
            "prompt": "获取国内热点新闻", "schedule_type": "scheduled",
            "run_at": "2099-01-01T00:00:00Z",
        })
        assert r.status_code == 200
        jid = r.json()["id"]
        job = next(j for j in client.get("/cron").json()["jobs"] if j["id"] == jid)
        assert job["task_type"] == "agent"
        assert job["prompt"] == "获取国内热点新闻"


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
