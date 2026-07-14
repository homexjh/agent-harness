"""设置中心 API —— 对齐 QwenPaw Settings 的 7 个缺失模块。

全部端点都在 LLM 主循环之外（纯 REST 读写 JSON / 文件），不会阻塞聊天流。
数据持久化在独立目录 ~/.agent-harness/ 下（不再占用 ~/.workbuddy，那是 WorkBuddy IDE 的数据目录），
每个模块一个独立 JSON，互不污染。

包含：
- Environments    /settings/envs
- Security        /settings/security   （含 enable_auth / approval_level 即时生效）
- Voice           /settings/voice
- Token Usage     /settings/token-usage (+ /details)
- Backups         /settings/backups (+ create/list/download/restore/delete)
- Debug           /settings/debug/logs
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .token_usage import read_details, read_summary
from .config import DATA_HOME

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings")

# 统一数据目录：独立 ~/.agent-harness（不再写入 ~/.workbuddy，那是 WorkBuddy IDE 的数据目录）
_WB = DATA_HOME
_WB.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 通用 JSON 读写
# ---------------------------------------------------------------------------
def _json_path(name: str) -> Path:
    return _WB / name


def _load(name: str, default: Any = None) -> Any:
    p = _json_path(name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save(name: str, data: Any) -> None:
    p = _json_path(name)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ===========================================================================
# Environments
# ===========================================================================
class EnvVar(BaseModel):
    key: str
    value: str
    secret: bool = False


@router.get("/envs")
def get_envs() -> Dict[str, Any]:
    data = _load("envs.json", {"vars": []})
    return data


@router.put("/envs")
def put_envs(payload: Dict[str, Any]) -> Dict[str, Any]:
    vars_list = payload.get("vars", [])
    # 校验
    seen = set()
    clean = []
    for v in vars_list:
        k = (v.get("key") or "").strip()
        if not k:
            continue
        if k in seen:
            continue
        seen.add(k)
        clean.append({"key": k, "value": str(v.get("value", "")), "secret": bool(v.get("secret", False))})
    _save("envs.json", {"vars": clean})
    _apply_envs(clean)
    return {"ok": True, "vars": clean}


def _apply_envs(vars_list: List[Dict[str, Any]]) -> None:
    """把环境变量应用到当前进程（供工具/子进程读取）。"""
    for v in vars_list:
        try:
            os.environ[v["key"]] = v["value"]
        except Exception:
            pass


def apply_envs_on_startup() -> None:
    try:
        data = _load("envs.json", {"vars": []})
        _apply_envs(data.get("vars", []))
    except Exception:
        pass


# ===========================================================================
# Security
# ===========================================================================
@router.get("/security")
def get_security() -> Dict[str, Any]:
    from .config import get_config

    cfg = get_config()
    local = _load("security.json", {})
    return {
        "tool_guard": local.get("tool_guard", {"enabled": True, "block_destructive": True}),
        "file_guard": local.get("file_guard", {"enabled": True, "block_path_escape": True}),
        "skill_scanner": local.get("skill_scanner", {"enabled": True, "scan_on_load": True}),
        # 与全局配置联动（即时生效的部分）
        "enable_auth": bool(cfg.security.get("enable_auth", False)),
        "approval_level": cfg.approval_level or "AUTO",
        "allow_no_auth_hosts": local.get("allow_no_auth_hosts", []),
    }


@router.put("/security")
def put_security(payload: Dict[str, Any]) -> Dict[str, Any]:
    from .config import save_config

    local = {
        "tool_guard": payload.get("tool_guard", {"enabled": True, "block_destructive": True}),
        "file_guard": payload.get("file_guard", {"enabled": True, "block_path_escape": True}),
        "skill_scanner": payload.get("skill_scanner", {"enabled": True, "scan_on_load": True}),
        "allow_no_auth_hosts": payload.get("allow_no_auth_hosts", []),
    }
    _save("security.json", local)
    # 即时生效部分：enable_auth / approval_level 同步进运行时配置
    partial: Dict[str, Any] = {}
    if "enable_auth" in payload or "approval_level" in payload:
        sec = {}
        if "enable_auth" in payload:
            sec["enable_auth"] = bool(payload["enable_auth"])
        if "approval_level" in payload:
            partial["approval_level"] = payload["approval_level"]
        if sec:
            partial["security"] = sec
        if "enable_auth" in payload and not payload["enable_auth"]:
            partial["service"] = {"api_key": ""}
    if partial:
        save_config(partial)
    return {"ok": True, **get_security()}


# ===========================================================================
# Voice Transcription
# ===========================================================================
@router.get("/voice")
def get_voice() -> Dict[str, Any]:
    return _load(
        "voice.json",
        {
            "enabled": False,
            "audio_mode": "off",  # off | push-to-talk | always-on
            "stt_provider": "whisper-local",  # whisper-local | openai | none
            "tts_provider": "none",  # none | openai | edge
            "language": "zh",
            "note": "转录依赖 STT 提供方；whisper-local 需要本地 whisper 服务，留空则仅保存配置。",
        },
    )


@router.put("/voice")
def put_voice(payload: Dict[str, Any]) -> Dict[str, Any]:
    cur = get_voice()
    cur.update({k: payload[k] for k in ["enabled", "audio_mode", "stt_provider", "tts_provider", "language"] if k in payload})
    _save("voice.json", cur)
    return {"ok": True, **cur}


# ===========================================================================
# Token Usage
# ===========================================================================
@router.get("/token-usage")
def token_usage_summary() -> Dict[str, Any]:
    return read_summary()


@router.get("/token-usage/details")
def token_usage_details(limit: int = Query(default=200, le=2000)) -> Dict[str, Any]:
    return {"details": read_details(limit)}


# ===========================================================================
# Backups
# ===========================================================================
_BACKUP_DIR = _WB / "backups"
_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# 仅备份 agent-harness 真正管理的配置，避免把整个仓库/.git 一并打进 zip
# （DATA_HOME 是 agent-harness 的独立数据目录，config 目录可能被 AGENT_CONFIG_DIR
#   指向其它仓库，因此这里只取 runtime.json 文件本身，而非整个 config 目录树）。
from .config import CONFIG_PATH as _RUNTIME_CONFIG_PATH  # noqa: E402

_STATE_FILES = [
    "cron.json",
    "cron_history.json",
    "mcp.json",
    "agents.json",
    "skills_state.json",
    "core_files_state.json",
    "tools_state.json",
    "memory_config.json",
    "context_config.json",
    "sessions.json",
    "token_usage.json",
    "security.json",
    "voice.json",
    "envs.json",
]

# 技能只备份清单（SKILL.md / SKILL.json / SKILL.yaml），不打包二进制资源，控制体积
_SKILL_MANIFEST_NAMES = {"skill.md", "skill.json", "skill.yaml", "skill.yml"}


class BackupCreate(BaseModel):
    name: str = ""


def _list_backups() -> List[Dict[str, Any]]:
    out = []
    for z in sorted(_BACKUP_DIR.glob("*.zip"), reverse=True):
        try:
            st = z.stat()
            out.append(
                {
                    "id": z.stem,
                    "name": z.stem,
                    "size": st.st_size,
                    "created": int(st.st_mtime),
                }
            )
        except Exception:
            pass
    return out


@router.get("/backups")
def list_backups() -> Dict[str, Any]:
    return {"backups": _list_backups()}


@router.post("/backups")
def create_backup(body: BackupCreate) -> Dict[str, Any]:
    bid = body.name.strip() or f"backup-{time.strftime('%Y%m%d-%H%M%S')}"
    zpath = _BACKUP_DIR / f"{bid}.zip"
    if zpath.exists():
        bid = f"{bid}-{int(time.time())}"
        zpath = _BACKUP_DIR / f"{bid}.zip"
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            # 运行时配置（仅 runtime.json 本身，避免把整个 config 目录树打进包）
            _rtc = Path(_RUNTIME_CONFIG_PATH)
            if _rtc.exists():
                zf.write(_rtc, "config/runtime.json")
            # 状态 JSON
            for name in _STATE_FILES:
                p = _WB / name
                if p.exists():
                    zf.write(p, f"state/{name}")
            # 技能：仅清单文件（SKILL.md / SKILL.json / SKILL.yaml），不包二进制
            skills_dir = _WB / "skills"
            if skills_dir.exists():
                for f in skills_dir.rglob("*"):
                    if f.is_file() and f.name.lower() in _SKILL_MANIFEST_NAMES:
                        zf.write(f, f"skills/{f.relative_to(skills_dir)}")
        return {"ok": True, "id": zpath.stem, "name": zpath.stem}
    except Exception as e:
        # 失败则清理可能产生的半截 zip，避免留下损坏文件
        try:
            if zpath.exists():
                zpath.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"备份失败：{e}")


@router.get("/backups/{bid}/download")
def download_backup(bid: str) -> FileResponse:
    zpath = _BACKUP_DIR / f"{bid}.zip"
    if not zpath.exists():
        raise HTTPException(status_code=404, detail="备份不存在")
    return FileResponse(zpath, filename=f"{bid}.zip", media_type="application/zip")


@router.post("/backups/{bid}/restore")
def restore_backup(bid: str) -> Dict[str, Any]:
    zpath = _BACKUP_DIR / f"{bid}.zip"
    if not zpath.exists():
        raise HTTPException(status_code=404, detail="备份不存在")
    try:
        with zipfile.ZipFile(zpath, "r") as zf:
            # 仅还原到 DATA_HOME/state 与 config 目录（安全：不解压任意路径）
            _rtc_parent = Path(_RUNTIME_CONFIG_PATH).parent
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if info.filename.startswith("state/"):
                    zf.extract(info, _WB)
                elif info.filename.startswith("config/"):
                    zf.extract(info, _rtc_parent)
        return {"ok": True, "id": bid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"恢复失败：{e}")


@router.delete("/backups/{bid}")
def delete_backup(bid: str) -> Dict[str, Any]:
    zpath = _BACKUP_DIR / f"{bid}.zip"
    if zpath.exists():
        zpath.unlink()
    return {"ok": True}


# ===========================================================================
# Debug —— 读取后端日志文件末尾 N 行
# ===========================================================================
# 日志位于 agent-harness 项目自己的 logs/ 目录（与 app.py 中 RotatingFileHandler 落盘路径保持一致）
_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "agent-harness.log"


@router.get("/debug/logs")
def debug_logs(lines: int = Query(default=200, le=2000)) -> Dict[str, Any]:
    if not _LOG_PATH.exists():
        return {"lines": [], "path": str(_LOG_PATH), "exists": False}
    try:
        # 用 deque 高效取末尾 N 行，避免一次性读大文件
        from collections import deque

        with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=lines)
        return {"lines": [l.rstrip("\n") for l in tail], "path": str(_LOG_PATH), "exists": True}
    except Exception as e:
        return {"lines": [], "path": str(_LOG_PATH), "exists": True, "error": str(e)}
