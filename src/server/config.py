"""运行时配置（热加载）—— 前端设置面板 POST /config 即写即生效，无需重启。

落盘位置：<repo>/config/runtime.json。
加密 at rest：若设置了 CONFIG_SECRET 环境变量，或同目录 .config_secret 存在，
则用 Fernet 对称加密整份 JSON 文件（密钥文件 chmod 600，并应加入 .gitignore）。
无密钥时退化为明文（仅建议本地开发，会在日志打印警告）。

配置字段：
- llm.api_key / llm.base_url / llm.model   —— 真实 LLM（DeepSeek 等）接入信息
- service.api_key                          —— 服务级访问令牌（启用鉴权时前端须携带）
- security.enable_auth                     —— 是否开启服务级 Bearer/x-api-key 鉴权
- rate_limit                              —— 单 IP 每分钟请求上限（run/stream 端点），0=关闭
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def _repo_root() -> str:
    # src/server/config.py -> parents[2] == agent-harness
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


CONFIG_DIR = os.environ.get("AGENT_CONFIG_DIR", os.path.join(_repo_root(), "config"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "runtime.json")
SECRET_PATH = os.path.join(CONFIG_DIR, ".config_secret")

# ---------------------------------------------------------------------------
# 独立数据目录
# ---------------------------------------------------------------------------
# agent-harness 的全部运行时数据不再写入 ~/.workbuddy（那是 WorkBuddy IDE 的数据目录，
# 内含 IDE 的 skills/binaries/traces/logs 等），统一落到独立的 ~/.agent-harness。
# 可用环境变量 AGENT_DATA_HOME 覆盖（容器/多实例场景）。
DATA_HOME = Path(os.environ.get("AGENT_DATA_HOME", Path.home() / ".agent-harness"))

# 仅迁移这些 agent-harness 自有文件/目录；绝不移动 IDE 的 skills/binaries/traces/logs/memory 等。
# 注意：~/.workbuddy/skills 是 WorkBuddy IDE 的技能目录，不可迁移，agent-harness 的用户级技能已改指
#       DATA_HOME/skills（见 plugins.py），两者彻底解耦。
_MIGRATE_FILES = [
    "cron.json", "cron_history.json", "mcp.json", "mcp-approvals.json",
    "agents.json", "skills_state.json", "core_files_state.json", "tools_state.json",
    "memory_config.json", "context_config.json", "core_files_config.json",
    "workspace-state.json", "sessions.json", "token_usage.json", "security.json",
    "voice.json", "envs.json", "approvals.sqlite", "checkpoints.sqlite",
]
_MIGRATE_DIRS = ["workspace", "backups", "sessions"]


def migrate_from_workbuddy() -> None:
    """首次启动把散落在 ~/.workbuddy 的 agent-harness 数据挪到独立目录 DATA_HOME。

    仅迁移上面白名单中的 agent-harness 自有文件，绝不触碰 WorkBuddy IDE 的数据
    （skills/binaries/traces/logs/memory/workbuddy.db 等）。幂等：已存在则跳过。
    """
    legacy = Path.home() / ".workbuddy"
    if not legacy.exists():
        return
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    for name in _MIGRATE_FILES:
        for suffix in ("", "-wal", "-shm"):
            src = legacy / f"{name}{suffix}"
            if src.exists():
                dst = DATA_HOME / f"{name}{suffix}"
                if not dst.exists():
                    shutil.move(str(src), str(dst))
    for d in _MIGRATE_DIRS:
        src = legacy / d
        if src.exists() and src.is_dir():
            dst = DATA_HOME / d
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                target = dst / item.name
                if not target.exists():
                    shutil.move(str(item), str(target))
            try:
                if not any(src.iterdir()):
                    src.rmdir()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 配置数据结构
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 配置数据结构
# ---------------------------------------------------------------------------
def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _default_llm() -> Dict[str, Any]:
    return {"provider": "", "api_key": "", "base_url": "", "model": "deepseek-chat", "reasoning": False}


def _default_running() -> Dict[str, Any]:
    """对齐 QwenPaw AgentsRunningConfig 的默认结构（字段名/嵌套保持一致）。"""
    return {
        "max_iters": 100,
        "loop": {
            "iteration": {"enabled": True, "max_iterations": None},
            "doom_loop": {
                "enabled": True,
                "window_size": 3,
                "similarity_threshold": 1.0,
                "stages": [
                    {
                        "after": 3,
                        "action": "modify_prompt",
                        "prompt": (
                            "[WARNING] Repetitive pattern detected. You are "
                            "repeating similar actions without progress. Try a "
                            "completely different approach."
                        ),
                    },
                    {
                        "after": 6,
                        "action": "stop",
                        "prompt": "Doom loop: agent stuck after 6 consecutive repetitions",
                    },
                ],
                "in_loop_modes": False,
            },
            "rubric": {
                "enabled": False,
                "prompt": (
                    "You did not call any tool in the last turn. If the task is "
                    "truly complete, confirm it. Otherwise, continue working with "
                    "tool calls."
                ),
                "max_interventions": 1,
                "in_loop_modes": False,
            },
        },
        "llm_retry_enabled": True,
        "llm_max_retries": 3,
        "llm_backoff_base": 1.0,
        "llm_backoff_cap": 10.0,
        "llm_max_concurrent": 4,
        "llm_max_qpm": 0,
        "shell_command_timeout": 60.0,
        "shell_command_executable": "",
        "max_input_length": 131072,
        "context_manager_backend": "light",
        "light_context_config": {
            "strategy": "scroll",
            "dialog_path": "dialog",
            "token_count_estimate_divisor": 4,
            "context_compact_config": {
                "enabled": True,
                "compact_threshold_ratio": 0.8,
                "reserve_threshold_ratio": 0.1,
            },
            "tool_result_pruning_config": {
                "enabled": True,
                "pruning_recent_n": 2,
                "pruning_old_msg_max_bytes": 3000,
                "pruning_recent_msg_max_bytes": 50000,
                "execution_layer_max_bytes": 50000,
                "offload_retention_days": 5,
                "tool_results_cache": "tool_results",
            },
            "scroll_config": {
                "db_filename": "history.db",
                "tool_output_token_cap": 3000,
                "repl_timeout_s": 300,
                "history_retention_days": 30,
            },
        },
        "auto_title_config": {"enabled": True, "timeout_seconds": 30.0},
        "memory_manager_backend": "remelight",
        "reme_light_memory_config": {
            "summarize_when_compact": True,
            "auto_memory_interval": 5,
            "dream_cron": "0 23 * * *",
            "rebuild_memory_index_on_start": False,
            "enable_search_raw_log": False,
            "auto_memory_search_config": {"enabled": True, "max_results": 5},
            "embedding_model_config": {
                "backend": "openai",
                "base_url": "",
                "api_key": "",
                "model_name": "",
                "dimensions": 1024,
                "enable_cache": True,
                "max_cache_size": 3000,
                "max_input_length": 8192,
                "max_batch_size": 10,
            },
        },
        "daily_memory_dir": "memory",
        # agent-harness 自定义：QwenPaw 看门狗无对应数值，但 reaper 需要
        "approval_timeout_seconds": 300,
    }


def _mask_secrets(obj: Any) -> None:
    """原地递归脱敏：key 为 api_key 或以 _token 结尾且为字符串的值置为 ****。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _mask_secrets(v)
            elif isinstance(k, str) and ("api_key" in k or k.endswith("_token")) and isinstance(v, str) and v:
                obj[k] = "****"
    elif isinstance(obj, list):
        for item in obj:
            _mask_secrets(item)


