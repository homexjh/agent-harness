"""插件/配件管理端点：Files、Tools、Skills、MCP、ACP、Agents、Cron、Heartbeat、Sessions。

提供 QwenPaw 侧边栏所需的全部管理视图，数据持久化在独立目录 ~/.agent-harness/ 下（不再占用 ~/.workbuddy）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import FileResponse

from .config import DATA_HOME
from .auth import require_auth
from pydantic import BaseModel, Field

from .graph_provider import get_context_manager, reset_context_manager
from . import memory as memory_mod
from . import context_config as cc_mod
from .core_files import get_core_files_manager
from .scheduler import (
    CRON_DB_PATH,
    get_history,
    get_job_state,
    run_job_now,
    schedule_text,
    schedule_type_from_cron,
    sync_jobs,
    sync_memory_jobs,
    validate_schedule,
)
from .sse_utils import jsonable

router = APIRouter(prefix="")


# ---------------------------------------------------------------------------
# 目录约定
# ---------------------------------------------------------------------------
def _workbuddy_dir() -> Path:
    # agent-harness 独立数据目录（不再使用 ~/.workbuddy，那是 WorkBuddy IDE 的数据目录）
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    return DATA_HOME


def _workspace_dir() -> Path:
    p = _workbuddy_dir() / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db_path(name: str) -> Path:
    return _workbuddy_dir() / name


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _safe_path(root: Path, path: str) -> Path:
    """把任意路径解析到 root 下，禁止越界。"""
    if not path:
        return root
    cand = root / path
    try:
        ap = cand.resolve()
    except OSError as e:
        raise ValueError(f"invalid path: {path}: {e}") from e
    root_ap = root.resolve()
    if not (ap == root_ap or str(ap).startswith(str(root_ap) + os.sep)):
        raise ValueError(f"path '{path}' escapes workspace sandbox")
    return ap


@router.get("/workspace/files/{path:path}")
def workspace_file(path: str):
    """直接访问工作区中的文件（用于前端渲染截图/图片/视频）。"""
    root = _workspace_dir()
    target = _safe_path(root, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target)


# ---------------------------------------------------------------------------
# Workspace 文件管理
# ---------------------------------------------------------------------------
class FileWrite(BaseModel):
    path: str
    content: str


class FileRename(BaseModel):
    old: str
    new: str


@router.get("/workspace")
def workspace_list(path: str = ""):
    root = _workspace_dir()
    target = _safe_path(root, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")
    if target.is_file():
        return {
            "type": "file",
            "path": str(target.relative_to(root)),
            "size": target.stat().st_size,
            "modified": int(target.stat().st_mtime),
        }
    entries = []
    for p in sorted(target.iterdir()):
        st = p.stat()
        entries.append({
            "name": p.name,
            "type": "dir" if p.is_dir() else "file",
            "path": str(p.relative_to(root)),
            "size": st.st_size if p.is_file() else 0,
            "modified": int(st.st_mtime),
        })
    return {"type": "dir", "path": str(target.relative_to(root)), "entries": entries}


@router.get("/workspace/read")
def workspace_read(path: str):
    root = _workspace_dir()
    target = _safe_path(root, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = target.read_bytes().hex()[:4096]
    return {"path": str(target.relative_to(root)), "content": text, "size": target.stat().st_size}


@router.post("/workspace")
def workspace_write(payload: FileWrite):
    root = _workspace_dir()
    target = _safe_path(root, payload.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload.content, encoding="utf-8")
    return {"ok": True, "path": str(target.relative_to(root)), "size": target.stat().st_size}


@router.delete("/workspace")
def workspace_delete(path: str):
    root = _workspace_dir()
    target = _safe_path(root, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="path not found")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True}


@router.patch("/workspace")
def workspace_rename(payload: FileRename):
    root = _workspace_dir()
    old = _safe_path(root, payload.old)
    new = _safe_path(root, payload.new)
    if not old.exists():
        raise HTTPException(status_code=404, detail="source not found")
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    return {"ok": True, "path": str(new.relative_to(root))}


# ---------------------------------------------------------------------------
# Core Files（QwenPaw 风格核心文件卡片）
# ---------------------------------------------------------------------------
CORE_FILES = ["AGENTS.md", "SOUL.md", "PROFILE.md", "BOOTSTRAP.md", "HEARTBEAT.md", "MEMORY.md"]


def _core_files_state_path() -> Path:
    return _db_path("core_files_state.json")


def _core_files_state() -> dict:
    p = _core_files_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {name: True for name in CORE_FILES}


def _save_core_files_state(state: dict) -> None:
    _core_files_state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/workspace/core-files")
def core_files_list():
    """返回 QwenPaw 风格 Core Files 列表。"""
    root = _workspace_dir()
    state = _core_files_state()
    result = []
    for name in CORE_FILES:
        target = root / name
        st = target.stat() if target.exists() else None
        result.append({
            "name": name,
            "enabled": state.get(name, True),
            "exists": target.exists(),
            "size": st.st_size if st else 0,
            "modified": int(st.st_mtime) if st else 0,
        })
    return {"files": result}


@router.patch("/workspace/core-files/{name}/enable")
def core_file_toggle(name: str, enabled: bool):
    """启用/禁用 Core File。"""
    if name not in CORE_FILES:
        raise HTTPException(status_code=400, detail="not a core file")
    state = _core_files_state()
    state[name] = enabled
    _save_core_files_state(state)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tools 注册表与状态管理
# ---------------------------------------------------------------------------

def _tools_state_path() -> Path:
    return _db_path("tools_state.json")


def _tools_state() -> dict:
    p = _tools_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"tools": {}}


def _save_tools_state(state: dict) -> None:
    _tools_state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _tool_meta(name: str) -> dict:
    """每个工具的元信息：是否可禁用、是否需要配置、配置字段等。"""
    sensitive = name in ("exec", "write_file", "edit_file")
    async_capable = name in ("exec",)
    config_fields = []
    if name == "exec":
        config_fields = [
            {"name": "allowlist", "label": "Safe command allowlist", "type": "textarea", "placeholder": "One pattern per line, e.g. ls, git status", "required": False, "help": "Commands matching these patterns bypass manual approval."}
        ]
    return {
        "sensitive": sensitive,
        "async_capable": async_capable,
        "requires_config": sensitive,
        "config_fields": config_fields,
        "icon": "",
    }


def _tool_state(name: str, state: dict | None = None) -> dict:
    state = state or _tools_state()
    meta = _tool_meta(name)
    base = {
        "enabled": True,
        "async_execution": False,
        "config_values": {},
    }
    user = state.get("tools", {}).get(name, {})
    merged = {**base, **user}
    merged.setdefault("enabled", True)
    merged.setdefault("async_execution", False)
    merged.setdefault("config_values", {})
    return merged


@router.get("/tools")
def tools_list(user_id: str = Depends(require_auth)):
    """返回当前 harness 已注册工具及其 JSONSchema，附带启用/配置状态。"""
    from .graph_provider import _build_tools

    root = _workspace_dir()
    cm = get_context_manager(user_id)
    tools = _build_tools(str(root), cm, filter_disabled=False)
    state = _tools_state()
    result = []
    for name, tool in tools.items():
        schema = getattr(tool, "args_schema", None)
        meta = _tool_meta(name)
        ts = _tool_state(name, state)
        result.append({
            "name": name,
            "description": getattr(tool, "description", "") or "",
            "schema": schema.model_json_schema() if schema else {"properties": {}, "type": "object"},
            "sensitive": meta["sensitive"],
            "icon": meta["icon"],
            "enabled": ts["enabled"],
            "requires_config": meta["requires_config"],
            "config_fields": meta["config_fields"],
            "config_values": ts.get("config_values", {}),
            "async_execution": ts.get("async_execution", False) and meta["async_capable"],
            "async_capable": meta["async_capable"],
        })
    return result


class ToolToggle(BaseModel):
    enabled: bool


@router.patch("/tools/{name}")
def tool_toggle(name: str, payload: ToolToggle):
    """启用/禁用工具。禁用后下次图重建将不再装载该工具。"""
    from .graph_provider import _graph

    state = _tools_state()
    if name not in state.get("tools", {}):
        state.setdefault("tools", {})[name] = _tool_state(name, state)
    state["tools"][name]["enabled"] = payload.enabled
    _save_tools_state(state)
    # 强制重建图
    from . import graph_provider as gp
    gp._graph = None
    return {"ok": True}


class ToolConfig(BaseModel):
    config: dict[str, Any]


@router.post("/tools/{name}/config")
def tool_config(name: str, payload: ToolConfig):
    """保存工具配置。"""
    state = _tools_state()
    if name not in state.get("tools", {}):
        state.setdefault("tools", {})[name] = _tool_state(name, state)
    state["tools"][name]["config_values"] = payload.config
    _save_tools_state(state)
    return {"ok": True}


class ToolAsync(BaseModel):
    async_execution: bool


@router.patch("/tools/{name}/async")
def tool_async(name: str, payload: ToolAsync):
    """切换工具的异步执行模式。"""
    state = _tools_state()
    if name not in state.get("tools", {}):
        state.setdefault("tools", {})[name] = _tool_state(name, state)
    state["tools"][name]["async_execution"] = payload.async_execution
    _save_tools_state(state)
    # 强制重建图
    from . import graph_provider as gp
    gp._graph = None
    return {"ok": True}


@router.post("/tools/{name}/enable")
def tool_enable_all(name: str):
    """一键启用全部工具。"""
    state = _tools_state()
    for t in state.get("tools", {}):
        state["tools"][t]["enabled"] = True
    _save_tools_state(state)
    from . import graph_provider as gp
    gp._graph = None
    return {"ok": True}


# ---------------------------------------------------------------------------
# Skills 扫描与状态管理
# ---------------------------------------------------------------------------
def _skills_state_path() -> Path:
    return _db_path("skills_state.json")


def _skills_state() -> dict:
    p = _skills_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"skills": {}}


def _save_skills_state(state: dict) -> None:
    _skills_state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _scan_skill_dir(base: Path, scope: str) -> list[dict]:
    out = []
    if not base.exists():
        return out
    for skill_dir in sorted(base.iterdir()):
        if not skill_dir.is_dir():
            continue
        md = skill_dir / "SKILL.md"
        meta = skill_dir / "SKILL.json"
        if not md.exists() and not meta.exists():
            continue
        sid = skill_dir.name
        info = {
            "id": sid,
            "name": sid,
            "description": "",
            "version": "",
            "tags": [],
            "emoji": "",
            "scope": scope,
            "location": str(skill_dir),
        }
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                info.update({k: v for k, v in data.items() if k in ("name", "description", "version", "tags", "emoji")})
                if isinstance(info.get("tags"), str):
                    info["tags"] = [info["tags"]]
            except json.JSONDecodeError:
                pass
        if md.exists():
            text = md.read_text(encoding="utf-8")
            fm = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
            if fm:
                try:
                    import yaml
                    data = yaml.safe_load(fm.group(1)) or {}
                    info.update({k: v for k, v in data.items() if k in ("name", "description", "version", "tags", "emoji")})
                    if isinstance(info.get("tags"), str):
                        info["tags"] = [info["tags"]]
                    info["description"] = info.get("description") or (fm.group(2).strip().split("\n")[0][:120])
                except Exception:
                    info["description"] = text.strip().split("\n")[0][:120]
            else:
                info["description"] = text.strip().split("\n")[0][:120]
            info["has_doc"] = True
        out.append(info)
    return out


@router.get("/skills")
def skills_list():
    """扫描用户级和项目级技能目录，附带启用状态。

    注意：项目级技能目录是仓库内的 ``.workbuddy/skills``（agent-harness 自带资产），
    它位于项目根目录下，并非 WorkBuddy IDE 的全局数据目录 ``~/.workbuddy``，
    因此不参与“数据目录独立化”迁移，保持原路径扫描。
    """
    project = Path.cwd() / ".workbuddy" / "skills"
    user = _workbuddy_dir() / "skills"
    state = _skills_state()
    skills = _scan_skill_dir(user, "system") + _scan_skill_dir(project, "project")
    for s in skills:
        sid = f"{s['scope']}:{s['id']}"
        s["enabled"] = state.get("skills", {}).get(sid, {}).get("enabled", True)
        s["source"] = "builtin" if s["scope"] == "system" else "custom"
    return {"skills": skills}


class SkillToggle(BaseModel):
    enabled: bool


@router.patch("/skills/{scope}/{skill_id}")
def skill_toggle(scope: str, skill_id: str, payload: SkillToggle):
    """启用/禁用指定技能。"""
    state = _skills_state()
    sid = f"{scope}:{skill_id}"
    state.setdefault("skills", {})[sid] = {"enabled": payload.enabled}
    _save_skills_state(state)
    return {"ok": True}


class SkillBatch(BaseModel):
    ids: list[str]
    enabled: bool


@router.post("/skills/batch")
def skill_batch(payload: SkillBatch):
    """批量启用/禁用技能。ids 格式为 scope:id。"""
    state = _skills_state()
    for sid in payload.ids:
        state.setdefault("skills", {})[sid] = {"enabled": payload.enabled}
    _save_skills_state(state)
    return {"ok": True}


# ---------------------------------------------------------------------------
# MCP 管理
# ---------------------------------------------------------------------------
def _mcp_config() -> dict:
    p = _workbuddy_dir() / "mcp.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"mcpServers": {}}


def _save_mcp_config(cfg: dict) -> None:
    p = _workbuddy_dir() / "mcp.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_mcp_transport(raw: Any) -> str:
    if not isinstance(raw, str):
        return "stdio"
    v = raw.strip().lower()
    if v in ("streamablehttp", "streamable_http", "streamable-http", "http"):
        return "streamable_http"
    if v == "sse":
        return "sse"
    return "stdio"


@router.get("/mcp")
def mcp_list():
    """返回标准化后的 MCP 客户端列表。"""
    cfg = _mcp_config()
    servers = cfg.get("mcpServers", {})
    result = []
    for key, s in servers.items():
        transport = _normalize_mcp_transport(s.get("transport") or s.get("type"))
        if not transport and (s.get("url") or s.get("baseUrl")):
            transport = "streamable_http"
        item = {
            "key": key,
            "name": s.get("name") or key,
            "description": s.get("description", ""),
            "enabled": s.get("enabled", True),
            "transport": transport,
            "url": s.get("url") or s.get("baseUrl", ""),
            "headers": s.get("headers", {}),
            "command": s.get("command", ""),
            "args": s.get("args", []),
            "env": s.get("env", {}),
            "cwd": s.get("cwd", ""),
        }
        result.append(item)
    return {"clients": result}


class McpServer(BaseModel):
    key: str
    name: str = ""
    description: str = ""
    transport: str = "stdio"  # stdio | sse | streamable_http
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    enabled: bool = True


@router.post("/mcp")
def mcp_add(server: McpServer):
    cfg = _mcp_config()
    cfg.setdefault("mcpServers", {})
    item: dict[str, Any] = {
        "name": server.name or server.key,
        "description": server.description,
        "enabled": server.enabled,
        "transport": server.transport,
    }
    if server.transport in ("streamable_http", "sse"):
        item["url"] = server.url
    if server.headers:
        item["headers"] = server.headers
    if server.transport == "stdio":
        item["command"] = server.command
        item["args"] = server.args
        item["env"] = server.env
        if server.cwd:
            item["cwd"] = server.cwd
    cfg["mcpServers"][server.key] = item
    _save_mcp_config(cfg)
    return {"ok": True}


@router.post("/mcp/import")
def mcp_import(payload: dict[str, Any]):
    """批量导入 MCP 配置，支持 mcpServers 格式或直接对象。"""
    cfg = _mcp_config()
    cfg.setdefault("mcpServers", {})
    servers = payload.get("mcpServers") or payload
    if not isinstance(servers, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    for key, data in servers.items():
        if not isinstance(data, dict):
            continue
        transport = _normalize_mcp_transport(data.get("transport") or data.get("type"))
        if not transport and (data.get("url") or data.get("baseUrl")):
            transport = "streamable_http"
        item: dict[str, Any] = {
            "name": data.get("name") or key,
            "description": data.get("description", ""),
            "enabled": data.get("enabled", True),
            "transport": transport,
        }
        if transport in ("streamable_http", "sse") or data.get("url") or data.get("baseUrl"):
            item["url"] = data.get("url") or data.get("baseUrl", "")
        if data.get("headers"):
            item["headers"] = data["headers"]
        if transport == "stdio" or data.get("command"):
            item["command"] = data.get("command", "")
            item["args"] = data.get("args", [])
            item["env"] = data.get("env", {})
            if data.get("cwd"):
                item["cwd"] = data["cwd"]
        cfg["mcpServers"][key] = item
    _save_mcp_config(cfg)
    return {"ok": True}


@router.delete("/mcp/{name}")
def mcp_delete(name: str):
    cfg = _mcp_config()
    cfg.get("mcpServers", {}).pop(name, None)
    _save_mcp_config(cfg)
    return {"ok": True}


@router.patch("/mcp/{name}")
def mcp_toggle(name: str, enabled: bool):
    cfg = _mcp_config()
    s = cfg.get("mcpServers", {}).get(name)
    if not s:
        raise HTTPException(status_code=404, detail="mcp server not found")
    s["enabled"] = enabled
    _save_mcp_config(cfg)
    return {"ok": True}


# ---------------------------------------------------------------------------
# ACP (Agent Configuration Protocol) 管理
# ---------------------------------------------------------------------------
BUILTIN_ACP_AGENTS = {
    "opencode": {"name": "OpenCode", "command": "opencode", "args": [], "env": {}, "enabled": False},
    "qwen_code": {"name": "Qwen Code", "command": "qwen-code", "args": [], "env": {}, "enabled": False},
    "claude_code": {"name": "Claude Code", "command": "claude", "args": [], "env": {}, "enabled": False},
    "codex": {"name": "Codex", "command": "codex", "args": [], "env": {}, "enabled": False},
}


def _acp_config_path() -> Path:
    return _db_path("acp.json")


def _acp_config() -> dict:
    p = _acp_config_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"agents": {}}


def _save_acp_config(cfg: dict) -> None:
    _acp_config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _merged_acp_agents() -> dict:
    """合并内置 ACP agent 与用户自定义配置。"""
    configured = _acp_config().get("agents", {})
    merged: dict[str, dict] = {}
    for key, base in BUILTIN_ACP_AGENTS.items():
        user = configured.get(key, {})
        merged[key] = {**base, **{k: v for k, v in user.items() if v or v is False}, "builtin": True}
    for key, user in configured.items():
        if key not in merged:
            merged[key] = {**user, "builtin": False}
    return merged


@router.get("/acp")
def acp_list():
    """返回 ACP agent 配置列表（内置+自定义）。"""
    agents = _merged_acp_agents()
    return {"agents": agents}


class ACPAgent(BaseModel):
    key: str
    name: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    trusted: bool = True
    tool_parse_mode: str = "call_title"
    stdio_buffer_limit_bytes: int = 4 * 1024 * 1024


@router.post("/acp")
def acp_upsert(agent: ACPAgent):
    """创建或更新 ACP agent 配置。"""
    cfg = _acp_config()
    cfg.setdefault("agents", {})
    cfg["agents"][agent.key] = {
        "name": agent.name or agent.key,
        "command": agent.command,
        "args": agent.args,
        "env": agent.env,
        "enabled": agent.enabled,
        "trusted": agent.trusted,
        "tool_parse_mode": agent.tool_parse_mode,
        "stdio_buffer_limit_bytes": agent.stdio_buffer_limit_bytes,
    }
    _save_acp_config(cfg)
    return {"ok": True}


@router.delete("/acp/{key}")
def acp_delete(key: str):
    """删除自定义 ACP agent（内置不可删除，但可重置为默认值）。"""
    cfg = _acp_config()
    agents = cfg.get("agents", {})
    if key in agents:
        del agents[key]
        _save_acp_config(cfg)
    return {"ok": True}


@router.patch("/acp/{key}/enable")
def acp_toggle(key: str, enabled: bool):
    """启用/禁用 ACP agent。"""
    cfg = _acp_config()
    cfg.setdefault("agents", {})
    if key in cfg["agents"]:
        cfg["agents"][key]["enabled"] = enabled
    else:
        base = BUILTIN_ACP_AGENTS.get(key, {"name": key, "command": "", "args": [], "env": {}})
        cfg["agents"][key] = {**base, "enabled": enabled}
    _save_acp_config(cfg)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Agents 管理
# ---------------------------------------------------------------------------
def _agents_db() -> dict:
    p = _db_path("agents.json")
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {
        "agents": [
            {
                "id": "default",
                "name": "Default Agent",
                "emoji": "🤖",
                "system_prompt": "You are a helpful assistant.",
                "default_model": "deepseek-chat",
                "default_mode": "chat",
                "created": int(time.time()),
            }
        ]
    }


def _save_agents_db(db: dict) -> None:
    _db_path("agents.json").write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("/agents")
def agents_list():
    return _agents_db()


class AgentProfile(BaseModel):
    id: str | None = None
    name: str
    emoji: str = "🤖"
    system_prompt: str
    default_model: str = "deepseek-chat"
    default_mode: str = "chat"


@router.post("/agents")
def agents_create(profile: AgentProfile):
    db = _agents_db()
    pid = profile.id or f"agent-{int(time.time() * 1000)}"
    db["agents"] = [a for a in db.get("agents", []) if a["id"] != pid]
    db["agents"].append({
        "id": pid,
        "name": profile.name,
        "emoji": profile.emoji,
        "system_prompt": profile.system_prompt,
        "default_model": profile.default_model,
        "default_mode": profile.default_mode,
        "created": int(time.time()),
    })
    _save_agents_db(db)
    return {"ok": True, "id": pid}


@router.delete("/agents/{pid}")
def agents_delete(pid: str):
    db = _agents_db()
    db["agents"] = [a for a in db.get("agents", []) if a["id"] != pid]
    _save_agents_db(db)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Cron Jobs（已接入 APScheduler 后台调度）
# ---------------------------------------------------------------------------
def _cron_db() -> dict:
    if CRON_DB_PATH.exists():
        return json.loads(CRON_DB_PATH.read_text(encoding="utf-8"))
    return {"jobs": []}


@router.get("/cron")
def cron_list():
    db = _cron_db()
    jobs = db.get("jobs", [])
    for j in jobs:
        jid = j.get("id")
        j["schedule_text"] = schedule_text(j.get("schedule", ""))
        j["schedule_type"] = j.get("schedule_type") or schedule_type_from_cron(j.get("schedule", ""))
        j["timezone"] = j.get("timezone") or "Asia/Shanghai"
        j["task_type"] = j.get("task_type") or "command"
        if jid:
            st = get_job_state(jid)
            j["next_run_at"] = st["next_run_at"]
            j["last_status"] = st["last_status"]
            j["last_run_at"] = st["last_run_at"]
            j["last_error"] = st["last_error"]
    return db


class CronJob(BaseModel):
    id: str | None = None
    name: str
    schedule: str = ""  # cron expression（周期任务用）
    command: str = ""   # 命令文本或消息内容
    enabled: bool = True
    task_type: str = "command"      # agent | command
    prompt: str = ""                # agent 类型任务：到点要执行的任务描述
    timezone: str = "Asia/Shanghai"
    schedule_type: str | None = None  # hourly | daily | weekly | custom | scheduled(once)
    run_at: str | None = None       # 一次性任务（对齐 qwenpaw 的 once）：ISO8601 时间


@router.post("/cron")
def cron_add(job: CronJob):
    db = _cron_db()
    jid = job.id or f"cron-{int(time.time() * 1000)}"
    # 一次性任务用 run_at；周期任务用 schedule
    if job.run_at:
        schedule_type = job.schedule_type or "scheduled"
    else:
        schedule_type = job.schedule_type or schedule_type_from_cron(job.schedule)
    db["jobs"] = [j for j in db.get("jobs", []) if j["id"] != jid]
    db["jobs"].append({
        "id": jid,
        "name": job.name,
        "schedule": job.schedule,
        "command": job.command,
        "enabled": job.enabled,
        "task_type": job.task_type,
        "prompt": job.prompt,
        "timezone": job.timezone,
        "schedule_type": schedule_type,
        "run_at": job.run_at,
        "created": int(time.time()),
    })
    CRON_DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_jobs()
    return {"ok": True, "id": jid}


@router.delete("/cron/{jid}")
def cron_delete(jid: str):
    db = _cron_db()
    db["jobs"] = [j for j in db.get("jobs", []) if j["id"] != jid]
    CRON_DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_jobs()
    return {"ok": True}


@router.patch("/cron/{jid}")
def cron_toggle(jid: str, enabled: bool):
    db = _cron_db()
    for j in db.get("jobs", []):
        if j["id"] == jid:
            j["enabled"] = enabled
            break
    CRON_DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    sync_jobs()
    return {"ok": True}


@router.post("/cron/{jid}/run")
def cron_run_now(jid: str):
    return run_job_now(jid)


@router.get("/cron/history")
def cron_history_all():
    return get_history()


@router.get("/cron/{jid}/history")
def cron_history_job(jid: str):
    return get_history(jid)


@router.get("/cron/{jid}/state")
def cron_state(jid: str):
    return get_job_state(jid)


@router.get("/cron/validate")
def cron_validate(schedule: str):
    return validate_schedule(schedule)


# ---------------------------------------------------------------------------
# Heartbeat 健康
# ---------------------------------------------------------------------------
@router.get("/heartbeat")
def heartbeat():
    import psutil
    mem = psutil.Process().memory_info()
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - psutil.Process().create_time()),
        "memory": {"rss_mb": int(mem.rss / 1024 / 1024), "vms_mb": int(mem.vms / 1024 / 1024)},
        "workspace": str(_workspace_dir()),
    }


# ---------------------------------------------------------------------------
# Sessions 会话列表（从内存 checkpointer 读取 threads）
# ---------------------------------------------------------------------------
@router.get("/sessions")
def sessions_list(user_id: str = Depends(require_auth)):
    """返回当前用户最近活跃的会话列表（来自持久化的会话索引 sessions.json）。

    多用户隔离：只返回归属当前 user_id 的会话（每个会话索引写入时记录 user_id）。
    历史真相源是会话索引文件（开对话时由 _touch_session 维护），不再从 checkpointer
    枚举——checkpoint 已落盘到 DATA_HOME/checkpoints.sqlite（~/.agent-harness），由 get_shared_checkpointer 管理。
    """
    threads = []
    idx = _db_path("sessions.json")
    if idx.exists():
        data = json.loads(idx.read_text(encoding="utf-8"))
        for tid, meta in data.get("threads", {}).items():
            if meta.get("user_id", "default") != user_id:
                continue
            threads.append({"id": tid, "updated_at": meta.get("updated_at", 0), "title": meta.get("title", tid)})
    threads.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"sessions": threads}


def _touch_session(tid: str, title: str = "", user_id: str = "default") -> None:
    """维护会话索引（按用户隔离）。会话索引键为原始 tid，并附加 user_id 字段供列表过滤。"""
    p = _db_path("sessions.json")
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"threads": {}}
    data["threads"][tid] = {
        "updated_at": int(time.time()),
        "title": title or data["threads"].get(tid, {}).get("title", tid),
        "user_id": user_id,
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@router.delete("/sessions/{tid}")
async def sessions_delete(tid: str, user_id: str = Depends(require_auth)):
    """删除某会话：从会话索引剔除，并清理复合键对应的 checkpointer 落盘状态。"""
    p = _db_path("sessions.json")
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        data["threads"].pop(tid, None)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 清理 checkpointer（落盘 SQLite）中的 thread 状态，避免脏状态复活。
    # 用复合键 user:thread —— 与聊天路径写入的 checkpoint 键一致，否则删不掉。
    from .graph_provider import get_shared_checkpointer

    cp = get_shared_checkpointer()
    ck = f"{user_id}:{tid}"
    try:
        await cp.adelete_thread(ck)
    except Exception:  # noqa: BLE001
        logger.warning("sessions_delete clear checkpoint failed ck=%s", ck)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Memory Manager（QwenPaw ReMeLight 等效）端点
# ---------------------------------------------------------------------------
class MemoryConfigPayload(BaseModel):
    config: dict = Field(default_factory=dict)


class MemorySearchPayload(BaseModel):
    query: str = ""
    max_results: int = 5


class MemoryAutoPayload(BaseModel):
    messages: list = Field(default_factory=list)


@router.get("/memory/config")
def memory_config_get(user_id: str = Depends(require_auth)):
    """返回当前 Memory Manager 配置（runtime.json running 段单一事实源，引擎级全局共享）。"""
    cfg = memory_mod.runtime_memory_config()
    return cfg.model_dump()


@router.put("/memory/config")
def memory_config_put(payload: MemoryConfigPayload, user_id: str = Depends(require_auth)):
    """保存 Memory Manager 配置（写入 runtime.json running 段）并（重新）调度 dream cron。

    记忆 vault 与镜像配置 memory_config.json 按用户隔离（user_id）。
    """
    cfg = memory_mod.MemoryManagerConfig(**payload.config)
    # 主源：runtime.json 的 running 段（与 QwenPaw 对齐，UI 修改真实生效）
    try:
        from . import config as runtime_config

        runtime_config.save_config(
            {
                "running": {
                    "memory_manager_backend": cfg.backend,
                    "reme_light_memory_config": cfg.reme_light_memory_config.model_dump(),
                }
            }
        )
    except Exception as e:  # noqa: BLE001
        print(f"[memory] runtime config save failed: {e}")
    # 镜像到 memory_config.json（向后兼容旧代码/手动读取，按用户隔离）
    memory_mod.save_config(cfg, user_id)
    # 重置单例，使下次访问用新配置重建
    memory_mod.reset_memory_manager()
    # 按 dream_cron 重新调度
    try:
        sync_memory_jobs()
    except Exception as e:  # noqa: BLE001
        print(f"[memory] sync_memory_jobs failed: {e}")
    return memory_mod.runtime_memory_config().model_dump()


@router.get("/memory/stats")
def memory_stats_get(user_id: str = Depends(require_auth)):
    """返回记忆 vault 统计（笔记数、索引块数、嵌入是否启用、上次 dream）。"""
    mm = memory_mod.get_memory_manager(user_id)
    if mm is None:
        return {"enabled": False, "backend": memory_mod.runtime_memory_config().backend}
    return {"enabled": True, **mm.stats()}


@router.post("/memory/search")
def memory_search_ep(payload: MemorySearchPayload, user_id: str = Depends(require_auth)):
    """调试用：直接对记忆 vault 做混合检索（按用户隔离）。"""
    mm = memory_mod.get_memory_manager(user_id)
    if mm is None:
        return {"enabled": False, "results": []}
    results = mm.vault.hybrid_search(payload.query, payload.max_results)
    return {"enabled": True, "query": payload.query, "results": results}


@router.post("/memory/dream")
def memory_dream_ep(user_id: str = Depends(require_auth)):
    """手动触发一次 dream（整合/去重记忆，按用户隔离）。"""
    mm = memory_mod.get_memory_manager(user_id)
    if mm is None:
        return {"enabled": False, "ok": False, "error": "memory disabled"}
    return mm.dream()


@router.post("/memory/reindex")
def memory_reindex_ep(user_id: str = Depends(require_auth)):
    """重建记忆索引（扫描 vault 并重新嵌入，按用户隔离）。"""
    mm = memory_mod.get_memory_manager(user_id)
    if mm is None:
        return {"enabled": False, "ok": False, "error": "memory disabled"}
    n = mm.vault.rebuild_index()
    return {"enabled": True, "ok": True, "chunks": n}


@router.post("/memory/auto-memory")
def memory_auto_ep(payload: MemoryAutoPayload, user_id: str = Depends(require_auth)):
    """手动把一段对话写入每日笔记（auto_memory 的显式触发，按用户隔离）。"""
    mm = memory_mod.get_memory_manager(user_id)
    if mm is None:
        return {"enabled": False, "ok": False, "error": "memory disabled"}
    before = mm.vault.stats().get("notes", 0)
    mm.auto_memory(payload.messages, force=True)
    after = mm.vault.stats().get("notes", 0)
    return {"enabled": True, "ok": True, "notes_before": before, "notes_after": after}


# ---------------------------------------------------------------------------
# Context Manager（QwenPaw LightContextCard 等效）端点
# ---------------------------------------------------------------------------
class ContextConfigPayload(BaseModel):
    config: dict = Field(default_factory=dict)


@router.get("/context/config")
def context_config_get(user_id: str = Depends(require_auth)):
    """返回当前 Context Manager 配置（按用户隔离，镜像 QwenPaw LightContextCard 配置面）。"""
    return cc_mod.load_config(user_id).model_dump()


@router.put("/context/config")
def context_config_put(payload: ContextConfigPayload, user_id: str = Depends(require_auth)):
    """保存 Context Manager 配置并重置单例，使下次访问用新配置重建（按用户隔离）。"""
    cfg = cc_mod.ContextManagerConfig(**payload.config)
    cc_mod.save_config(cfg, user_id)
    reset_context_manager()
    return cfg.model_dump()


@router.get("/context/threads")
def context_threads_get(user_id: str = Depends(require_auth)):
    """列出当前用户进程内出现过 turn 的所有 thread_id（复合键）。"""
    cm = get_context_manager(user_id)
    return {"threads": cm.thread_ids()}


@router.get("/context/inspect")
def context_inspect_get(thread_id: str = "", user_id: str = Depends(require_auth)):
    """检视某 thread 的折叠情况与存储全文（无 thread_id 时返回配置摘要 + thread 列表）。

    存储键为复合键 user:thread，故检视某具体 thread 时按当前用户拼出复合键。
    """
    cm = get_context_manager(user_id)
    if not thread_id:
        return {
            "budget_tokens": cm.budget_tokens,
            "strip_media": cm.strip_media,
            "max_tool_result_chars": cm._max_tool_result_chars,
            "recall_enabled": cm.allow_unsandboxed_recall,
            "threads": cm.thread_ids(),
        }
    return cm.inspect(f"{user_id}:{thread_id}")


@router.post("/context/clear")
def context_clear_post(thread_id: str = "", user_id: str = Depends(require_auth)):
    """清空指定 thread 的存储 turn（折叠区原文一并丢弃，无法再 recall，按用户隔离）。"""
    if not thread_id:
        return {"ok": False, "error": "thread_id required"}
    cm = get_context_manager(user_id)
    n = cm.clear(f"{user_id}:{thread_id}")
    return {"ok": True, "thread_id": thread_id, "removed": n}


# ---------------------------------------------------------------------------
# Core Files (QwenPaw layer-1: AGENTS/SOUL/PROFILE/BOOTSTRAP/HEARTBEAT/MEMORY)
# ---------------------------------------------------------------------------
class CoreFilesEnabledPayload(BaseModel):
    files: list[str] = Field(default_factory=list)


class CoreFileContentPayload(BaseModel):
    content: str = Field(default="")


@router.get("/core-files")
def core_files_list():
    """List the six core markdown files with metadata."""
    return get_core_files_manager().list_files()


@router.get("/core-files/enabled")
def core_files_enabled_get():
    """Get the currently enabled core files (ordering matters)."""
    return get_core_files_manager().get_enabled()


@router.put("/core-files/enabled")
def core_files_enabled_put(payload: CoreFilesEnabledPayload):
    """Set which core files are injected into the system prompt, in order."""
    return get_core_files_manager().set_enabled(payload.files)


@router.get("/core-files/{name}")
def core_files_read(name: str):
    """Read a core markdown file by name."""
    try:
        return {"filename": name, "content": get_core_files_manager().read_file(name)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.put("/core-files/{name}")
def core_files_write(name: str, payload: CoreFileContentPayload):
    """Write a core markdown file by name."""
    try:
        return get_core_files_manager().write_file(name, payload.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/core-files/initialize")
def core_files_initialize(body: dict = None):
    """Copy bundled templates into the workspace (skip existing by default)."""
    language = (body or {}).get("language", "zh")
    return get_core_files_manager().initialize_templates(language)
