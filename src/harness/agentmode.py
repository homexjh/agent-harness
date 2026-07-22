"""AgentMode —— 能力打包与门控（QwenPaw: modes/base.py 的 AgentMode 行为束）。

一个 mode 把 { gates, 可用工具集, 系统提示 } 打成一包。运行时按 mode 名解析出对应装配：
- chat    —— 通用对话：迭代兜底 + 防打转 + 预算。
- coding  —— 编码任务：额外挂 rubric（防只说不做）+ 文件/编码类工具 + 编码规范提示。
- mission —— 任务驱动：MissionGate(读 prd.json 完成标记) + rubric + budget，反过早收工。

对照 QwenPaw：`register_react_gates()` 给默认 ReAct 装 Iteration+DoomLoop+Rubric；
Mission/Coding 模式在此基础上叠加各自的门与工具。这里用"按 mode 返回 gate 列表 +
工具白名单 + 提示"实现同一思想，且 gate 会话状态由 LangGraph checkpointer 持久化。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from .gates.base import StopGate
from .gates.budget import BudgetGate
from .gates.doom_loop import DoomLoopGate
from .gates.file_loop import FileLoopGate
from .gates.iteration import IterationGate
from .gates.rubric import MarkerRubric, StandaloneRubricGate
from .stability.modes import MissionGate


MISSION_MARKERS = ["MISSION COMPLETE", "任务完成", "TASK DONE", "ALL DONE"]

# ---------------------------------------------------------------------------
# 通用内核（三种模式共享）：身份 + 行为准则 + 工具纪律 + 多模态 + 沟通风格
# 这是 Agent Harness 相对 QwenPaw 最关键的"丝滑"来源：一段清晰、可执行、
# 不啰嗦的行为契约，让真实模型像成熟产品一样主动推进、闭环交付。
# ---------------------------------------------------------------------------
_CORE = """\
你是 **Agent Harness** —— 一个能真正动手的通用智能体，而不仅是聊天机器人。

## 核心原则
1. **先动手，再开口。** 凡是能用工具核实、获取或完成的事，先调用工具，不要凭印象臆测或只给口头步骤。
2. **闭环交付。** 接到任务就持续推进到真正完成（拿到结果 / 改好文件 / 跑通命令），不要在"还差一步"时停下来问"还需不需要我做"。
3. **自行纠错。** 工具报错就读取错误信息、调整参数重来；不要卡住或把原始错误直接甩给用户。
4. **诚实边界。** 拿不到的信息、做不到的事，明确说"我无法……"，并指出可以打开哪个路径或换什么方式。

## 工具纪律
- 文件 / 目录 / 命令 / 计算 / 时间 / 截图 都有对应工具，需要时就用，参数要具体。
- 改文件前先读原文；引用代码位置用 `path/to/file.py:42` 格式。
- `exec` 在工作区内执行 shell。只读/查看类命令直接跑；涉及写盘或不可逆操作时，说明你要做什么。
- 不编造路径、配置项或命令输出；以工具真实返回为准。
- **问候、闲聊或明显无需工具的问题（如"你好""谢谢""再见"），直接回答，不要调用工具。**

