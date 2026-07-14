"""Cron 定时任务调度器：把 cron.json 中的任务真正调度到 APScheduler 并在触发时执行。

命令默认在工作区目录以 shell 方式执行，执行结果写入 cron_history.json。
"""
from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

# ---------------------------------------------------------------------------
# 路径约定（与 plugins.py 保持一致）
# ---------------------------------------------------------------------------
def _workbuddy_dir() -> Path:
    p = Path.home() / ".workbuddy"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _workspace_dir() -> Path:
    p = _workbuddy_dir() / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db_path(name: str) -> Path:
    return _workbuddy_dir() / name


CRON_DB_PATH = _db_path("cron.json")
HISTORY_PATH = _db_path("cron_history.json")


def _cron_db_path() -> Path:
    """暴露 cron.json 路径（供 create_timer 等写入一次性任务）。"""
    return CRON_DB_PATH

# ---------------------------------------------------------------------------
# 调度器实例
# ---------------------------------------------------------------------------
_scheduler: Optional[BackgroundScheduler] = None


def _load_db(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_db(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_run(job: dict, status: str, output: str = "", error: str = "") -> dict:
    """记录一次任务执行结果，返回 run 记录。"""
    db = _load_db(HISTORY_PATH)
    runs = db.get("runs", [])
    record = {
        "id": f"run-{uuid.uuid4().hex[:8]}",
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "command": job.get("command"),
        "scheduled_at": datetime.now(timezone.utc).isoformat(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "output": output,
        "error": error,
    }
    runs.append(record)
    db["runs"] = runs[-200:]  # 只保留最近 200 条
    _save_db(HISTORY_PATH, db)
    return record


def _run_job(job: dict) -> dict:
    """执行单个 cron 任务。注意：APScheduler 会在后台线程中调用此函数。"""
    command = (job.get("command") or "").strip()
    if not command:
        return _record_run(job, "error", error="命令为空")

    timeout = job.get("timeout", 300)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=_workspace_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = f"[exit {proc.returncode}]\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        status = "success" if proc.returncode == 0 else "failed"
        rec = _record_run(job, status, output=output)
    except subprocess.TimeoutExpired:
        rec = _record_run(job, "timeout", error=f"命令执行超过 {timeout} 秒")
    except Exception as e:
        rec = _record_run(job, "error", error=str(e))
    else:
        # 一次性任务（有 run_at）触发后从列表移除，保持列表干净（对齐 qwenpaw once）
        if job.get("run_at") and rec.get("status") in ("success", "failed"):
            try:
                _remove_job_from_db(job.get("id"))
            except Exception:
                pass
    return rec


def _make_job_id(jid: str) -> str:
    return f"ah-cron-{jid}"


def _parse_cron_trigger(schedule: str, timezone: str | None = None) -> Optional[CronTrigger]:
    """把 schedule 解析为 APScheduler CronTrigger。支持 5 位或 6 位 cron 表达式，可指定时区。"""
    parts = schedule.strip().split()
    if len(parts) not in (5, 6):
        return None
    try:
        fields = ["minute", "hour", "day", "month", "day_of_week"]
        if len(parts) == 6:
            fields.insert(0, "second")
        kwargs = {f: p for f, p in zip(fields, parts)}
        if timezone:
            kwargs["timezone"] = timezone
        return CronTrigger(**kwargs)
    except Exception:
        return None


def _parse_run_at(run_at: str, tz: str | None = None) -> Optional[DateTrigger]:
    """把 run_at（ISO8601）解析为 APScheduler DateTrigger（一次性任务，对齐 qwenpaw 的 once）。

    返回 None 表示解析失败或时间已过期。

    解析策略：优先用标准库 ``datetime.fromisoformat``（create_timer 写入的
    ``datetime.isoformat()`` 形如 ``2026-07-12T17:50:00.123456+00:00``，3.11+ 原生支持），
    仅在失败时回退到 ``dateutil``，避免 dateutil 缺失时直接抛 ModuleNotFoundError 导致
    一次性提醒任务调度失败（① 对齐 qwenpaw 的关键路径）。

    注意：参数名用 ``tz`` 而非 ``timezone``，避免遮蔽模块顶部的 ``datetime.timezone`` 导入。
    """
    raw = (run_at or "").strip()
    if not raw:
        return None

    dt = None
    # 1) 标准库优先（兼容 Z 后缀与带微秒的 ISO 形式）
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        dt = None
    # 2) 回退 dateutil（可选依赖，缺失不致命）
    if dt is None:
        try:
            from dateutil import parser as _dtparser  # type: ignore
            dt = _dtparser.isoparse(raw)
        except Exception:
            dt = None
    if dt is None:
        return None

    if dt.tzinfo is None and tz:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        except Exception:
            pass
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt <= datetime.now(timezone.utc):
        return None
    return DateTrigger(run_date=dt, timezone=tz or "UTC")


def schedule_type_from_cron(schedule: str) -> str:
    """根据 cron 表达式推断 Schedule Type，用于兼容旧数据。"""
    s = (schedule or "").strip()
    parts = s.split()
    if len(parts) not in (5, 6):
        return "custom"
    minute, hour, dom, month, dow = parts[:5]
    if hour == "*" and dom == "*" and month == "*" and dow == "*":
        return "hourly"
    if dom == "*" and month == "*" and dow == "*":
        return "daily"
    if dom == "*" and month == "*" and dow != "*":
        return "weekly"
    return "custom"


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------
def _remove_job_from_db(jid: str) -> None:
    """从 cron.json 中移除某个任务（一次性任务触发后清理用）。"""
    db = _load_db(CRON_DB_PATH)
    before = len(db.get("jobs", []))
    db["jobs"] = [j for j in db.get("jobs", []) if j.get("id") != jid]
    if len(db["jobs"]) != before:
        _save_db(CRON_DB_PATH, db)


def sync_jobs() -> None:
    """从 cron.json 重新加载所有任务到调度器。增删改后应调用。

    支持两类调度：
    - 周期任务：job["schedule"] 为 cron 表达式（5/6 位）。
    - 一次性任务（对齐 qwenpaw 的 once）：job["run_at"] 为 ISO8601 时间，
      用 DateTrigger 在指定时刻触发一次，触发后自动从列表移除。
    """
    global _scheduler
    if _scheduler is None:
        return

    db = _load_db(CRON_DB_PATH)
    jobs = db.get("jobs", [])
    desired_ids = {_make_job_id(j["id"]) for j in jobs if j.get("id")}

    # 移除已删除的任务
    for existing in list(_scheduler.get_jobs()):
        if existing.id.startswith("ah-cron-") and existing.id not in desired_ids:
            try:
                _scheduler.remove_job(existing.id)
            except Exception:
                pass

    # 添加或更新任务
    for job in jobs:
        jid = job.get("id")
        if not jid:
            continue
        if not job.get("enabled", True):
            try:
                _scheduler.remove_job(_make_job_id(jid))
            except Exception:
                pass
            continue

        # 一次性任务优先用 run_at（DateTrigger）
        run_at = job.get("run_at")
        if run_at:
            trigger = _parse_run_at(run_at, job.get("timezone"))
        else:
            trigger = _parse_cron_trigger(job.get("schedule", ""), job.get("timezone"))
        if trigger is None:
            continue

        _scheduler.add_job(
            _run_job,
            trigger=trigger,
            id=_make_job_id(jid),
            name=job.get("name", jid),
            replace_existing=True,
            args=[job],
            misfire_grace_time=3600,
        )


def start_scheduler() -> None:
    """启动后台调度器并从磁盘加载任务。"""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler()
    _scheduler.start()
    sync_jobs()
    sync_memory_jobs()


def stop_scheduler() -> None:
    """停止后台调度器。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown()
        _scheduler = None


def get_scheduler() -> Optional[BackgroundScheduler]:
    return _scheduler


def _notification_command(message: str, title: str = "提醒") -> str:
    """生成跨平台桌面通知命令（macOS osascript / Linux notify-send）。"""
    import shlex

    safe_msg = shlex.quote(message)
    safe_title = shlex.quote(title)
    return (
        f"(command -v osascript >/dev/null 2>&1 && osascript -e 'display notification {safe_msg} with title {safe_title}') || "
        f"(command -v notify-send >/dev/null 2>&1 && notify-send {safe_title} {safe_msg}) || "
        f"echo 'no notification provider'"
    )


def schedule_one_time(
    command: str,
    run_at: datetime,
    *,
    name: str = "one-time",
    job_id: Optional[str] = None,
    timeout: int = 60,
) -> dict:
    """调度一个一次性后台任务（DateTrigger），执行完后自动从调度器移除。

    返回 {"ok": True, "job_id": ..., "run_at": ...} 或 {"ok": False, "error": ...}。
    """
    global _scheduler
    if _scheduler is None:
        return {"ok": False, "error": "scheduler not started"}
    if run_at <= datetime.now(timezone.utc):
        return {"ok": False, "error": "run_at must be in the future"}

    jid = job_id or f"ah-once-{uuid.uuid4().hex[:8]}"

    def _wrapped() -> None:
        _run_job({"id": jid, "name": name, "command": command, "timeout": timeout})
        # 执行完成后从调度器移除，避免残留
        try:
            _scheduler.remove_job(jid)
        except Exception:
            pass

    try:
        _scheduler.add_job(
            _wrapped,
            trigger=DateTrigger(run_date=run_at, timezone=timezone.utc),
            id=jid,
            name=name,
            replace_existing=True,
            misfire_grace_time=3600,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, "job_id": jid, "run_at": run_at.isoformat()}


def run_job_now(jid: str) -> dict:
    """手动触发一个任务。"""
    db = _load_db(CRON_DB_PATH)
    job = next((j for j in db.get("jobs", []) if j.get("id") == jid), None)
    if job is None:
        return {"ok": False, "error": "任务不存在"}
    record = _run_job(job)
    return {"ok": True, "run": record}


def get_history(jid: Optional[str] = None) -> dict:
    """获取执行历史。"""
    db = _load_db(HISTORY_PATH)
    runs = db.get("runs", [])
    if jid:
        runs = [r for r in runs if r.get("job_id") == jid]
    return {"runs": list(reversed(runs[-50:]))}


def validate_schedule(schedule: str) -> dict:
    """校验 schedule 是否为可识别的 cron 表达式。"""
    trigger = _parse_cron_trigger(schedule)
    return {"valid": trigger is not None, "schedule": schedule}


# ---------------------------------------------------------------------------
# 友好文案（把 cron 表达式转成中文，方便前端直接展示）
# ---------------------------------------------------------------------------
_DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# crontab 约定 0=周日 … 6=周六；7 作为周日的别名
_NUM_TO_DAY = {
    "0": 6, "7": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5,
}


def _is_int(x: str) -> bool:
    try:
        int(x)
        return True
    except (TypeError, ValueError):
        return False


def schedule_text(schedule: str) -> str:
    """把 5/6 位 cron 表达式转成中文友好描述。

    例：
      "0 * * * *"        -> 每小时整点
      "30 9 * * *"       -> 每天 09:30
      "0 9 * * 1,3,5"    -> 每周一、三、五 09:00
      "*/15 9-17 * * 1-5"-> （自定义）*/15 9-17 * * 1-5
    """
    s = (schedule or "").strip()
    parts = s.split()
    if len(parts) not in (5, 6):
        return s or "（无效表达式）"
    minute, hour, dom, month, dow = parts[:5]

    # 每小时整点
    if hour == "*" and dom == "*" and month == "*" and dow == "*" and minute == "0":
        return "每小时整点"
    # 每小时的某分
    if hour == "*" and dom == "*" and month == "*" and dow == "*" and _is_int(minute):
        return f"每小时第 {int(minute)} 分"
    # 每天 HH:MM
    if dom == "*" and month == "*" and dow == "*" and _is_int(hour) and _is_int(minute):
        return f"每天 {int(hour):02d}:{int(minute):02d}"
    # 每周
    if dom == "*" and month == "*" and dow != "*" and _is_int(hour) and _is_int(minute):
        days = _days_of_week_text(dow)
        if days:
            return f"每周{('、'.join(days))} {int(hour):02d}:{int(minute):02d}"
    return s


def _days_of_week_text(dow: str) -> list[str]:
    """把 day-of-week 字段解析为 ['周一', ...] 列表，无法无损解析则返回 []。"""
    out: list[int] = []
    for raw in dow.split(","):
        r = raw.strip()
        if not r:
            return []
        if "-" in r:
            a, _, b = r.partition("-")
            if not (_is_int(a) and _is_int(b)):
                return []
            ia, ib = int(a), int(b)
            if str(ia) not in _NUM_TO_DAY or str(ib) not in _NUM_TO_DAY:
                return []
            sa, sb = _NUM_TO_DAY[str(ia)], _NUM_TO_DAY[str(ib)]
            if sa > sb:
                return []
            for d in range(sa, sb + 1):
                if d not in out:
                    out.append(d)
            continue
        if _is_int(r):
            if r not in _NUM_TO_DAY:
                return []
            d = _NUM_TO_DAY[r]
            if d not in out:
                out.append(d)
            continue
        # 名称（mon/sun 等）暂不支持
        return []
    return [_DAY_NAMES[d] for d in sorted(out)]


def get_job_state(jid: str) -> dict:
    """返回单个任务的运行时状态：下次执行、最近一次执行结果。"""
    out = {
        "next_run_at": None,
        "last_status": None,
        "last_run_at": None,
        "last_error": None,
    }
    sched = get_scheduler()
    if sched is not None:
        job = sched.get_job(_make_job_id(jid))
        nrt = getattr(job, "next_run_time", None)
        if nrt is not None:
            # 转成用户本地时间展示更友好
            out["next_run_at"] = nrt.isoformat()
    hist = get_history(jid)
    runs = hist.get("runs", [])
    if runs:
        last = runs[0]  # get_history 已按时间倒序
        out["last_status"] = last.get("status")
        out["last_run_at"] = last.get("started_at")
        out["last_error"] = last.get("error")
    return out


# ---------------------------------------------------------------------------
# Memory Manager 调度（dream cron）
# ---------------------------------------------------------------------------
def _memory_dream_job() -> None:
    """后台线程中触发一次记忆 dream（整合/去重）。"""
    try:
        from .memory import get_memory_manager

        mm = get_memory_manager()
        if mm is not None:
            mm.dream()
    except Exception as e:  # noqa: BLE001
        print(f"[memory] dream job failed: {e}")


def sync_memory_jobs() -> None:
    """按 memory_config.json 的 dream_cron 调度 dream 任务。

    在 start_scheduler() 与 PUT /memory/config 后调用，保证 dream 周期与配置一致。
    """
    global _scheduler
    if _scheduler is None:
        return
    # 先移除旧的 memory dream 任务（配置变更/关闭时自然失效）
    try:
        _scheduler.remove_job("ah-memory-dream")
    except Exception:
        pass
    try:
        from . import memory as memory_mod

        cfg = memory_mod.runtime_memory_config()
    except Exception:
        return
    if cfg.backend == "none":
        return
    cron = (cfg.reme_light_memory_config.dream_cron or "").strip()
    if not cron:
        return
    trigger = _parse_cron_trigger(cron)
    if trigger is None:
        return
    _scheduler.add_job(
        _memory_dream_job,
        trigger=trigger,
        id="ah-memory-dream",
        name="memory-dream",
        replace_existing=True,
        misfire_grace_time=3600,
    )
