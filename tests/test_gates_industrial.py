"""工业级 harness 补齐能力单测：BudgetGate / StandaloneRubricGate / FileLoopGate /
AgentMode 装配 / 上下文检视 + 媒体剥离 + 工具结果剪枝 / HITL 拒绝归一化。

零外部依赖（不需要网络 / API key）；直接 `pytest tests/test_gates_industrial.py`。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from harness.gates.base import StopAction
from harness.gates.budget import BudgetGate, _estimate_tokens
from harness.gates.file_loop import FileLoopGate
from harness.gates.rubric import (
    DefaultRubric,
    MarkerRubric,
    StandaloneRubricGate,
)


# --------------------------------------------------------------------------- #
# BudgetGate（priority 20）
# --------------------------------------------------------------------------- #
def test_budget_stops_when_exhausted():
    gate = BudgetGate(max_tokens=10)
    state = {"messages": [HumanMessage(content="x" * 400)]}  # ~100 tok
    r = gate.check(1, state, {})
    assert r is not None and r.action == StopAction.STOP
    assert "budget" in (r.reason or "").lower()


def test_budget_warns_once_near_limit():
    gate = BudgetGate(max_tokens=100, warn_ratio=0.8)
    # ~90 tok -> 落在 [80,100) 警告区间
    state = {"messages": [HumanMessage(content="y" * 360)]}
    gs = {}
    r1 = gate.check(1, state, gs)
    assert r1 is not None and r1.action == StopAction.CONTINUE
    assert r1.continuation_message and "预算" in r1.continuation_message
    # 第二次不再重复警告（仅一次）
    r2 = gate.check(2, state, gs)
    assert r2 is None


def test_budget_none_when_ample():
    gate = BudgetGate(max_tokens=1_000_000)
    state = {"messages": [HumanMessage(content="hi")]}
    assert gate.check(1, state, {}) is None


def test_estimate_tokens_counts_tool_calls():
    msg = AIMessage(
        content="",
        tool_calls=[{"name": "write_file", "args": {"path": "a", "content": "b"}, "id": "1"}],
    )
    assert _estimate_tokens([msg]) > 0


# --------------------------------------------------------------------------- #
# StandaloneRubricGate（priority 90，反过早收工）
# --------------------------------------------------------------------------- #
def test_rubric_pushes_continue_when_unsatisfied():
    """任务进行中（已调用工具）给出无标记的纯文本总结 -> CONTINUE 催继续。"""
    gate = StandaloneRubricGate(rubric=MarkerRubric(["DONE"]), max_interventions=2)
    state = {
        "messages": [
            HumanMessage(content="帮我创建 foo.txt"),
            AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "foo.txt", "content": "hi"}, "id": "c1"}]),
            ToolMessage(content="written foo.txt", tool_call_id="c1"),
            AIMessage(content="我先总结一下当前进度……"),
        ]
    }
    gs = {}
    r = gate.check(1, state, gs)
    assert r is not None and r.action == StopAction.CONTINUE
    assert gs["interventions"] == 1


def test_rubric_satisfied_lets_finish():
    gate = StandaloneRubricGate(rubric=MarkerRubric(["DONE"]))
    state = {"messages": [AIMessage(content="全部完成 DONE")]}
    assert gate.check(1, state, {}) is None


def test_rubric_ignores_tool_calls():
    gate = StandaloneRubricGate(rubric=MarkerRubric(["DONE"]))
    state = {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": "exec", "args": {}, "id": "1"}])
        ]
    }
    # 还在调用工具（在动手）-> 不干预
    assert gate.check(1, state, {}) is None


def test_rubric_max_interventions_release():
    """任务进行中但催促次数用尽 -> 放行。"""
    gate = StandaloneRubricGate(rubric=MarkerRubric(["DONE"]), max_interventions=1)
    state = {
        "messages": [
            HumanMessage(content="帮我做 X"),
            AIMessage(content="", tool_calls=[{"name": "exec", "args": {}, "id": "c1"}]),
            ToolMessage(content="done", tool_call_id="c1"),
            AIMessage(content="还没完"),
        ]
    }
    gs = {}
    assert gate.check(1, state, gs).action == StopAction.CONTINUE
    # 次数用尽 -> 放行（返回 None，交给 IterationGate 兜底）
    assert gate.check(2, state, gs) is None


def test_rubric_skips_chitchat_greetings():
    """Regression: 问候/闲聊不应触发 mission/coding 模式的任务续跑。"""
    gate = StandaloneRubricGate(rubric=MarkerRubric(["DONE"]), max_interventions=3)
    for greeting in ["你好", "Hello", "Hi", "在吗", "早上好！", "thanks"]:
        state = {
            "messages": [
                HumanMessage(content=greeting),
                AIMessage(content="你好！有什么我可以帮你的吗？"),
            ]
        }
        assert gate.check(1, state, {}) is None, f"greeting {greeting!r} should not trigger continuation"


def test_rubric_no_fire_on_pure_qa():
    """Regression: 纯问答（本轮未调用任何工具）不应被强制续跑。

    用户问"什么是递归"、模型直接文字回答是正确行为。强制续跑会导致
    "答完→停顿→又被逼着调工具→又输出一堆"的卡顿体验。
    """
    gate = StandaloneRubricGate(rubric=MarkerRubric(["MISSION COMPLETE"]), max_interventions=3)
    state = {
        "messages": [
            HumanMessage(content="用一句话解释什么是递归"),
            AIMessage(content="递归是函数调用自身的编程技术。"),
        ]
    }
    assert gate.check(1, state, {}) is None


def test_rubric_no_fire_on_task_without_tool_use():
    """即使用户提了任务，如果模型本轮从未调用工具就给了文本回答，也不强制续跑。

    这是有意的权衡：避免对正常回答造成卡顿。用户可以手动说"继续"来推动。
    """
    gate = StandaloneRubricGate(rubric=MarkerRubric(["DONE"]), max_interventions=3)
    state = {
        "messages": [
            HumanMessage(content="帮我写一个冒泡排序"),
            AIMessage(content="我先想想怎么实现。"),
        ]
    }
    assert gate.check(1, state, {}) is None


def test_rubric_fires_after_tool_use_then_premature_stop():
    """模型用了工具但过早给无标记文本收工 -> CONTINUE（rubric 的核心场景）。"""
    gate = StandaloneRubricGate(rubric=MarkerRubric(["DONE"]), max_interventions=3)
    state = {
        "messages": [
            HumanMessage(content="帮我创建 foo.txt 并写入 hello"),
            AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "foo.txt", "content": "hello"}, "id": "c1"}]),
            ToolMessage(content="written foo.txt (5 chars)", tool_call_id="c1"),
            AIMessage(content="文件已创建。"),  # 无 DONE 标记，过早收工
        ]
    }
    r = gate.check(1, state, {})
    assert r is not None and r.action == StopAction.CONTINUE


# --------------------------------------------------------------------------- #
# FileLoopGate（priority 60，文件驱动长任务）
# --------------------------------------------------------------------------- #
def test_file_loop_done_sentinel_stops():
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "DONE"), "w").close()
        gate = FileLoopGate(loop_dir=d)
        r = gate.check(1, {}, {})
        assert r is not None and r.action == StopAction.STOP


def test_file_loop_status_json_done_stops():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "status.json"), "w") as f:
            f.write('{"done": true}')
        gate = FileLoopGate(loop_dir=d)
        r = gate.check(1, {}, {})
        assert r is not None and r.action == StopAction.STOP


def test_file_loop_remaining_pushes_continue():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "status.json"), "w") as f:
            f.write('{"done": false, "remaining": ["step A", "step B"]}')
        gate = FileLoopGate(loop_dir=d)
        r = gate.check(1, {}, {})
        assert r is not None and r.action == StopAction.CONTINUE
        assert "step A" in (r.continuation_message or "")


def test_file_loop_timeout_stops():
    with tempfile.TemporaryDirectory() as d:
        gate = FileLoopGate(loop_dir=d, max_turns=5)
        r = gate.check(5, {}, {})
        assert r is not None and r.action == StopAction.STOP
        assert "timeout" in (r.reason or "").lower()


# --------------------------------------------------------------------------- #
# AgentMode 装配
# --------------------------------------------------------------------------- #
def test_agentmode_resolves_distinct_gate_sets():
    from harness.agentmode import get_mode, list_modes, resolve_gates

    names = {m["name"] for m in list_modes()}
    assert {"chat", "coding", "mission"} <= names

    chat = resolve_gates("chat")
    coding = resolve_gates("coding")
    mission = resolve_gates("mission")

    chat_names = {g.name for g in chat}
    coding_names = {g.name for g in coding}
    mission_names = {g.name for g in mission}

    assert "budget" in chat_names and "iteration" in chat_names
    # rubric 已从所有默认模式移除（实测导致疯狂续跑、60s 生成）
    assert "rubric" not in chat_names
    assert "rubric" not in coding_names
    assert "rubric" not in mission_names
    assert "MissionGate" in mission_names  # mission 挂 MissionGate

    # 未知 mode 回退 chat
    assert get_mode("nope").name == "chat"


# --------------------------------------------------------------------------- #
# HITL 拒绝归一化
# --------------------------------------------------------------------------- #
def test_hitl_decision_normalization():
    from harness.graph import _is_approved

    for v in ["approved", "yes", "ok", True, "allow"]:
        assert _is_approved(v) is True, v
    for v in ["rejected", "reject", "deny", "no", False, "cancel"]:
        assert _is_approved(v) is False, v
    # None（无值恢复）默认放行，兼容旧客户端
    assert _is_approved(None) is True
    # dict 形态
    assert _is_approved({"decision": "rejected"}) is False
    assert _is_approved({"action": "approved"}) is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("ALL INDUSTRIAL GATE TESTS PASSED")
