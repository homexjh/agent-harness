# Checkpoint 回滚（Time-Travel）设计文档

> 目标：在 agent-harness（LangGraph `StateGraph` + `AsyncSqliteSaver` 单例）上，暴露「检查点回滚」能力，覆盖三种形态：
> **P1 对话级撤销 / P2 调试重放 / P3 分叉实验**。
> 本文档为方案对齐版，落地前需原型期验证少数标注点（见 §9）。

---

## 1. 背景与动机

用户已在闲聊（chat）路径确认 LangGraph 框架可用——本方案的能力**对所有 mode（chat / coding / mission）生效**，与具体模式无关（`mode` 本身是 `AgentState` 的一个字段，回滚时一并恢复）。当前 checkpointer 只用到第一层能力：

| 已具备 | 缺失 |
|---|---|
| `AsyncSqliteSaver` 落盘 `~/.agent-harness/checkpoints.sqlite` | `aget_state_history` 从未调用 |
| 会话复合键 `user:thread` 隔离 | `aupdate_state` 从未调用 |
| 进程崩溃恢复（crash recovery） | replay / fork 从未暴露 |
| `GET /threads/{id}/state`（仅当前状态） | 历史步骤快照不可见 |

"检查点回滚" = LangGraph 官方的 **time-travel**：每个 super-step 的状态快照已落盘，理论可回看、可重放、可分叉。本方案把这套原生能力暴露成功能。

**与现有 `reset_thread` 的区别**：`POST /threads/{id}/reset` 是「全删 checkpoint + 清审批 + 清上下文」（`adelete_thread`），等价于 git 的 `rm -rf .git`。本方案的回滚是「精细移动到某一步」，历史不丢、只移动当前指针，语义更接近 `git reset --soft`。

---

## 2. 底层原语（LangGraph time-travel）

所有三种 UI 共用同一组底层 API，来自 `langgraph` 的 compiled graph：

- `await graph.aget_state_history(cfg)` → 返回该 thread 所有步骤快照，按时间倒序。每项为 `(state, config)`，含：
  - `checkpoint_id` / `parent_checkpoint_id`（步骤 DAG 的父子边）
  - `next`：下一个要执行的 node 列表（挂起点）
  - `created_at`、`step`（第几步）
  - `values`：完整 `AgentState` 快照（含 `messages`）
- `await graph.aupdate_state(target_cfg, values)` → 以 `target_cfg` 指定的 checkpoint 为父，应用 `values` 变更，生成**新 checkpoint**。
  - **软回滚关键语义**：`aupdate_state(target_cfg, {})`（空 values）会"复制"目标步的状态为新当前点，**不丢历史、只移动当前指针**。这是对话撤销与分叉的干净实现。
- `await graph.ainvoke(input, target_cfg)` → 从 `target_cfg` 指定的 checkpoint **replay**（重新执行后续路径，产生新分支）。

> ⚠️ **原型期验证点 V1**：`aupdate_state(target_cfg, {})` 是否确实生成"指向目标状态的新 checkpoint 且 `next` 正确"。需小脚本实测确认（LangGraph 版本差异）。

---

## 3. 数据模型

### 3.1 AgentState（`src/harness/state.py`）

```python
class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]  # append-only 原始事件日志，永不折叠
    prompt_messages: list           # 上下文窗口（ContextManager 折叠产出，整列表替换）
    turn: int                       # 循环轮次
    gate_state: dict                # 各 LoopGate 内部状态（随 thread 持久化）
    context_state: dict             # 上下文管理器内部状态
    decision: str                   # continue / stop / ask
    stop_reason: str
    pending_after_hitl: str         # HITL 恢复后回到的节点
    hitl_decision: str              # approved / rejected / None
    mode: str                       # chat / coding / mission
```

**回滚本质**：`messages` 是 `add_messages` reducer 的 append-only 日志。回滚不动 reducer 语义，而是**移动当前指针到历史某步的快照**（`aupdate_state(target, {})`），让后续生成从那一步继续。这与"直接删消息"不同——历史 checkpoint 全保留，可再前进。

### 3.2 history 返回结构（设计）

`GET /threads/{id}/history` 返回压缩列表，避免把每条完整 messages 都下发：