def _migrate_approval_level(cfg_dict: Dict[str, Any]) -> str:
    """从新/旧配置推导 approval_level（STRICT/SMART/AUTO/OFF）。"""
    if isinstance(cfg_dict.get("approval_level"), str) and cfg_dict["approval_level"]:
        return cfg_dict["approval_level"]
    sec = cfg_dict.get("tool_execution_security") or {}
    agent = cfg_dict.get("agent") or {}
    if sec.get("enable_approval") is False:
        return "OFF"
    if sec.get("trust_mode") or agent.get("trust_mode"):
        return "OFF"
    if sec.get("auto_approve_safe_exec", agent.get("auto_approve_safe_exec", True)):
        return "SMART"
    return "AUTO"


def _migrate_running(cfg_dict: Dict[str, Any]) -> Dict[str, Any]:
    """构建 running：优先用已保存的 running（深度合并默认值），再叠加旧分组迁移。"""
    saved = cfg_dict.get("running") or {}
    r = _deep_merge(_default_running(), saved)
    ra = cfg_dict.get("react_agent") or {}
    lr = cfg_dict.get("llm_retry") or {}
    tes = cfg_dict.get("tool_execution_security") or {}
    ltm = cfg_dict.get("long_term_memory") or {}

    if ra.get("max_iterations") is not None:
        r["loop"]["iteration"]["max_iterations"] = ra["max_iterations"]
    if ra.get("shell_command_timeout") is not None:
        r["shell_command_timeout"] = ra["shell_command_timeout"]
    if ra.get("max_context_length") is not None:
        r["max_input_length"] = ra["max_context_length"]
    if ra.get("context_manager_backend"):
        r["context_manager_backend"] = ra["context_manager_backend"]
    if ra.get("memory_manager_backend"):
        r["memory_manager_backend"] = ra["memory_manager_backend"]
    if lr:
        r["llm_retry_enabled"] = bool(lr.get("enabled", r["llm_retry_enabled"]))
        r["llm_max_retries"] = lr.get("max_retries", r["llm_max_retries"])
        r["llm_backoff_base"] = lr.get("backoff_seconds", r["llm_backoff_base"])
    if tes.get("approval_timeout_seconds") is not None:
        r["approval_timeout_seconds"] = tes["approval_timeout_seconds"]
    if ltm.get("enabled") is False:
        r["memory_manager_backend"] = "none"
    return r


