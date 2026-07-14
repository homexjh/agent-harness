"""图供给：构建编译好的 harness 图（单例），用于 HTTP 服务。

模型策略：
- 设置了 DEEPSEEK_API_KEY -> 用 make_deepseek_model()（deepseek-v4-pro，streaming=True）。
- 否则 -> DemoAgentModel（确定性、无需 key，可演示流式 + 工具 + 人工裁决），
  保证前端在没有 key 时也能完整跑通产品形态。

工具：calculator + write_file（PolicyGuardedTool 包裹）+ recall（ContextManager 召回）。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import platform
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from typing import Any, AsyncIterator

from ..harness.context.manager import ContextManager
from ..harness.context.recall_tool import make_recall_tool
from .memory import get_memory_manager
from .core_files import get_core_files_manager
from ..harness.agentmode import get_mode, resolve_gates
from ..harness.graph import build_graph
from ..harness.security.guard import (
    ToolGuardEngine,
    FilePathGuardian,
    RuleBasedGuardian,
    ShellEvasionGuardian,
)
from ..harness.security.guarded_tool import make_guarded_tools
from ..harness.security.approval import ApprovalGate
from ..harness.stability.observability import Metrics
from .scheduler import schedule_one_time, sync_jobs


_ANSISTANT_MSG_PREFIX = "agent-harness exec"

# ANSI 转义码剥离：exec 子进程（如 curl wttr.in）输出的颜色码 \033[38;5;111m 等
# 会让前端 UI 显示为乱码（"038;5;111m"），且干扰 LLM 理解工具输出。统一剥离。
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^[\]]")


def _strip_ansi(text: str) -> str:
    """剥离 ANSI 转义码（颜色、光标控制、OSC 序列等）。"""
    return _ANSI_RE.sub("", text) if text else text


_ASSISTANT_ID = "agent-harness"


def get_assistant_id() -> str:
    return _ASSISTANT_ID


def _workspace_dir() -> Path:
    """与 plugins.py 一致的工作区根目录：~/.workbuddy/workspace。"""
    p = Path.home() / ".workbuddy" / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


class DemoAgentModel(BaseChatModel):
    """无 key 时的确定性演示模型：能走通 工具调用 + 最终收敛 + 触发人工裁决。

    继承 BaseChatModel，让 LangGraph 把它当作真正的 chat model 处理，
    从而在 stream_mode="messages" 下逐 token 输出，提升前端感知速度。
    """

    model: str = Field(default="demo", description="模型标识")
    chunk_size: int = Field(default=4, description="流式分片大小")

    def _last_human(self, messages):
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                return m.content or ""
        return ""

    def _last_tool(self, messages):
        for m in reversed(messages):
            if isinstance(m, ToolMessage):
                return m
        return None

    def _with_reasoning(self, msg: AIMessage, reasoning: str) -> AIMessage:
        msg.additional_kwargs = msg.additional_kwargs or {}
        msg.additional_kwargs["reasoning_content"] = reasoning
        return msg

    def _pick_response(self, messages) -> AIMessage:
        text = self._last_human(messages)
        last_tool = self._last_tool(messages)

        if last_tool is not None:
            content = str(last_tool.content or "")
            # 尝试解析工具返回的 JSON
            data: dict = {}
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                pass
            tool = data.get("tool") or ""

            # screenshot 返回 path -> 下一步 view_image
            if data.get("ok") and data.get("path") and tool == "desktop_screenshot":
                return self._with_reasoning(
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "view_image",
                                "args": {"image_path": data["path"]},
                                "id": "call_view_1",
                            }
                        ],
                    ),
                    "截图已保存到工作区，我需要查看图片内容才能回答。",
                )

            # view_image 返回 image_url -> 演示最终回答
            if data.get("ok") and data.get("image_url") and tool == "view_image":
                return self._with_reasoning(
                    AIMessage(
                        content=(
                            "我已经截图并查看了当前桌面。画面里是默认的系统桌面背景，"
                            "没有额外的文件或应用窗口遮挡（演示模式：未配置真实 API Key，"
                            "配置多模态模型后我会真正『看懂』截图内容并回答细节）。"
                        )
                    ),
                    "我已经查看完图片，现在给出总结。",
                )

            # 时间类
            if tool == "get_current_time":
                try:
                    local = data.get("local", "")
                    return self._with_reasoning(
                        AIMessage(content=f"现在是（本地时间）：{local}。"),
                        "已获取到当前时间，直接回答。",
                    )
                except Exception:
                    return self._with_reasoning(
                        AIMessage(content="已获取时间信息。"), "已获取时间信息。"
                    )

            # 目录列举
            if tool == "list_dir":
                return self._with_reasoning(
                    AIMessage(
                        content=(
                            "工作区当前内容如下：\n"
                            f"{content}\n\n"
                            "（演示模式：以上为工作区文件清单。）"
                        )
                    ),
                    "目录已列出，整理后回复。",
                )

            # 命令执行
            if tool == "exec":
                return self._with_reasoning(
                    AIMessage(
                        content=(
                            "命令已执行，输出如下：\n"
                            f"```\n{content}\n```"
                        )
                    ),
                    "命令执行完毕，整理输出。",
                )

            # 计算
            if tool == "calculator":
                return self._with_reasoning(
                    AIMessage(content=f"计算结果是：{content}。"),
                    "计算完成，给出结果。",
                )

            # 读文件
            if tool == "read_file":
                return self._with_reasoning(
                    AIMessage(
                        content=(
                            "文件内容如下：\n"
                            f"```\n{content}\n```"
                        )
                    ),
                    "文件已读取，展示内容。",
                )

            # 写/改文件
            if tool in ("write_file", "edit_file"):
                return self._with_reasoning(
                    AIMessage(content=f"已处理文件：{content}。"),
                    "文件已写入/修改。",
                )

            # 通用工具后收尾
            return self._with_reasoning(
                AIMessage(content=f"工具已执行，结果：\n{content[:800]}"),
                "工具已执行，整理结果。",
            )

        # ── 首轮：按意图选择工具 ──
        s = str(text)
        low = s.lower()

        # 桌面 / 屏幕 / 截图 / 视觉
        if re.search(r"桌面|屏幕|截图|screenshot|看看.*(桌面|屏幕)|桌面.*什么|屏幕.*什么|窗口|网页|页面", s, re.IGNORECASE):
            return self._with_reasoning(
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "desktop_screenshot", "args": {}, "id": "call_screenshot_1"}
                    ],
                ),
                "用户想了解桌面/屏幕内容，我应该先截图，再查看图片。",
            )

        # 时间
        if re.search(r"现在几点|现在是什么时间|今天日期|今天是几号|现在时间|当前时间|date|时间", s, re.IGNORECASE):
            return self._with_reasoning(
                AIMessage(
                    content="",
                    tool_calls=[{"name": "get_current_time", "args": {}, "id": "call_time_1"}],
                ),
                "用户询问当前时间，调用 get_current_time。",
            )

        # 列目录 / 文件清单
        if re.search(r"有什么文件|列目录|目录|工作区|文件清单|ls|看看.*文件|file list", s, re.IGNORECASE):
            return self._with_reasoning(
                AIMessage(
                    content="",
                    tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": "call_ls_1"}],
                ),
                "用户想查看工作区文件，先列出目录。",
            )

        # 计算
        m = re.search(r"计算|算一下|等于多少|=|\b(\d+)\s*[\+\-\*/]\s*(\d+)\b", s)
        if m and re.search(r"计算|算一下|等于多少", s, re.IGNORECASE):
            # 尝试提取两个数字
            nums = re.findall(r"\d+", s)
            if len(nums) >= 2:
                a, b = int(nums[0]), int(nums[1])
                return self._with_reasoning(
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "calculator", "args": {"a": a, "b": b}, "id": "call_calc_1"}],
                    ),
                    f"用户要求计算 {a} + {b}，调用 calculator。",
                )

        # 执行命令 / 跑脚本 / 终端（限定明确触发词，避免"运行流畅"等误命中）
        if re.search(r"执行|跑一下|跑个|命令|command|终端|terminal|shell|运行命令|运行脚本|运行.*命令", s, re.IGNORECASE):
            # 从用户话里抽取命令；没有就给一个无害的只读命令
            cmd = re.sub(r"^[请帮我]*(执行|运行|跑一下|命令|终端|terminal|shell)[：:，。\s]*", "", s, flags=re.IGNORECASE).strip()
            if not cmd or len(cmd) > 80:
                cmd = "echo 演示：可执行命令（配置真实模型后可运行任意 shell）"
            return self._with_reasoning(
                AIMessage(
                    content="",
                    tool_calls=[{"name": "exec", "args": {"command": cmd, "timeout": 30}, "id": "call_exec_1"}],
                ),
                f"用户要求执行命令，调用 exec: {cmd[:40]}。",
            )

        # 读/查看文件（尝试从话里抽文件名）
        fm = re.search(r"读[取看]?[\s：:]*(.+?\.\w+)|查看[\s：:]*(.+?\.\w+)|打开[\s：:]*(.+?\.\w+)", s)
        if re.search(r"读|查看|打开", s, re.IGNORECASE) and fm:
            fname = (fm.group(1) or fm.group(2) or fm.group(3) or "").strip()
            if fname:
                return self._with_reasoning(
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "read_file", "args": {"path": fname}, "id": "call_read_1"}],
                    ),
                    f"用户想查看文件 {fname}，调用 read_file。",
                )

        # 写 / 保存 / 创建文件
        if re.search(r"写|保存|创建|生成.*文件|新建|记下来|存成", s, re.IGNORECASE):
            return self._with_reasoning(
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "write_file", "args": {"path": "out.txt", "content": s}, "id": "call_write_1"}
                    ],
                ),
                "用户要求创建/保存文件，调用 write_file。",
            )

        # 兜底：自然回应，并提示可接真实模型
        return self._with_reasoning(
            AIMessage(
                content=(
                    f"收到：{text}\n\n"
                    "（当前是演示模式，未配置 LLM API Key，由本地确定性模型应答，"
                    "能覆盖截图/时间/列目录/执行命令/计算/读写文件等常见意图。\n"
                    "在左侧「设置」填入 base_url + api_key 即可接入真实模型，"
                    "届时提示词与工具循环会让体感与 QwenPaw 一致地丝滑。）"
                )
            ),
            "这是闲聊或无需工具的问题，直接回答。",
        )

    def _generate(
        self,
        messages: list,
        stop: Optional[list] = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        response = self._pick_response(messages)
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(
        self,
        messages: list,
        stop: Optional[list] = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        return self._generate(messages, stop, run_manager, **kwargs)

    def _stream(
        self,
        messages: list,
        stop: Optional[list] = None,
        run_manager=None,
        **kwargs,
    ) -> Iterator[ChatGenerationChunk]:
        response = self._pick_response(messages)
        content = response.content or ""
        tool_calls = getattr(response, "tool_calls", None) or []
        reasoning = (getattr(response, "additional_kwargs", {}) or {}).get("reasoning_content", "")
        chunk_size = self.chunk_size

        # 先输出 reasoning 分片，让前端能看到“Thinking”
        if reasoning:
            for i in range(0, len(reasoning), chunk_size):
                part = reasoning[i : i + chunk_size]
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        additional_kwargs={"reasoning_content": part},
                    )
                )
                if run_manager:
                    run_manager.on_llm_new_token("")

        if not content and tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content="", tool_calls=tool_calls)
            )
            return

        for i in range(0, len(content), chunk_size):
            part = content[i : i + chunk_size]
            is_last = i + chunk_size >= len(content)
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content=part,
                    tool_calls=tool_calls if is_last else [],
                )
            )
            if run_manager:
                run_manager.on_llm_new_token(part)

    async def _astream(
        self,
        messages: list,
        stop: Optional[list] = None,
        run_manager=None,
        **kwargs,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._stream(messages, stop, run_manager, **kwargs):
            yield chunk

    def bind_tools(self, tools, *args, **kwargs):
        """覆盖 BaseChatModel 默认实现（默认会抛 NotImplementedError）。

        demo 模型不需要真正绑定工具，直接返回自身即可。
        """
        return self

    @property
    def _llm_type(self) -> str:
        return "demo-agent-model"


def _build_model():
    """按运行时配置选模型：配置了 llm.api_key 就接真实 LLM，否则用演示模型。

    配置来自 config.RuntimeConfig（前端 /config 热更新），无需重启。
    """
    from ..harness.models import make_deepseek_model
    from .config import get_config

    cfg = get_config()
    api_key = (cfg.llm.get("api_key") or "").strip()
    if api_key:
        return make_deepseek_model(
            model=(cfg.llm.get("model") or "deepseek-chat").strip(),
            api_key=api_key,
            base_url=(cfg.llm.get("base_url") or None),
            streaming=True,
            reasoning=bool(cfg.llm.get("reasoning", False)),
            provider=(cfg.llm.get("provider") or "").strip(),
        )
    return DemoAgentModel()


def _safe_path(workdir: str, path: str) -> str:
    """把工具传入的 path 解析到工作区内，并强制不越出 workdir（沙箱）。"""
    import os as _os

    p = path or ""
    cand = p if _os.path.isabs(p) else _os.path.join(workdir, p)
    ap = _os.path.abspath(cand)
    root = _os.path.abspath(workdir)
    if not (ap == root or ap.startswith(root + _os.sep)):
        raise ValueError(f"path '{path}' escapes workspace sandbox")
    return ap


class ExecArgs(BaseModel):
    """exec 工具参数：兼容标准 `command` 与 Qwen 风格 `args` 两种输入。"""

    command: str = Field(default="", description="要执行的 shell 命令字符串")
    args: Any = Field(
        default=None,
        description="命令参数列表或字符串（部分模型会输出 args 而不是 command）",
    )
    timeout: Optional[int] = Field(default=None, description="命令执行超时秒数（不传则用 running.shell_command_timeout）")


class TimerArgs(BaseModel):
    """create_timer 工具参数：在指定秒数后触发一次桌面通知提醒。"""

    seconds: int = Field(..., ge=1, le=3600, description="多少秒后触发提醒（1-3600）")
    message: str = Field(..., description="提醒内容")
    title: str = Field(default="喝水提醒", description="提醒标题")


def _build_tools(workdir: str, cm: ContextManager, filter_disabled: bool = True):
    import os as _os
    import subprocess as _sp

    # QwenPaw 对齐：读取运行时配置中的 shell 默认超时
    from .config import get_config as _get_config

    _cfg = _get_config()
    _running = _cfg.running if isinstance(_cfg.running, dict) else {}
    _default_shell_timeout = int(_running.get("shell_command_timeout", 60))

    def _tools_enabled() -> dict[str, bool]:
        p = Path.home() / ".workbuddy" / "tools_state.json"
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {k: v.get("enabled", True) for k, v in data.get("tools", {}).items()}
        except Exception:
            return {}

    enabled_map = _tools_enabled() if filter_disabled else {}

    def _write_file(path: str, content: str) -> str:
        p = _safe_path(workdir, path)
        _os.makedirs(_os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"written {p} ({len(content)} chars)"

    def _read_file(path: str) -> str:
        p = _safe_path(workdir, path)
        if not _os.path.exists(p):
            return f"Error: file not found: {path}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def _edit_file(path: str, old: str, new: str) -> str:
        p = _safe_path(workdir, path)
        if not _os.path.exists(p):
            return f"Error: file not found: {path}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        if old not in data:
            return f"Error: `old` string not found in {path}; no change made."
        cnt = data.count(old)
        data = data.replace(old, new)
        with open(p, "w", encoding="utf-8") as f:
            f.write(data)
        return f"edited {p} ({cnt} occurrence(s) replaced)"

    def _list_dir(path: str = ".") -> str:
        p = _safe_path(workdir, path)
        if not _os.path.exists(p):
            return f"Error: dir not found: {path}"
        entries = []
        for name in sorted(_os.listdir(p)):
            full = _os.path.join(p, name)
            kind = "d" if _os.path.isdir(full) else "f"
            size = _os.path.getsize(full) if _os.path.isfile(full) else 0
            entries.append(f"{kind} {name} ({size}B)")
        return "\n".join(entries) or "(empty)"

    def _exec(**kwargs) -> str:
        """在工作区内执行 shell 命令（敏感工具：需人工审批 + 危险模式拦截）。

        兼容两种参数风格：
        - 标准 schema：command="echo hi"
        - Qwen 等模型可能输出 args=["echo", "hi"]，这里自动拼接成命令字符串。

        注意：LangChain StructuredTool 会把参数名 `args` 内部重命名为 `v__args`，
        因此用 **kwargs 接收并同时检查两个 key。
        """
        command = kwargs.get("command", "")
        args = kwargs.get("args") or kwargs.get("v__args")
        timeout = kwargs.get("timeout") if kwargs.get("timeout") is not None else _default_shell_timeout
        if not command and args is not None:
            if isinstance(args, (list, tuple)):
                command = " ".join(str(a) for a in args)
            else:
                command = str(args)
        if not command:
            return "Error: no command provided"
        try:
            proc = _sp.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = _strip_ansi((proc.stdout or "")[-6000:])
            err = _strip_ansi((proc.stderr or "")[-2000:])
            return f"[exit {proc.returncode}]\nSTDOUT:\n{out}\nSTDERR:\n{err}"
        except _sp.TimeoutExpired:
            return f"Error: command timed out after {timeout}s"
        except Exception as e:  # noqa: BLE001
            return f"Error: {e}"

    def _desktop_screenshot(path: str = "", capture_window: bool = False) -> str:
        """Capture the desktop/all monitors and save to workspace. Returns a JSON with the saved path."""
        try:
            import mss
        except ImportError as e:  # pragma: no cover
            return json.dumps(
                {"ok": False, "error": "desktop_screenshot requires the 'mss' package. Install with: pip install mss"},
                ensure_ascii=False,
                indent=2,
            )
        path = (path or "").strip()
        if not path:
            path = f"desktop_screenshot_{int(time.time())}.png"
        if not path.lower().endswith(".png"):
            path = path.rstrip("/\\") + ".png"
        p = _safe_path(workdir, path)
        _os.makedirs(_os.path.dirname(p), exist_ok=True)

        system = platform.system()
        if system == "Darwin" and capture_window:
            try:
                proc = _sp.run(
                    ["screencapture", "-w", p],
                    capture_output=True,
                    text=True,
                    timeout=35,
                )
                if proc.returncode != 0:
                    stderr = (proc.stderr or "").strip() or "Unknown error"
                    return json.dumps({"ok": False, "error": f"screencapture failed: {stderr}"}, ensure_ascii=False, indent=2)
            except _sp.TimeoutExpired:
                return json.dumps({"ok": False, "error": "screencapture timed out (window selection cancelled?)"}, ensure_ascii=False, indent=2)
            except Exception as e:  # noqa: BLE001
                return json.dumps({"ok": False, "error": f"screencapture failed: {e}"}, ensure_ascii=False, indent=2)
        else:
            try:
                with mss.mss() as sct:
                    sct.shot(mon=0, output=p)
            except Exception as e:  # noqa: BLE001
                return json.dumps({"ok": False, "error": f"desktop_screenshot failed: {e}"}, ensure_ascii=False, indent=2)

        if not _os.path.isfile(p):
            return json.dumps({"ok": False, "error": "screenshot reported success but file was not created"}, ensure_ascii=False, indent=2)
        rel = _os.path.relpath(p, workdir)
        return json.dumps(
            {
                "ok": True,
                "tool": "desktop_screenshot",
                "path": rel,
                "message": f"Desktop screenshot saved to {rel}",
            },
            ensure_ascii=False,
            indent=2,
        )

    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
    _VIDEO_EXTENSIONS = {".mp4", ".webm", ".mpeg", ".mov", ".avi", ".mkv"}

    def _media_url(rel_path: str) -> str:
        return f"/workspace/files/{rel_path.replace(os.sep, '/')}"

    def _view_image(image_path: str = "", path: str = "") -> str:
        """Load a workspace image file so the model/frontend can see it. Returns JSON with image_url.

        Accepts either `image_path` (preferred) or `path` as the relative path inside the workspace.
        """
        file_path = image_path or path
        p = _safe_path(workdir, file_path)
        if not _os.path.isfile(p):
            return json.dumps({"ok": False, "error": f"file not found: {file_path}"}, ensure_ascii=False, indent=2)
        ext = _os.path.splitext(p)[1].lower()
        if ext not in _IMAGE_EXTENSIONS:
            return json.dumps({"ok": False, "error": f"unsupported image format: {ext}"}, ensure_ascii=False, indent=2)
        rel = _os.path.relpath(p, workdir)
        return json.dumps(
            {
                "ok": True,
                "tool": "view_image",
                "image_url": _media_url(rel),
                "path": rel,
                "message": f"Image loaded: {rel}",
            },
            ensure_ascii=False,
            indent=2,
        )

    def _view_video(video_path: str = "", path: str = "") -> str:
        """Load a workspace video file so the frontend can play it. Returns JSON with video_url.

        Accepts either `video_path` (preferred) or `path` as the relative path inside the workspace.
        """
        file_path = video_path or path
        p = _safe_path(workdir, file_path)
        if not _os.path.isfile(p):
            return json.dumps({"ok": False, "error": f"file not found: {file_path}"}, ensure_ascii=False, indent=2)
        ext = _os.path.splitext(p)[1].lower()
        if ext not in _VIDEO_EXTENSIONS:
            return json.dumps({"ok": False, "error": f"unsupported video format: {ext}"}, ensure_ascii=False, indent=2)
        rel = _os.path.relpath(p, workdir)
        return json.dumps(
            {
                "ok": True,
                "tool": "view_video",
                "video_url": _media_url(rel),
                "path": rel,
                "message": f"Video loaded: {rel}",
            },
            ensure_ascii=False,
            indent=2,
        )

    def _get_current_time() -> str:
        now = datetime.now(timezone.utc)
        return json.dumps(
            {
                "ok": True,
                "utc": now.isoformat(),
                "local": now.astimezone().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )

    def _create_timer(seconds: int, message: str, title: str = "提醒") -> str:
        """创建一个一次性定时提醒：seconds 秒后通过桌面通知提醒用户。

        对齐 QwenPaw 的 `cron create`（schedule_type=scheduled, run_at 一次性）：
        任务写入 cron.json 并由后台调度器执行——可见、可删、不阻塞 LLM 主循环。
        """
        from .scheduler import _notification_command, _cron_db_path

        run_at = (datetime.now(timezone.utc()) + timedelta(seconds=seconds)).isoformat()
        command = _notification_command(message, title)
        jid = f"timer-{int(datetime.now(timezone.utc()).timestamp())}-{abs(hash(message)) % 10000}"
        # 直接写入 cron.json（scheduled 一次性），再 sync_jobs 载入调度器
        try:
            import json as _json
            from pathlib import Path as _Path
            dbp = _cron_db_path()
            db = _json.loads(dbp.read_text(encoding="utf-8")) if dbp.exists() else {"jobs": []}
            db["jobs"] = [j for j in db.get("jobs", []) if j.get("id") != jid]
            db["jobs"].append({
                "id": jid,
                "name": f"{title}：{message}",
                "schedule": "",
                "command": command,
                "enabled": True,
                "task_type": "command",
                "timezone": "Asia/Shanghai",
                "schedule_type": "scheduled",
                "run_at": run_at,
                "created": int(datetime.now(timezone.utc()).timestamp()),
            })
            dbp.parent.mkdir(parents=True, exist_ok=True)
            dbp.write_text(_json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
            sync_jobs()
        except Exception as e:  # noqa: BLE001
            return _json.dumps({"ok": False, "error": f"无法写入定时任务: {e}"}, ensure_ascii=False)
        return _json.dumps(
            {
                "ok": True,
                "job_id": jid,
                "run_at": run_at,
                "message": message,
                "seconds": seconds,
                "note": "已加入 Cron 列表，可在 Cron 面板查看/删除。",
            },
            ensure_ascii=False,
            indent=2,
        )

    write_file = StructuredTool.from_function(
        func=_write_file, name="write_file",
        description="把文本写到工作区文件（path 相对工作区，content 为内容）。会创建父目录。",
    )
    read_file = StructuredTool.from_function(
        func=_read_file, name="read_file",
        description="读取工作区内某个文件的全文（path 相对工作区）。",
    )
    edit_file = StructuredTool.from_function(
        func=_edit_file, name="edit_file",
        description="精确替换文件内容：把文件里的 old 字符串替换成 new（需先 read_file 确认原文）。",
    )
    list_dir = StructuredTool.from_function(
        func=_list_dir, name="list_dir",
        description="列出工作区某目录下的文件与子目录（path 相对工作区，默认根）。",
    )
    exec_tool = StructuredTool.from_function(
        func=_exec, name="exec",
        description="在工作区内执行 shell 命令并返回输出（敏感操作，需人工审批）。",
        args_schema=ExecArgs,
    )
    calculator = StructuredTool.from_function(
        func=lambda a, b: str(a + b), name="calculator",
        description="整数加法：calculator(a, b)。",
    )
    desktop_screenshot = StructuredTool.from_function(
        func=_desktop_screenshot, name="desktop_screenshot",
        description="截取整个桌面/所有显示器并保存到工作区。对“桌面上有什么”“屏幕里是什么”这类视觉问题，优先使用本工具。返回保存的图片路径。",
    )
    view_image = StructuredTool.from_function(
        func=_view_image, name="view_image",
        description="加载工作区里的图片文件（由 desktop_screenshot 等工具产生），让模型/前端能看到它。参数名：image_path（或 path），值为图片在工作区中的相对路径。",
    )
    view_video = StructuredTool.from_function(
        func=_view_video, name="view_video",
        description="加载工作区里的视频文件，让前端能播放。参数名：video_path（或 path），值为视频在工作区中的相对路径。",
    )
    get_current_time = StructuredTool.from_function(
        func=_get_current_time, name="get_current_time",
        description="获取当前 UTC 与本地时间。",
    )
    create_timer = StructuredTool.from_function(
        func=_create_timer,
        name="create_timer",
        description="创建一个一次性定时提醒：在 seconds 秒后通过桌面通知提醒用户。用于‘10秒后提醒我喝水’这类需求，优先使用本工具而不是 exec。",
        args_schema=TimerArgs,
    )

    # 常规工具：标准三守卫（含 FilePath 沙箱、ShellEvasion）
    std_engine = ToolGuardEngine(guardians=[
        FilePathGuardian(allowed_roots=[workdir, os.getcwd()]),
        RuleBasedGuardian(),
        ShellEvasionGuardian(),
    ])
    guarded = make_guarded_tools(
        [
            calculator,
            write_file,
            read_file,
            edit_file,
            list_dir,
            desktop_screenshot,
            view_image,
            view_video,
            get_current_time,
            create_timer,
        ],
        std_engine,
    )
    # exec 单独用"允许 shell 语法但拦截灾难性命令"的引擎（不挂 ShellEvasion，
    # 否则正常管道/重定向会被误杀）；exec 本就在 SENSITIVE_TOOLS 里，强制走人工审批。
    exec_engine = ToolGuardEngine(guardians=[
        FilePathGuardian(allowed_roots=[workdir, os.getcwd()]),
        RuleBasedGuardian(allowlist={"exec"}),
    ])
    guarded.update(make_guarded_tools([exec_tool], exec_engine))
    guarded["recall"] = make_recall_tool(cm)
    # 长期记忆工具：memory_search（对齐 QwenPaw 的 ReMeLight memory_search）
    mm = get_memory_manager()
    if mm is not None:
        for t in mm.list_memory_tools():
            guarded.setdefault(t.name, t)
    # 根据用户在前端 Tools 面板的开关过滤禁用工具
    guarded = {k: v for k, v in guarded.items() if enabled_map.get(k, True)}
    return guarded


_graph = None
_metrics = None
_cm = None
_built_version = -1  # 配置版本：变化时重建图（换模型/key 即时生效）
_shared_checkpointer = None  # 所有图共享，保证多会话历史跨模型/请求持久，且 HITL 可恢复
_request_graph_cache: dict = {}
_request_graph_lock = threading.Lock()


def get_metrics() -> Metrics:
    global _metrics
    if _metrics is None:
        _metrics = Metrics()
    return _metrics


def get_context_manager() -> ContextManager:
    global _cm
    if _cm is None:
        # 默认启用受控召回：recall 是 agent 在**同一 thread 内**的显式动作，
        # 只还原本会话自己折叠的历史，不跨会话，属安全操作。可用 env 关闭。
        recall_on = os.getenv("ALLOW_UNSANDBOXED_RECALL", "1") == "1"
        from .context_config import load_config as load_context_config

        cfg = load_context_config()
        _cm = ContextManager(
            budget_tokens=int(os.getenv("CONTEXT_BUDGET", str(cfg.budget_tokens))),
            allow_unsandboxed_recall=(
                cfg.enable_recall if os.getenv("ALLOW_UNSANDBOXED_RECALL") is None else recall_on
            ),
            strip_media=cfg.strip_media,
            max_tool_result_chars=cfg.max_tool_result_chars,
            metrics=get_metrics(),
        )
    return _cm


def reset_context_manager() -> None:
    """清掉单例，使下次访问按最新 context_config.json 重建。"""
    global _cm
    _cm = None


def _checkpoint_db_path() -> Path:
    """checkpoint 持久化文件路径（与现有 ~/.workbuddy 数据文件同目录）。"""
    p = Path.home() / ".workbuddy"
    p.mkdir(parents=True, exist_ok=True)
    return p / "checkpoints.sqlite"


def get_shared_checkpointer():
    """所有编译图共享同一个 async SQLite checkpointer。

    状态持久化到 ~/.workbuddy/checkpoints.sqlite，进程重启后可恢复对话/审批/循环
    状态（对齐 QwenPaw「文件即真相源、重启可恢复」）。

    必须由 app lifespan 调用 init_shared_checkpointer() 完成异步初始化后才能使用；
    未初始化时显式报错，避免静默回退到内存导致「以为持久化其实没持久化」。
    """
    if _shared_checkpointer is None:
        raise RuntimeError(
            "checkpointer 尚未初始化，请确认 app lifespan 已调用 init_shared_checkpointer()"
        )
    return _shared_checkpointer


async def init_shared_checkpointer() -> None:
    """在应用启动（running event loop 内）初始化全局 async SQLite checkpointer。

    AsyncSqliteSaver 构造时会绑定当前事件循环，因此必须在 lifespan（或任意 async
    上下文）中调用，不能在同步函数里直接 new。持久化使「旧对话卡死」在重启后不再发生。
    """
    global _shared_checkpointer
    if _shared_checkpointer is not None:
        return
    db = str(_checkpoint_db_path())
    # busy_timeout=30s：若 checkpointer 连接短暂被锁（例如重启瞬间旧实例未完全退出），
    # 操作会重试而非静默挂起；配合 start.sh 启动前杀旧进程，杜绝多实例共享 sqlite 导致的冻结。
    conn = await aiosqlite.connect(db, timeout=30.0)
    await conn.execute("PRAGMA busy_timeout=30000")
    try:
        await conn.execute("PRAGMA journal_mode=WAL")
    except Exception:  # noqa: BLE001
        pass
    _shared_checkpointer = AsyncSqliteSaver(conn)


def _approval_gate_from_config(cfg=None) -> "ApprovalGate":
    """按配置 + 环境变量构造审批门：对齐 QwenPaw approval_level。

    approval_level: OFF=全部自动放行 / SMART=只读自动放行 / AUTO=同上 /
    STRICT=全部人工审批。环境变量 AGENT_TRUST_MODE 仍可强制信任模式。
    """
    import os as _os
    from .config import get_config as _gc, approval_settings

    cfg = cfg or _gc()
    level = cfg.approval_level if isinstance(cfg.approval_level, str) else "AUTO"
    trust_mode, safe_exec = approval_settings(level)
    if _os.getenv("AGENT_TRUST_MODE"):
        trust_mode = True
    return ApprovalGate(trust_mode=trust_mode, auto_approve_safe_exec=safe_exec)


def _resolve_max_iter(cfg) -> int:
    """从 running.loop.iteration.max_iterations 解析最大迭代轮数，None 回退 running.max_iters。"""
    env = os.getenv("MAX_ITERATIONS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    running = cfg.running if isinstance(cfg.running, dict) else {}
    loop = running.get("loop", {}) or {}
    it = loop.get("iteration", {}) or {}
    mi = it.get("max_iterations")
    if mi is None:
        mi = running.get("max_iters", 100)
    try:
        return int(mi)
    except (TypeError, ValueError):
        return 100


def get_graph():
    """返回编译好的 harness 图（单例，复用同一 ContextManager / Metrics / checkpointer）。

    配置版本变更（前端保存 /config）时自动重建，使新的 api_key/model 即时生效。
    """
    global _graph, _built_version
    from .config import get_config

    cfg = get_config()
    if _graph is not None and _built_version == cfg.version:
        return _graph
    workdir = os.getenv("AGENT_WORKDIR") or str(_workspace_dir())
    os.makedirs(workdir, exist_ok=True)
    cm = get_context_manager()
    tools = _build_tools(workdir, cm)
    model = _build_model()
    max_iter = _resolve_max_iter(cfg)
    _graph = build_graph(
        model,
        tools,
        max_iterations=max_iter,
        context_manager=cm,
        approval_gate=_approval_gate_from_config(cfg),
        metrics=get_metrics(),
        checkpointer=get_shared_checkpointer(),
        memory_manager=get_memory_manager(),
        core_files_manager=get_core_files_manager(),
    )
    _built_version = cfg.version
    return _graph


def get_request_graph(llm_overrides: dict) -> tuple[Any, bool]:
    """按请求级模型覆盖 + 会话模式构建（并缓存）一个图，复用全局共享 checkpointer。

    前端可"每次请求携带 model/reasoning/api_key/mode"，即时切换模型/模式而无需先 POST /config，
    且同一 thread_id 的历史在任意图之间都能被寻址（HITL 恢复、上下文连续）。
    不同 mode 装载不同的 Loop Gates 集合与系统提示（chat/coding/mission）。

    返回：
        (graph, actual_reasoning) —— actual_reasoning 与请求时一致，不再因工具绑定被强制关闭。
    """
    from .config import get_config, approval_settings
    from ..harness.models import make_deepseek_model

    cfg = get_config()
    api_key = (llm_overrides.get("api_key") or "").strip() or (cfg.llm.get("api_key") or "").strip()
    model = (llm_overrides.get("model") or "").strip() or (cfg.llm.get("model") or "deepseek-chat").strip()
    base_url = (llm_overrides.get("base_url") or "").strip() or (cfg.llm.get("base_url") or None)
    reasoning = bool(
        llm_overrides.get("reasoning")
        if llm_overrides.get("reasoning") is not None
        else cfg.llm.get("reasoning", False)
    )
    provider = (llm_overrides.get("provider") or "").strip() or (cfg.llm.get("provider") or "").strip()
    mode = (llm_overrides.get("mode") or "chat").strip().lower()

    key_hash = hashlib.sha256((api_key or "none").encode()).hexdigest()[:16]
    # 与 _approval_gate_from_config 保持同一来源：approval_level -> (trust, safe)
    level = cfg.approval_level if isinstance(cfg.approval_level, str) else "AUTO"
    trust_mode, safe_exec = approval_settings(level)
    if os.getenv("AGENT_TRUST_MODE"):
        trust_mode = True
    # 缓存失效键直接包含「影响图编译的字段」，而不依赖全局 cfg.version：
    # - model/reasoning/trust/safe_exec 等原本就在 sig 内；
    # - max_iterations(mi) 显式加入，使「改最大迭代轮数」能正确触发重建；
    # - 不依赖 cfg.version，避免「改 rate_limit/llm_retry 等无关字段也强制全量重编译」的副作用。
    sig = json.dumps(
        {
            "m": model, "b": base_url, "r": reasoning, "p": provider, "k": key_hash,
            "mode": mode, "trust": trust_mode, "safe_exec": safe_exec,
            # 直接用影响图编译的字段做失效键，避免原先依赖全局 cfg.version 导致的
            # “任何 /config 保存都让全部请求图缓存失效、并发重编译”的副作用
            "mi": _resolve_max_iter(cfg),
        },
        sort_keys=True,
    )
    with _request_graph_lock:
        if sig in _request_graph_cache:
            return _request_graph_cache[sig]  # 缓存的是 (graph, reasoning) 元组

    workdir = os.getenv("AGENT_WORKDIR") or str(_workspace_dir())
    os.makedirs(workdir, exist_ok=True)
    cm = get_context_manager()
    tools = _build_tools(workdir, cm)
    # Qwen3.6-plus / DeepSeek / o1 / o3 等主流推理模型已支持“思考+工具”并发。
    # 不再一刀切关闭 reasoning；若模型本身不兼容，后端会抛 API 错误并由前端显示，
    # 用户可手动关闭 reasoning 开关。
    if api_key:
        model_obj = make_deepseek_model(
            model=model,
            api_key=api_key,
            base_url=base_url,
            streaming=True,
            reasoning=reasoning,
            provider=provider,
        )
    else:
        model_obj = DemoAgentModel()
    mode_spec = get_mode(mode)
    max_iter = _resolve_max_iter(cfg)
    gates = resolve_gates(mode, {"max_iterations": max_iter})
    g = build_graph(
        model_obj,
        tools,
        gates=gates,
        context_manager=cm,
        approval_gate=_approval_gate_from_config(cfg),
        metrics=get_metrics(),
        checkpointer=get_shared_checkpointer(),
        system_hint=mode_spec.system_prompt,
        memory_manager=get_memory_manager(),
        core_files_manager=get_core_files_manager(),
    )
    with _request_graph_lock:
        _request_graph_cache[sig] = (g, reasoning)
    return g, reasoning