```json
[
  {
    "checkpoint_id": "1a2b...",
    "parent_id": "0f9c...",
    "step": 7,
    "created_at": "2026-07-25T16:00:00Z",
    "next": ["agent_node"],
    "n_messages": 14,
    "last_role": "ai",
    "preview": "好的，我来帮你看看这个文件…"   // 末条消息截断预览
  }
]
```

### 3.3 复合键隔离

所有操作必须沿用 `_ckpt_thread(user_id, thread_id)` = `"{user}:{thread}"` 作为 `thread_id`。回滚 / 分叉不得跨用户。fork 新线也须带同一 `user` 前缀。

---

## 4. API 契约（P0 后端原语）

均在 `src/server/app.py` 新增，鉴权复用 `require_auth`。

### 4.1 `GET /threads/{id}/history`
返回 §3.2 的步骤列表（倒序）。无 body。

### 4.2 `POST /threads/{id}/rollback`
```json
{ "checkpoint_id": "1a2b..." }
```
行为：`aupdate_state(target_cfg, {})` → 软回滚到该步（当前指针移动，历史不丢）。
返回：`{ "ok": true, "thread_id": "...", "checkpoint_id": "new..." }`

> 对应 **P1 对话级撤销**。前端"回到这里"按钮即调此。

### 4.3 `POST /threads/{id}/fork`
```json
{ "checkpoint_id": "1a2b...", "new_thread_id": "opt..." }
```
行为：从目标步复制到新 thread（`graph.with_config({"configurable":{"thread_id": NEW_CK}}).aupdate_state(target_cfg, {})`），原线完整保留。
返回：`{ "ok": true, "new_thread_id": "user:xxx" }`

> 对应 **P3 分叉实验**。

### 4.4 `POST /threads/{id}/replay`
```json
{ "checkpoint_id": "1a2b...", "input": "可选，覆盖该步之后的走向" }
```
行为：`graph.ainvoke(input, target_cfg)` → 从该步重新执行后续（生成新路径）。
返回：SSE 流（复用现有 `/api/chat` 的流格式）。

> 对应 **P2 调试重放** 的"重跑"动作。

---

## 5. 前端 UI 形态（React 18 + Vite + TS，无 UI 库需手写组件）

### P1 对话级撤销
- 在每条 AI 消息气泡旁加「↺ 回到这里」按钮。
- 点击 → `POST /rollback {checkpoint_id: 该消息所属步}`。
- 成功后前端 localStorage/store 截断该消息之后的显示，并提示"已回滚到这一步，可继续聊"。
- **体验要点**：回滚后用户能接着发新消息，新消息从该步继续（靠软回滚指针移动）。

### P2 调试重放
- 会话侧栏加「时间轴」面板，调 `GET /history` 渲染步骤列表（step / 时间 / 末条预览）。
- 点某步展开：`GET /threads/{id}/state?checkpoint_id=`（需扩展现有 `get_state` 接受 `checkpoint_id` 参数）→ 显示该步完整 `AgentState`（messages / gate_state / decision / next）。
- 「▶ 重跑这步」按钮 → `POST /replay`。

### P3 分叉实验
- 时间轴某步「⑂ 另开一条」→ `POST /fork` → 跳转新会话（新 thread_id），原会话保留。
- 会话列表需能区分"主线 / 分叉线"（可加 `forked_from` 元数据，落会话索引）。

---

## 6. 边界与坑（必须处理）

1. **HITL 挂起复活**：若回滚/分叉的目标步之后存在一个 pending approval，旧审批登记（`get_hub().clear_thread(ck)` 维护的共享表）必须清理，否则会复活一个已失效的审批。复用 `reset_thread` 里的 `clear_thread` 逻辑，在 rollback/fork 跨越 pending 步时调用。
2. **`messages` 截断语义**：见 §2 V1。若 `aupdate_state(target, {})` 实测不支持"纯指针移动"，退化方案为 `fork` 到新 thread（保留原线），UI 上等同回滚。
3. **gate_state / turn / context_state 一致性**：这些字段随 checkpoint 整体快照持久化。软回滚会一并恢复（正确行为——回滚就该恢复全部状态）。replay 会基于恢复后的状态重跑。
4. **并发写**：checkpointer 是 SQLite 单例。rollback/fork 是写操作，沿用现有 `busy_timeout=30000` + WAL 兜底；同一 thread 的 rollback 与正在进行的 `ainvoke` 需串行（前端在回滚期间禁用输入框）。
5. **与现有 `reset_thread` 共存**：reset = 全删（救卡死）；rollback = 精细移动。两者不冲突，文档需向用户说明差异。
6. **prompt_messages 窗口**：回滚后该步的 `prompt_messages` 是那刻的折叠窗口；继续对话时 `ContextManager` 会重新计算，无需特殊处理。