def approval_settings(level: str) -> tuple[bool, bool]:
    """QwenPaw approval_level -> (trust_mode, auto_approve_safe_exec)。

    OFF=全部自动放行 / SMART=只读自动放行 / AUTO=同上（agent-harness 粒度下
    与 SMART 等价）/ STRICT=全部人工审批。
    """
    level = (level or "AUTO").upper()
    if level == "OFF":
        return True, True
    if level in ("SMART", "AUTO"):
        return False, True
    return False, False  # STRICT


@dataclass
class RuntimeConfig:
    # ===== 基础设施（agent-harness 自有，非 QwenPaw agent-config） =====
    llm: Dict[str, Any] = field(default_factory=lambda: {
        "provider": "",
        "api_key": "",
        "base_url": "",
        "model": "deepseek-chat",
        "reasoning": False,
    })
    service: Dict[str, Any] = field(default_factory=lambda: {"api_key": ""})
    security: Dict[str, Any] = field(default_factory=lambda: {"enable_auth": False, "users": []})
    rate_limit: int = 60  # 每分钟每 IP 上限；0 = 关闭（HTTP 端点限流）
    rate_limiter: Dict[str, Any] = field(default_factory=lambda: {
        "enabled": False,
        "requests_per_minute": 60,
    })
    version: int = 0

    # ===== QwenPaw 真实 agent-config 对齐（字段名/嵌套对齐 qwenpaw.config） =====
    language: str = "zh"  # AgentProfileConfig.language
    approval_level: str = "AUTO"  # AgentProfileConfig.approval_level: STRICT/SMART/AUTO/OFF
    plan: Dict[str, Any] = field(default_factory=lambda: {"enabled": False})  # PlanConfig
    running: Dict[str, Any] = field(default_factory=_default_running)  # AgentsRunningConfig

    def masked(self) -> Dict[str, Any]:
        """返回脱敏副本，供 GET /config 回显（不泄露密钥明文）。"""
        d = asdict(self)
        _mask_secrets(d)
        return d