## 沟通风格
- 与用户提问语言一致（中文提问用中文答）。
- 简洁：直接给结果、路径、原因；少寒暄、少套话。
- 事实类回答最好有依据（你刚读过的路径 / 刚跑出的输出）；结论要可核实。
- 多步任务完成后给一句简短总结，而不是复述每一步。
"""

CHAT_PROMPT = (
    _CORE
    + "\n你当前处于 **通用对话模式（chat）**：以协助用户完成各类查询、操作与轻量任务为主，"
    "遇到需要动手的事直接用工具推进。\n"
)

CODING_PROMPT = (
    _CORE
    + "\n你当前处于 **编码模式（coding）**：\n"
    "- 用工具读/写/改文件与执行命令，不凭空臆造文件内容；\n"
    "- 改动前先读原文，保持路径纪律，提交/运行前自查；\n"
    "- 未真正完成前不要只做口头总结，继续调用工具推进。\n"
)

MISSION_PROMPT = (
    _CORE
    + "\n你当前处于 **任务模式（mission）**：必须持续推进直到目标达成：\n"
    "- 把大目标拆成可验证的小步骤，逐个用工具完成；\n"
    "- 每步完成后检查是否还有未完成项，有就继续；\n"
    "- 全部完成后，输出一行且仅一行包含 `MISSION COMPLETE` 的最终结论。\n"
)


@dataclass
class ModeSpec:
    name: str
    description: str
    system_prompt: str
    # 该模式的门集合工厂：给定运行时选项返回 gate 列表
    gate_factory: Callable[[dict], list]
    # 该模式暴露的工具名白名单；None 表示暴露全部已注册工具
    tool_allow: Optional[list] = None


def _chat_gates(opts: dict) -> list:
    return [
        IterationGate(max_iterations=opts.get("max_iterations", 30)),
        BudgetGate(max_tokens=opts.get("max_tokens", 300_000)),
        DoomLoopGate(),
    ]


def _coding_gates(opts: dict) -> list:
    return [
        IterationGate(max_iterations=opts.get("max_iterations", 40)),
        BudgetGate(max_tokens=opts.get("max_tokens", 300_000)),
        DoomLoopGate(),
        # StandaloneRubricGate 已移除：实测在工具调用后会疯狂重复触发（最多 5 次），
        # 导致 60 秒生成、模型反复被逼续跑。防偷懒的收益远不抵卡顿代价。
    ]


def _mission_gates(opts: dict) -> list:
    gates: list[StopGate] = [
        IterationGate(max_iterations=opts.get("max_iterations", 60)),
        BudgetGate(max_tokens=opts.get("max_tokens", 500_000)),
        MissionGate(
            prd_path=opts.get("prd_path"),
            completion_markers=opts.get("markers", MISSION_MARKERS),
            max_turns=opts.get("mission_max_turns", 50),
        ),
        DoomLoopGate(),
        # StandaloneRubricGate 已移除：MissionGate 已能靠标记判定完成，
        # 不需要 rubric 再额外强制续跑。
    ]
    if opts.get("loop_dir"):
        gates.append(FileLoopGate(loop_dir=opts["loop_dir"]))
    return gates


_MODES = {
    "chat": ModeSpec(
        name="chat",
        description="通用对话：迭代兜底 + 防打转 + 预算控制。",
        system_prompt=CHAT_PROMPT,
        gate_factory=_chat_gates,
        tool_allow=None,
    ),
    "coding": ModeSpec(
        name="coding",
        description="编码任务：文件/命令工具 + 防只说不做 + 编码规范。",
        system_prompt=CODING_PROMPT,
        gate_factory=_coding_gates,
        tool_allow=None,
    ),
    "mission": ModeSpec(
        name="mission",
        description="任务驱动：读 prd.json 完成标记，反过早收工。",
        system_prompt=MISSION_PROMPT,
        gate_factory=_mission_gates,
        tool_allow=None,
    ),
}


def list_modes() -> list:
    return [
        {"name": m.name, "description": m.description} for m in _MODES.values()
    ]


def get_mode(name: Optional[str]) -> ModeSpec:
    return _MODES.get((name or "chat").strip().lower(), _MODES["chat"])


def resolve_gates(mode_name: Optional[str], opts: Optional[dict] = None) -> list:
    """按 mode 名解析出门集合。opts 可覆盖 max_iterations/max_tokens/prd_path 等。"""
    opts = dict(opts or {})
    # 环境变量兜底
    opts.setdefault("max_iterations", int(os.getenv("MAX_ITERATIONS", "30")))
    return get_mode(mode_name).gate_factory(opts)