7. **⚠️ 外部副作用不回滚（所有 mode 通用，coding/mission 尤甚）**：checkpoint 只持久化 graph 内存态（`messages` / `gate_state` / `turn` / `context_state` 等），**不管理工具产生的外部副作用**。若某步之后模型通过 `write_file` 改了磁盘文件、发了网络请求、或写入了外部存储，回滚 checkpoint **不会撤销这些已发生的副作用**——回滚后对话状态"看起来"回到了那步，但磁盘/外部系统已是新状态。这是 LangGraph checkpoint 的固有限制。文档/UI 须向用户明示此边界（尤其 coding/mission 写文件场景），避免"回滚了但文件没变"的困惑。如需真·副作用回滚，需额外做 command/saga 补偿模式（超出本期范围，列为后续方向）。

   **实测补充（P0 验证发现，2026-07-23）**：软回滚（`aupdate_state` 移动指针）**会保留**目标步之后的旧 checkpoint 在 sqlite 中（这是 time-travel「可再前进」的设计收益）。但这带来一个微妙点——被保留的历史仍可能被模型"感知"到：
   - 经实测，**会话 `messages` 的截断是正确且干净的**：回滚到第一轮末后 `get_state` 确实只剩 2 条消息，回滚点之后的内容（如"城市=北京"）**不在会话消息里**。
   - 但模型回复仍可能"提及"已回滚事实。排查确认这**不来自**会话 checkpoint（已截断）、**也不来自**长期记忆 vault（`~/.agent-harness/{user}/memory_vault/` 的 daily/index/dream 经 grep 确认无该事实）——属真实 LLM 在 `memory_search` 返回空时的自行补全（confabulation），或极少数情况下 `auto_memory` 在回滚前已写入 vault 的外部副作用。
   - **结论**：回滚对 graph 状态的还原是正确的；"模型仍说出旧事实"属 §6.7 外部记忆类，非回滚 bug。若要强隔离，需额外在 rollback 时一并 `get_hub().clear_thread` + 清理/重建该 thread 的 memory_vault 索引（即"硬回滚"语义），但会与 time-travel 保留历史的设计目标冲突，列为后续可选方向。UI 提示文案应说明：回滚还原对话，但模型凭长期记忆/自身补全可能仍提及旧信息。

8. **⚠️ `checkpoint_ns` 必须随 `checkpoint_id` 显式提供（P0 实测踩坑，2026-07-23）**：LangGraph 的 `AsyncSqliteSaver.aput` / `aupdate_state` 写入路径用 `config["configurable"]["checkpoint_ns"]` **方括号直取**（非 `.get` 容错），当 config 带了 `checkpoint_id` 却缺 `checkpoint_ns` 时直接 `KeyError: 'checkpoint_ns'`。而 `aget_state`（读路径）用 `.get` 容错，所以 `/state` 不报错、`/rollback` 却挂——极易误判。
   - **修复**：所有携带 `checkpoint_id` 的 config（rollback 的 target_cfg、fork 的 target_cfg 与新 thread cfg、replay 注入的 config、以及 `get_state` 扩展路径）统一补 `"checkpoint_ns": ""`。普通聊天（仅 `thread_id`）由 Pregel 自动补 `checkpoint_ns`，无需手动加。
   - **影响面**：此坑同时存在于 `aupdate_state` 与带 `checkpoint_id` 的 `astream`（replay 走此路径），漏掉任一都会 500。已在 `app.py` 四处补齐并通过端到端验证。

---

## 7. 实施计划（分阶段）