def _normalize(cfg_dict: Dict[str, Any]) -> Dict[str, Any]:
    """补齐缺省字段 + 旧版 runtime.json 向后兼容迁移。"""
    cfg_dict = cfg_dict or {}
    security = {**{"enable_auth": False, "users": []}, **(cfg_dict.get("security") or {})}
    service = {**{"api_key": ""}, **(cfg_dict.get("service") or {})}
    # 关闭鉴权时自动清空服务令牌，避免前后端状态不一致导致保存死锁
    if not security.get("enable_auth"):
        service["api_key"] = ""
    return {
        "version": cfg_dict.get("version", 0),
        "rate_limit": cfg_dict.get("rate_limit", 60),
        "llm": {**_default_llm(), **(cfg_dict.get("llm") or {})},
        "service": service,
        "security": security,
        "rate_limiter": {
            **{"enabled": False, "requests_per_minute": 60},
            **(cfg_dict.get("rate_limiter") or {}),
        },
        "language": cfg_dict.get("language", "zh"),
        "approval_level": _migrate_approval_level(cfg_dict),
        "plan": {**{"enabled": False}, **(cfg_dict.get("plan") or {})},
        "running": _migrate_running(cfg_dict),
    }


# ---------------------------------------------------------------------------
# 加密（Fernet）
# ---------------------------------------------------------------------------
def _load_or_create_secret() -> Optional[bytes]:
    """返回 Fernet key；若无密钥源则 None（明文模式）。"""
    env = os.environ.get("CONFIG_SECRET")
    if env:
        try:
            return env.encode() if len(env) >= 16 else Fernet.generate_key()
        except Exception:
            pass
    if os.path.exists(SECRET_PATH):
        try:
            with open(SECRET_PATH, "rb") as f:
                return f.read().strip()
        except Exception:
            pass
    # 首次运行：生成一个密钥文件（仅本机可读写）
    try:
        key = Fernet.generate_key()
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SECRET_PATH, "wb") as f:
            f.write(key)
        os.chmod(SECRET_PATH, 0o600)
        return key
    except Exception:
        return None


_FERNET: Optional[Fernet] = None
try:
    _raw = _load_or_create_secret()
    if _raw:
        _FERNET = Fernet(_raw)
except Exception:
    _FERNET = None

if _FERNET is None:
    import logging
    logging.getLogger("config").warning(
        "CONFIG_SECRET 未配置，runtime.json 将以明文保存（仅本地开发）。生产环境请设置 CONFIG_SECRET。"
    )


def _encrypt(plain: str) -> Dict[str, Any]:
    return {"_encrypted": True, "data": _FERNET.encrypt(plain.encode()).decode()}


def _decrypt(blob: Any) -> Dict[str, Any]:
    if isinstance(blob, dict) and blob.get("_encrypted"):
        raw = _FERNET.decrypt(blob["data"].encode()).decode()
        return json.loads(raw)
    return blob


# ---------------------------------------------------------------------------
# 单例 + 热加载
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_instance: Optional[RuntimeConfig] = None


def _load_from_disk() -> RuntimeConfig:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                blob = json.load(f)
            data = _decrypt(blob)
            cfg = RuntimeConfig(**_normalize(data))
            return cfg
        except Exception:
            pass
    return RuntimeConfig()


def get_config() -> RuntimeConfig:
    """返回当前配置（单例，进程内共享）。"""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = _load_from_disk()
    return _instance


def save_config(partial: Dict[str, Any]) -> RuntimeConfig:
    """合并并持久化配置，版本号 +1。返回最新配置。"""
    global _instance
    with _lock:
        cur = asdict(get_config())
        merged = _deep_merge(cur, partial)
        # 归一化：清理空字符串 api_key 时仍保留字段
        cfg = RuntimeConfig(**_normalize(merged))
        cfg.version = cur.get("version", 0) + 1
        os.makedirs(CONFIG_DIR, exist_ok=True)
        payload = asdict(cfg)
        if _FERNET is not None:
            blob = _encrypt(json.dumps(payload, ensure_ascii=False))
        else:
            blob = payload
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False, indent=2)
        # 配置落盘权限收紧
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except Exception:
            pass
        _instance = cfg
    return cfg


def reload_config() -> RuntimeConfig:
    """强制从磁盘重新加载（一般无需调用，save_config 已更新单例）。"""
    global _instance
    with _lock:
        _instance = _load_from_disk()
    return _instance