| 阶段 | 内容 | 改动面 | 风险 |
|---|---|---|---|
| **P0** | 后端三原语：`/history` `/rollback` `/fork` `/replay` + 扩展 `get_state` 接受 `checkpoint_id` | 仅 `app.py` | 低 |
| **P1** | 前端「回到这里」按钮 + 截断显示 | 前端组件 + `app.py` | 中（前端状态同步） |
| **P2** | 时间轴面板 + 步骤快照查看 + replay | 前端为主 | 中 |
| **P3** | 分叉实验 + 会话树元数据 | 前端 + 会话索引 | 高（多线管理） |

**建议起点**：P0 + P1（最快见效、用户感知最强、风险最低），P2/P3 复用同一套原语。

---

## 8. 验证策略

- **单测（后端）**：用内存 `MemorySaver` 跑一个 3 步 graph，断言 `aget_state_history` 返回 3 步；`aupdate_state(step2_cfg, {})` 后 `aget_state` 的 `values` == step2 状态；`fork` 后新 thread 状态 == step2 且原 thread 不变。
- **集成（端到端）**：真实后端起 `qwen3.6-plus`，聊 3 轮 → `/history` 应见 3+ 步 → `/rollback` 到 step1 → 再发一条 → 确认回复从 step1 语境继续、无幻觉身份 → `/fork` 开新线确认原线保留。
- **HITL 回归**：回滚跨越 pending approval 步时，确认旧审批不再复活（用 coding 模式触发一次审批后回滚验证）。

### 8.1 P0 端到端验证结果（2026-07-23，真实后端 PID 52173 @ :8123，demo 模型）

全部 4 端点 + `get_state(checkpoint_id)` 已用真实 HTTP 调用验证通过：

| 端点 | 验证动作 | 结果 |
|---|---|---|
| `GET /history` | 3 轮聊天 → 返回 15 个 checkpoint 步骤（step -1→13），`n_messages` 正确映射到用户轮次（2/4/6） | ✅ |
| `POST /rollback` | 回滚到第一轮末（n_messages=2），返回 `ok`；`get_state` 确认仅剩 2 条消息 | ✅ |
| 续聊（回滚后） | 回滚点后续聊，`get_state` 显示 4 条（2 回滚 + 1 人类 + 1 AI），新消息 append 到回滚点之后 | ✅（V3 通过） |
| `POST /fork` | 从第一轮末分叉出新 thread `e2e_rb-fork-xxxx`，新线 2 条消息；原 thread 仍 4 条完整保留 | ✅（V2 隔离 + 原线保留通过） |
| `POST /replay` | 带新 input 从 checkpoint 重放，SSE 返回 `metadata` + 流式 `messages`，后端日志 `STREAM_DONE` | ✅ |
| `get_state(checkpoint_id)` | 扩展路径正常返回指定步状态 | ✅ |

**修复记录**：验证中发现并修复 `KeyError: 'checkpoint_ns'`（见 §6.8），补强 4 处 config 后全绿。验证用测试 thread（`e2e_rb` / `e2e_clean` 等）残留在 `~/.agent-harness/checkpoints.sqlite`，不影响功能，可随时手动清理。

---

## 9. 开放问题 / 原型期验证点

- **V1（已验证 ✅）**：`aupdate_state(target_cfg, {})` 软回滚语义如 §2 所述（生成指向目标状态的新 checkpoint、不丢历史、移动指针）。真实后端实测通过。
- **V2（已验证 ✅）**：`aget_state_history` 在 `AsyncSqliteSaver` 单例 + 复合键 `user:thread` 下返回正确，fork 后原线保留、新线独立。
- **V3（已验证 ✅）**：前端"截断显示"语义等价于后端"指针移动"——回滚后新消息 append 到回滚点之后（实测 n_messages 2→4，非回到旧末端）。
- **Q（待用户确认）**：是否需要"回滚也回到指定步之后的某条具体消息"（更细粒度）？还是按 checkpoint step 粒度足够？→ 初版按 step 粒度，前端可在 P1/P2 按 `n_messages` 映射成"用户轮次"展示。
- **新开放点**：`checkpoint_ns` 缺失导致 500（§6.8，已修）。是否需要"硬回滚"（删除目标步之后 checkpoint + 清理 memory_vault 索引）以强隔离模型记忆？→ 列为后续可选方向，与 time-travel 保留历史目标权衡。
