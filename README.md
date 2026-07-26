# agent-harness

基于 **LangGraph 自建 StateGraph** 的 Agent harness。把 **Loop Gates / StopHandler / 上下文管理 / 治理 / 长期记忆** 用图节点重新实现成可落地、可观测、可崩溃恢复的稳定版本。

核心设计取舍：

- **Governor 是真·图节点**，跑在每轮"模型调用 + 工具执行"的干净边界上；STOP 在 step 边界直接生效，无需延迟落槌的 hack。
- **上下文管理**用 fold-not-summarize（零丢失），独立成 `context/` 模块；原始消息全文写穿 SQLite，折叠只是占位桩，召回即还原。
- **治理**用 `PolicyGuardedTool`（工具内守卫，拒绝即不放行）+ 工具前 `ApprovalGate`（人工裁决，真 `interrupt()`）。
- **长期记忆**是独立的语义保险库（markdown vault + 混合检索），与上下文窗口、核心文件 persona 是三层不同职责。

> 为什么不直接用 `create_deep_agent`？对照本地源码发现：它的 `create_deep_agent` 不构建 `StateGraph`，返回已编译图、builder 不可见，**无法插入 Governor 节点**。所以借它的"零件"，握自己的"图"。

## 稳定版拓扑

```
START → context → agent → (有 tool_calls? approval : governor)
                       │
        approval ──ASK──► hitl ──resume──► tools
            │continue                         │
            ▼                                 ▼
          tools ─────────────────────────► governor
                                              │
                          ┌──────────────────┼───────────────────┐
                       STOP(END)          CONTINUE              ASK
                                                  └────────────► hitl
```

- **context**：`ContextManager` 在每轮模型调用前折叠/召回，产出 `prompt_messages` 窗口（原始 `messages` 永不折叠，仅作事件日志）。
- **approval**：工具执行前拦截敏感工具，触发 ASK → `interrupt()`。
- **governor**：运行全部 Loop Gates，STOP / CONTINUE（续跑桩）/ ASK。

## 目录结构

```
src/harness/
  state.py            # AgentState（messages / prompt_messages / turn / gate_state / context_state / decision / pending_after_hitl）
  gates/              # Loop Gates（零依赖，可单独单测）
    base.py           # StopAction / StopHandlerResult / StopGate / run_stop_gates
    iteration.py      # IterationGate（硬迭代上限，priority=10）
    doom_loop.py      # DoomLoopGate（滑窗相似度防鬼打墙，priority=70）
    budget.py         # BudgetGate（token 预算硬闸门，priority=20）
    rubric.py         # StandaloneRubricGate（防过早收工，priority=90）
    file_loop.py      # FileLoopGate（磁盘状态文件持久化循环，priority=60）
  governor.py         # Governor 节点 + route_after_governor
  agent.py            # agent 节点（模型调用 + 重试/熔断 + 用 prompt_messages）
  agentmode.py        # 模式系统（ModeSpec / chat|coding|mission 门集合与提示）
  tools_node.py       # tools 节点（等价 ToolNode；错误回传模型；结果外置）
  context/            # 上下文管理（fold-not-summarize，零丢失）
    store.py          # TurnStore：SQLite 写穿式，原始消息全文落库
    manager.py        # ContextManager：预算触发折叠 / 召回 / rollback 同步
    recall_tool.py    # recall 工具（沙箱门控）
  security/           # 治理与安全（防御纵深）
    guard.py          # ToolGuardEngine + FilePath/RuleBased/ShellEvasion guardian
    guarded_tool.py   # PolicyGuardedTool：工具执行前过守卫，拒绝即不放行
    approval.py       # ApprovalGate：工具前敏感工具拦截 → ASK
  stability/          # 稳定性底座
    retry.py          # with_retry（指数退避）+ CircuitBreaker（熔断）
    observability.py  # Metrics（轮次/门触发/折叠/召回/token/错误）+ 结构化日志
    modes.py          # MissionGate：任务级门控（读 prd.json 完成标记 / 硬上限）
  models.py           # make_deepseek_model（OpenAI 兼容，env 驱动）+ ScriptedModel（确定性验证）
  reasoning_model.py  # ReasoningChatOpenAI：自实现 OpenAI 兼容流，捕获 reasoning_content
  graph.py            # build_graph()：完整拓扑装配
src/server/
  app.py              # FastAPI：Platform 兼容 REST + SSE 信封 + /config + 安全头 + 鉴权/限流
  graph_provider.py   # 编译图单例：模型 + 工具 + ContextManager（按用户隔离与热重建）
  config.py           # RuntimeConfig 单例：热加载 + Fernet 加密落盘（config/runtime.json）
  context_config.py   # ContextManager 配置（按用户隔离，持久化）
  memory.py           # 长期语义记忆：MemoryVault + 混合检索 + auto_memory + dream
  prompt_contributors.py # 可插拔系统提示管线（PromptManager + 贡献器）
  core_files.py       # 核心文件层：PROFILE/SOUL/AGENTS/MEMORY 等 persona markdown
  scheduler.py        # 定时任务 / CronManager（APScheduler 后台线程）
  plugins.py          # 配件管理路由聚合（Files/Tools/Skills/MCP/Agents/Cron/Memory/Context）
  approval_hub.py     # 审批中枢（PendingApproval + 跨 worker 共享 SQLite）
  approval_watchdog.py# 未决审批自动过期回收
  inbox_store.py      # 收件箱事件存储
  token_usage.py      # Token 用量统计（非阻塞）
  model_discovery.py  # 模型厂商目录 + 发现/校验
  ratelimit.py        # 按 IP 固定窗口限流
  settings_api.py     # /settings/* 设置中心路由
  auth.py             # 服务级鉴权（Bearer / x-api-key）
frontend/
  src/App.tsx / src/panels/* / src/index.css   # React + 官方 useStream
tests/                # gates / context / security 单测
examples/             # run_minimal.py / run_stable.py / run_deepseek.py
```

---

# 模块设计详解

## 1. 主图与状态（graph.py / state.py）

### 1.1 状态结构 `AgentState`
定义在 `state.py:18`，各 channel 的 reducer 语义决定了整个系统的可恢复性：

- `messages: Annotated[list[BaseMessage], add_messages]`（`state.py:20`）：按 message id 合并、**append-only 的原始事件日志，永不折叠**。这是"时间旅行"的基础——checkpointer 按 `thread_id` 存全部 `messages` 增量。
- `prompt_messages: list`（`state.py:22`）：`ContextManager` 折叠后产出的"发送窗口"，整列表替换（非 `add_messages`）。
- `turn: int`（`state.py:24`）：循环轮次，每过一次 governor 自增 1。
- `gate_state: dict`（`state.py:26`）：`{gate_name: {...}}`，随 `thread_id` 由 checkpointer 持久化（会话隔离）。**gate 内部状态进图状态而非类实例字段**，进程崩溃可恢复。
- `context_state: dict`（`state.py:28`）：`ContextManager` 内部状态（折叠序号等）。
- `decision` / `stop_reason` / `pending_after_hitl` / `hitl_decision` / `mode`（`state.py:30-38`）。

### 1.2 图装配
`build_graph(...)`（`graph.py:325`）是总入口，节点集合为（`graph.py:358-384`）：
`context → agent → (approval|governor)`，`approval → (tools|hitl)`，`tools → governor`，`governor → (context|hitl|END)`，`hitl → (tools|context|END)`。

条件边路由：`should_continue`（`graph.py:314`）、`route_after_approval`（`graph.py:299`）、`route_after_hitl`（`graph.py:303`）、`route_after_governor`（`governor.py:79`）。

### 1.3 关键实现细节
- **`agent_node` 合并流式 chunk 成 `AIMessage`**：`make_agent_node`（`graph.py:163`）在 `graph.py:185-196` 把多个 `AIMessageChunk` 累加后显式构造 `AIMessage`（带 `tool_calls`），使前端 `values` 事件能识别 `type='ai'`。
- **拒绝时补「已拒绝」ToolMessage**：HITL 拒绝且 `pending=="tools"` 时，为每条被拒 `tool_call` 合成 `ToolMessage`（`graph.py:278-293`），否则真实模型下轮因缺工具结果报错。
- **`prompt_messages` 与 `messages` 分离**：context 节点只写 `prompt_messages`（折叠后窗口），不覆盖 `messages`（`graph.py:158`），保证前端看到完整历史。
- **`hitl_node` 内联 `await` Future**：在工具执行前原地 `await hub.wait(pa)`（`graph.py:266`），同一执行流继续，不另起 `Command(resume=...)` 重放（避免重复消耗 token）。
- **多用户隔离在节点内现取**：图按 `(model,mode)` 编译期缓存为全局单例，但记忆/上下文不能跨用户共享，故 `context_node` 内按 `config["configurable"]["user_id"]` 调 `get_context_manager/get_memory_manager`（`graph.py:90-99`）。
- **`auto_memory` 走单线程后台 executor**：`mm.auto_memory` 调 LLM 提取事实并重建索引，同步执行会阻塞 context 节点数十秒，故 `_MEMORY_EXECUTOR = ThreadPoolExecutor(max_workers=1)` 异步提交（`graph.py:36`、`graph.py:148`）。

---

## 2. 模式系统（agentmode.py）

把 `{gates, 工具白名单, 系统提示}` 按模式打包成 `ModeSpec`（`agentmode.py:94`，字段 `name/description/system_prompt/gate_factory/tool_allow`），供 `build_graph` 检索装配。

- **三种模式**：`chat` / `coding` / `mission`，由 `get_mode`（`:172`）、`resolve_gates(mode, opts)`（`:176`）、`list_modes()`（`:166`）驱动。
- **迭代上限按模式递增**：chat 30 / coding 40 / mission 60（`agentmode.py:107/115/125`），任务越重容忍越多轮。
- **`_CORE` 行为契约**（`:34`）：一段可执行的行为准则（先动手 / 闭环交付 / 自行纠错 / 诚实边界），让真实模型主动推进。
- **chat 提示词禁止自创身份**（`:72`）：明确"不要声称把内容写进 PROFILE.md、chat 只读"——避免模型越权宣称写文件。
- **`StandaloneRubricGate` 被刻意移除**（`agentmode.py:118-120`）：实测它在工具调用后疯狂重复触发（最多 5 次）导致卡顿，防偷懒收益不抵代价。
- **mission 可选挂 `FileLoopGate`**：仅当 `opts["loop_dir"]` 存在时追加（`:136-137`），以磁盘状态文件做持久化循环真相源。
- **`chat` 模式只读**：`_writes_long_term_memory(mode)`（`graph.py:74`）在 chat 下跳过 `auto_memory` 写（见 §6），避免闲聊产生副作用与响应阻塞。

---

## 3. 模型层（models.py / reasoning_model.py / agent.py）

### 3.1 工厂 `make_deepseek_model`（`models.py:77`）
按厂商/模型名返回正确的 `ChatOpenAI` 或 `ReasoningChatOpenAI`，差异化注入 reasoning / thinking 参数：

- provider 差异化避免把未知参数塞给不支持的模型：`deepseek` 不接受 `thinking` 字段；`o1/o3` 用 `reasoning_effort`；`qwen3/qwq` 用 `reasoning={type:enabled}`（`models.py:13-42`）。
- reasoning 模式 `temperature` 强制 1.0（`models.py:133`）。
- `max_retries=2` + `timeout=120`（`models.py:137-138`），避免 openai SDK 默认 600s 干等。
- `ScriptedModel`（`models.py:159`）：确定性脚本模型，无 key 全链路测试用（`.step` 属性暴露脚本游标，测试可断言执行到第几步，`models.py:183`）。

### 3.2 `ReasoningChatOpenAI`（`reasoning_model.py:74`）
为什么自实现而非官方 `ChatOpenAI`：`langchain-openai 1.3.x + openai 2.44.0` 静默丢弃 `reasoning_content`，故直接用 `httpx` 流原始响应并转成 `AIMessageChunk(additional_kwargs={"reasoning_content":...})`。

- **合并 system 消息为一条**：Qwen/dashscope 兼容接口拒绝历史中间的 system 消息（如 rubric 续跑提示、context 折叠桩），合并避免 400（`reasoning_model.py:110-125`）。
- **`enable_thinking` 双向控制**（`reasoning_model.py:136-143`）：reasoning 模式开 `{reasoning:{type:enabled}, enable_thinking:true}`；否则显式 `enable_thinking=False`，关掉 qwen3.x 默认后台推理拖慢耗时。
- **tool_calls 增量去重**（`reasoning_model.py:202-219`）：必须 `id+name+arguments` 均为有效 JSON 才 emit，且用 `emitted_indices` 去重，否则下游重复执行工具 / 参数串损坏。
- **`_astream` 韧性重试**（`reasoning_model.py:262-305`）：`max_attempts=3`，仅重试"尚未产生任何 token"的连接级错误；一旦 `yielded=True` 则保留部分结果不再重试（修复"空气泡"重复输出）。

### 3.3 Agent 节点 `AgentNode`（`agent.py:94`）
把底层 LLM 包装成 LangGraph 可识别的 `BaseChatModel`，支持 token 级流式、重试降级、max_tokens 注入与 reasoning 归一化。

- 继承 `BaseChatModel` 而非直接调 `ChatOpenAI`：让 LangGraph 在 `stream_mode="messages"` 下直接捕获 token 级事件，前端实时看到思考与答案逐字出现（`agent.py:3-7`）。
- **流式直代理、非流式带重试**：`_stream/_astream` 直接代理底层（`agent.py:210-238`），仅 `_generate/_agenerate` 走重试（3 次指数退避，`_call_with_retry` `:267`），避免流式路径二次包装破坏增量。
- **`max_tokens` 仅 chat 注入**：`CHAT_MAX_TOKENS = 1500`（`graph.py:44`，原 300 会腰斩正常问答）。`make_call_model`（`agent.py:286`）在 `mode=="chat"` 时传 1500，其余 `None`（`graph.py:350`）→ `AgentNode.max_tokens`（`agent.py:110`）→ `_bind_tools` 注入 `bind_tools(..., max_tokens=1500)`（`agent.py:179`）。coding/mission 不设上限。
- **错误分类 `_classify`**（`agent.py:69`）：`timeout/429/503/connection` 归 `TransientError` 触发重试；`invalid/not found/unsupported` 归致命直接快速失败，避免无效空等。

---

## 4. 循环安全阀 / Loop Gates（gates/* / governor.py）

### 4.1 抽象（`gates/base.py`）
零依赖（不 import langgraph/langchain，可脱框架单测）：

- `StopAction(str, Enum)`（`base.py:18`）：`CONTINUE/STOP/ASK`。
- `StopGate`（`base.py:32`）：`name` / `priority` / `check(turn, state, gate_state)`。
- `run_stop_gates(gates, turn, state, gate_state)`（`base.py:53`）：**priority 升序短路**——第一个非 `None` 结果生效，后续门不再评估（`base.py:60-66`）。硬安全阀（低 priority）永远先说话。
- **门状态外置到 `gate_state`**：子类把内部状态写传入 dict，随 graph state 持久化（会话隔离）。

### 4.2 各门

| 门 | 文件 | priority | 职责 |
|---|---|---|---|
| `IterationGate` | `iteration.py:10` | **10**（最高） | 轮次到顶必须 STOP，整个 harness 的最后安全网 |
| `BudgetGate` | `budget.py:32` | 20 | token 预算硬闸门（默认 300k，mission 500k）；达 0.8 先 CONTINUE 提醒一次 |
| `DoomLoopGate` | `doom_loop.py:17` | 70 | 滑窗 md5 指纹检测"鬼打墙"；相似度 ≥0.85 STOP，≥0.6 且 warns<2 CONTINUE 警告 |
| `FileLoopGate` | `file_loop.py:19` | 60 | 磁盘 `DONE` 哨兵 / `status.json` 为真相源的长任务循环，进程重启可续跑 |
| `StandaloneRubricGate` | `rubric.py:83` | 90（最低） | 任务进行中模型却以纯文本收工、未达验收时 CONTINUE 催其继续；`max_interventions=3` 限流 |
| `MissionGate` | `stability/modes.py` | 90 | 任务级门控，读 `prd.json` 完成标记 + 硬上限 |

- **`DoomLoopGate` 细节**：相似度基于"跨轮累积的最近若干步指纹"而非本地 messages 条数（`doom_loop.py:55`）；`history` 仅保留最近 `window=6` 条（`doom_loop.py:52-53`）；`total<=1` 相似度返回 0（`doom_loop.py:44-46`）单步不误判。
- **`BudgetGate` 细节**：`_estimate_tokens`（`budget.py:16`）用字符数/4 启发式并计入 `tool_calls` JSON（`budget.py:27-28`）；软警告仅触发一次（`gate_state["warned"]`，`budget.py:49-50`）。
- **`FileLoopGate` 细节**：`check` 只在 `loop_dir` 设置时起作用，未设置则 `_is_complete` 返回 False，`max_turns` 兜底（`file_loop.py:36-37`）。

### 4.3 Governor 节点（`governor.py:27`）
在"模型 + 工具"整步边界上跑 Loop Gates，按 `StopAction` 更新状态并决定继续/停止/人工裁决。

- `turn = state.get("turn",0)+1`（`governor.py:31`）；`gate_state` **复制一份**避免就地改 state（`:33`，LangGraph 要求返回增量）。
- **CONTINUE 注入续跑提示**（`governor.py:56`）：当门判定任务未完成但模型想收工，注入 `SystemMessage` 逼其换思路（原始日志保留）。
- **ASK 设 `pending_after_hitl="agent"`**（`governor.py:70`）：工具后裁决恢复后重新评估，与工具前审批（`approval_node` 设 `"tools"`）区分。
- **默认逻辑**：模型给"最终答案"（无 tool_calls）→ stop；否则刚跑完工具 → continue（`governor.py:39-47`）。

---

## 5. 上下文管理（context/manager.py / store.py）★

这是本项目的核心创新点之一：**fold-not-summarize（折叠而非摘要）**——原始消息全文落库永不丢失，超预算时从最旧往新折叠成占位桩，并提供 agent 显式 `recall` 还原全文的能力。

### 5.1 两层存储
- **`TurnStore`（`store.py:18`）**：SQLite 写穿式持久化每个 thread 的原始消息全文（`schema` `store.py:36-45`，`seq` 1-based，`idx_turns_thread_seq` 索引）。`all(thread_id)` 返回 `[{seq, msg}, ...]` 按 seq 升序（`store.py:68`）。**`clear` 是真删除**（`store.py:88`），折叠原文一并丢失且不可 recall——文档需提示用户 clear 的不可逆性。
- **`ContextManager`（`manager.py:90`）**：维护折叠窗口与召回能力，复用 `TurnStore` 作真相源。

### 5.2 构造参数即上下文预算（`manager.py:91-110`）
| 参数 | 默认 | 含义 |
|---|---|---|
| `budget_tokens` | 8000 | 软预算，超则折叠 |
| `reserve_ratio` | 0.2 | 最近窗口保留区比例 |
| `hard_stop_tokens` | 300000 | 硬停兜底上限 |
| `strip_media` | True | 剥离历史 base64 媒体 |
| `recent_tool_result_chars` | 8000 / `old_tool_result_chars`=2000 / `recent_tool_window`=4 | 工具结果分层裁剪 |
| `max_tool_result_chars` | 0 | 历史字段，>0 走扁平兜底覆盖分层 |
| `enable_semantic_recall` | True | 语义召回开关 |

### 5.3 折叠策略 `_fold`（`manager.py:167`）
- **tool_calls 配对保护**（`manager.py:171-190`）：把"一个 `ai` 消息的 `tool_calls` + 紧随其后的所有 `tool` 消息"作为一个 **block** 整体折叠，绝不允许把 assistant 的 tool_call 和其 ToolMessage 拆开——否则破坏 OpenAI 消息配对会导致模型 400。
- **保留区 + 硬停双闸门**（`manager.py:194-222`）：先按 `reserve_tokens = budget*reserve_ratio` 从最新 block 往回保底；再在软预算内尽量容纳旧 block；若仍超 `hard_stop_tokens` 继续折最旧。单条最新消息永远在保留区（从最新往前累计），保底可见。
- **折叠桩形态**（`manager.py:283-311`）：`_make_map_stub` 生成 `SystemMessage`，content 形如：
  ```
  [CONTEXT FOLD] 2 segment(s), 15 msgs, ~3200 tok folded to save context.
    - [turns 1-8] (8) ## 14:03:21 UTC ...
    - [turns 12-18] (7) User: ...
  Call recall(seq=N) to restore a specific turn, or recall("keyword") to search.
  ```
  并通过 `additional_kwargs={"kind":"fold_stub"}`（`manager.py:311`）标记，准备阶段永不二次折叠。

### 5.4 回退（rollback）后消除泄漏 ★关键修复
`_sync_store`（`manager.py:150`）**每次都 `clear(thread_id)` 再按当前 `raw_messages` 全量重建**，seq 取 `enumerate(raw, start=1)` 的 1-based 下标。若只追加（旧 `_persist_new` `manager.py:158`），已回退的旧消息会残留在内存 store 中被折进窗口发给模型，表现为"明明回退了，AI 却还聊起回退点之后的事"。

### 5.5 召回 `recall`（`manager.py:479`）
`seq` 是 1-based 下标（`manager.py:153-155`），与 `recall(seq=N)` / `_eviction_index` 契约一致。语义召回降级链（`manager.py:479-589`）：
1. 精确 `seq=` → TurnStore 取全文
2. token 直取（外置 blob，需 `.txt` 且含 `/`）
3. embedding top-k（可用时，`_register_eviction` 缓存 embedding `:265-281`）
4. keyword 子串
5. 外置 blob 关键词搜索（`tool_result_store.search_thread`）

任一步命中即返回，全失败才报无匹配。**召回沙箱门控**：`allow_unsandboxed_recall` 由构造参数与 env `ALLOW_UNSANDBOXED_RECALL` 双重决定，未开启时 `recall` 直接拒绝——避免自动无差别召回泄露折叠区。

### 5.6 `prepare` 数据流（`manager.py:412`）
由图上下文节点调用（`graph.py:138`）：① `set_active_thread` → ② `_sync_store` 重建 store → ③ `_fold` 得 window + 被折 seqs → ④ 可选 `_strip_media`（`manager.py:314`，只改窗口副本，`_store` 原文保持完整） → ⑤ 可选 `_prune_tool_results`（`manager.py:352`，分层裁剪历史工具结果，外置占位符跳过） → ⑥ 若有 `system_hint` 插到最前（`manager.py:421-428`，即便折叠桩也要让真正的系统提示在最前）。返回 `(window, {"folded_seqs":[...], "fold_count":N})`。

### 5.7 配置（`context_config.py`）
`ContextManagerConfig`（`context_config.py:53`）持久化到 `DATA_HOME/{user_id}/context_config.json`。**配置与运行时环境变量叠加**：`graph_provider.get_context_manager`（`graph_provider.py:1010`）构造时预算可被 env `CONTEXT_BUDGET` 覆盖、 `enable_recall` 可被 `ALLOW_UNSANDBOXED_RECALL` 覆盖（`graph_provider.py:1022-1038`），即"配置持久化 + 环境变量热覆盖"双层。

---

## 6. 长期语义记忆（server/memory.py）★

与上下文窗口（§5）、核心文件 persona（§9）是**三层不同职责**的记忆架构：

| 层 | 模块 | 存储 | 写入时机 | 读取时机 |
|---|---|---|---|---|
| 核心文件层 | `core_files.py` | 工作区 6 个 .md | 用户/agent 主动写 | 每次 `build_system_prompt`（priority 20） |
| 上下文层 | `context/manager.py` + `store.py` | SQLite turns + 折叠桩 | 每轮写穿 | `prepare` 折叠；`recall` 还原 |
| 长期语义层 | `memory.py` | markdown vault + `index.json` | `auto_memory`(非 chat)/`dream` | `memory_search` 注入提示 / agent 显式搜 |

### 6.1 保险库结构
`DATA_HOME/{user_id}/memory_vault/`（`memory.py:315-317`）含三个子目录：`daily/`（auto_memory 写入的每日笔记）、`digest/`（摘要，预留）、`dream/`（整合记忆 `interests.md`）。

**`index.json` 结构**（`memory.py:335-338`）：
```json
{"chunks": [
  {"path": "daily/2026-07-26.md",
   "text": "## 14:03:21 UTC ...",
   "tokens": ["extracted", "fact"],
   "embedding": [0.12, -0.03, ...] | null,
   "source_hash": "sha256..."}
]}
```
每个 chunk 带 `source_hash`（内容哈希），`_update_index_for_rel`（`memory.py:340`）用它做增量更新——文件未变化直接跳过，避免重算嵌入；`embedding` 可能为 `null`（嵌入后端不可用或尚未 backfill）。

### 6.2 混合检索 `hybrid_search`（`memory.py:507`）
同时跑 BM25（`_bm25` `:480`，参数 `k1=1.5, b=0.75`）与向量余弦，再用 **RRF 融合**（`k=60`，`memory.py:525-528`）。BM25 的 `tokens` 预存在 chunk 里（`memory.py:369`），`_bm25` 直接复用避免每次检索重新分词。

### 6.3 嵌入客户端 `EmbeddingClient`（`memory.py:181`）
只用标准库 `urllib` 打 OpenAI 兼容 `/embeddings` 接口（`memory.py:209-226`），支持 openai/dashscope/gemini/ollama 等 backend，带 LRU 缓存（`max_cache_size` 默认 3000，`memory.py:90`）。

### 6.4 写入时机与副作用
- **`auto_memory`（`memory.py:600`）**：按 `auto_memory_interval`（默认 10，`memory.py:106`）逐 thread 计数，每满 N 轮写一次每日笔记；`_extract_facts`（`memory.py:621`）优先用 DeepSeek 抽取（无 key 时回退为"把用户陈述直接列要点"，`memory.py:646-650`）。**只在非 chat 模式触发**（见 §2 `_writes_long_term_memory`）。
- **`dream`（`memory.py:684`）**：取最近 7 个 daily 笔记整合进 `dream/interests.md`。
- **`memory_search`（`memory.py:586`）**：由 `MemoryContributor` 在系统提示管线中注入（§9）。
- **配置单一事实源**：`runtime_memory_config`（`memory.py:150`）从 `runtime.json` 的 `running` 段构造；`memory_config.json` 仅作向后兼容镜像。

### 6.5 非显而易见细节
- **索引 backfill**（`memory.py:349-355`）：即使文件未变化，只要旧 chunk `embedding is None`（之前后端不可用），仍会重新嵌入——后端就绪后自动补全向量。
- **`auto_memory` 的 thread 安全计数**（`memory.py:571`、`607-611`）：`_turn_count` 是 `{thread_id: int}` 字典，用 `self._lock` 保护；`tid` 默认兜底 `"default"` 防止直接调用崩溃。
- **`get_memory_manager` 缓存 + `none` 短路**（`memory.py:783`）：`backend=="none"` 直接返回 `None`，此时图不注册 `memory_search`、不触发 `auto_memory`。

---

## 7. 工具与工具结果（tools_node.py / tool_result_store.py / security/guarded_tool.py）

### 7.1 工具节点 `make_tools_node`（`tools_node.py:68`）
执行上一轮 `AIMessage` 的 `tool_calls`，结果包成 `ToolMessage` 追加。

- **工具错误回传模型而非中断**（`tools_node.py:104-110`）：`ToolException`→`Blocked:`，其它 `Exception`→`Error:`，让模型自行纠错，符合"闭环交付"。
- **未知工具名 → `Error: unknown tool {name}`**（`tools_node.py:95-96`）而非崩溃。
- **`tool_call_id` 必须回填**（`tools_node.py:122`）：每条 result 用 `ToolMessage(content=..., tool_call_id=tc["id"])` 对应，否则模型悬空。
- **`prune_tool_result`（`tools_node.py:26`）**：head/tail 截断（保留头尾）。

### 7.2 结果外置 `ToolResultStore`（`tool_result_store.py:41`）
把超大工具结果全文落本地磁盘，上下文留占位符，模型经 `recall` 还原；按 thread_id 隔离，绝不丢原文。

- **外置而非截断**：阈值（`AGENT_TOOL_RESULT_THRESHOLD_KB=50` KB，`tools_node.py:80`）以下保完整 inline，以上全文落盘 `~/.agent-harness/tool-results/<thread>/<tool>_<uuid>.txt`（`tool_result_store.py:26`），占位符 `[TOOL RESULT EXTERNALIZED]`（`tool_result_store.py:20`）可 recall。
- **防目录穿越**：`recall` 用 `resolve()` 校验 token 落在 root 内（`tool_result_store.py:66-72`）。
- **占位符含原始长度**（`make_placeholder` `:136`）：`Original length: {length} chars` 让模型感知被省略体量。
- **单例惰性初始化**：`get_tool_result_store()`（`tool_result_store.py:107`）读 env 阈值。

### 7.3 守卫包装 `PolicyGuardedTool`（`security/guarded_tool.py:9`）
包装底层工具，执行前过 `ToolGuardEngine`，拒绝则抛 `ToolException`（被 `tools_node` 捕获为 `Blocked:`）。保持原名（`self.name = tool.name`，`guarded_tool.py:20`）否则模型按原名调用会找不到；透传 `args_schema`（`guarded_tool.py:23`）暴露正确工具 schema。

---

## 8. 安全与治理（security/guard.py / approval.py / approval_hub.py / approval_watchdog.py）

### 8.1 防御纵深 `ToolGuardEngine`（`guard.py:127`）
聚合多个 Guardian 对 `(tool_name, args)` 做策略校验，任一拒绝即拒绝（**fail-closed**，`guard.py:147-148`）：

- `FilePathGuardian`（`guard.py:42`）：阻止路径穿越/出允许根（默认 `["."]`），路径字段按 key 名匹配（含 `path`/`file`）。
- `RuleBasedGuardian`（`guard.py:65`）：危险工具名 + 明文危险命令模式（子串匹配 `rm -rf`/`mkfs`/`:(){` 等，`guard.py:87-89`）拦截。
- `ShellEvasionGuardian`（`guard.py:93`）：shell 元字符注入检测，但**跳过数据字段**（文件写入工具的 `content/old/new/code` 是代码/HTML，扫 shell 元字符会误杀，仅扫路径/控制字段，`guard.py:98-115`）。

### 8.2 工具前人工裁决 `ApprovalGate`（`approval.py:123`）
在 agent→tools 之间拦截敏感工具调用，触发 ASK 路由到 hitl 节点做真人工裁决。

- `SENSITIVE_TOOLS`（`approval.py:26`）：`write_file/edit_file/delete_file/exec/.../publish`。
- **信任模式 `trust_mode`**（`approval.py:8-14`）：开启后除破坏性命令外全部自动放行，避免编码/agent 任务每步被审批打断。
- **安全命令自动放行**（`approval.py:150-165`）：`ls/cat/python/npm/git status` 等只读/开发类直接放行，仅写盘/不可逆/危险命令拦截。
- **破坏性命令硬拦截底线**（`approval.py:46-60`）：`rm -rf`/`mkfs`/`dd`/`管道到 sh`/`git push --force` 等无论是否信任模式都要求裁决。
- **文件写入工具自动放行**（`approval.py:37-43`）：`write_file/edit_file` 已被 `FilePathGuardian` 限制在 workspace 根（沙箱），无需每步审批。
- **`_is_sensitive` 支持后缀匹配**（`approval.py:142-145`）：`name.endswith("__"+s)` 兼容子代理工具名。

### 8.3 审批中枢 `approval_hub.py`
在"工具执行前"阻塞点用进程内 `asyncio.Future` 实现原地超时审批，并持久化到共享 SQLite 以支持多 worker。

- **为什么用 Future 而非 `interrupt()`**（`approval_hub.py:6-10`）：`interrupt()` 需要从外部另起 `graph.ainvoke(Command(...))` 恢复，会重复消耗 token、可能触发新工具调用；而进程内 Future 让前端批准/拒绝 → `hub.resolve(request_id, decision)` → `future.set_result`，原图继续执行。
- **`PendingApproval`（`approval_hub.py:69`）**：`request_id = f"{thread_id}:{uuid4().hex[:12]}"`（`:167`），`timeout` 默认 300s（来自 `running.approval_timeout_seconds`）。
- **共享 SQLite**（`approval_hub.py:89`）：`DATA_HOME/approvals.sqlite`，每次 `request` 落盘；`resolve` 先更新共享表再 set 本 worker 的 future；跨 worker 由后台 poller 扫描共享表落到本 worker 持有的 future（`_poll_once` `:261`）。
- **`wait` 故意不捕获 `CancelledError`**（`approval_hub.py:183`）：否则会让 `graph_task.cancel()` 失效。

### 8.4 审批看门狗 `approval_watchdog.py`
在 `interrupt` 之上补"未决审批自动过期"语义（应对前端没弹窗/用户没点）：每 10s 扫描（`_REAPER_INTERVAL_SECONDS` `:37`），超 `approval_timeout_seconds`（默认 300s，下限 10s）调用注入的 `on_expire` 回调以"拒绝"恢复图。与 `approval_hub` 的进程内 future 超时是**两层超时**设计。

---

## 9. 系统提示管线与核心文件（prompt_contributors.py / core_files.py / graph_provider.py）

### 9.1 可插拔 `PromptManager`（`prompt_contributors.py:428`）
用一组"单一职责贡献器"按优先级拼装最终系统提示，取代硬编码的单一拼接函数。各贡献器（name / priority）：

| 贡献器 | priority | 职责 |
|---|---|---|
| `AgentIdentityContributor` | 10 | 占位（多 agent 身份预留） |
| `WorkspacePromptFilesContributor` | 20 | 调 `CoreFilesManager.build_system_prompt` |
| `ModeHintContributor` | 25 | 透传 `mode_spec.system_prompt` |
| `MultimodalHintContributor` | 40 | 占位（视觉能力门控） |
| `CodingModeContributor` | 45 | 编码模式提示 |
| `MemoryContributor` | 80 | 调 `memory_search` 注入长期记忆段 |
| `ScrollContextContributor` | 86 | 折叠/召回纪律说明 |
| `DriverPolicyHintContributor` | 88 | 占位（请求级 driver 策略预留） |
| `EnvContextContributor` | 90 | 当前时间/平台/thread（解决长会话不知当前时间） |

- **单贡献器异常被捕获跳过**（`prompt_contributors.py:471-473`）：一个坏块不拖垮整次提示装配。
- **读写分离**：`MemoryContributor` 只做"读/检索注入"，`auto_memory` 的"写"仍在图节点（后台线程）。
- **`build_sync` 输出**：各贡献器非空片段按 priority 升序用 `PROMPT_SEPARATOR` 拼接（`prompt_contributors.py:474-476`）。
- **`MemoryContributor` 空结果跳过**（`prompt_contributors.py:247`）：当 `memory_search` 返回 `"no relevant memory found"` 时返回 `None` 不污染提示。

### 9.2 核心文件层 `CoreFilesManager`（`core_files.py:518`）
管理工作区根目录下的六份 persona markdown（AGENTS / SOUL / PROFILE / BOOTSTRAP / HEARTBEAT / MEMORY），按启用顺序拼装成系统提示的"核心文件段"。

- `DEFAULT_ENABLED_FILES = ["AGENTS.md","SOUL.md","PROFILE.md"]`（`core_files.py:40`）。
- **frontmatter 剥离**：启用文件里的 YAML frontmatter 在拼提示时丢弃（`core_files.py:641-644`），只把正文喂给模型。
- **模板幂等补齐**（`core_files.py:575-578`）：`get_core_files_manager` 单例创建时调 `initialize_templates("zh")`，仅写"缺失或 0 字节"的文件，**不触碰用户已有内容**。
- **HEARTBEAT 空文件跳过判定**（`core_files.py:661`）：清空 `HEARTBEAT.md` 即关闭周期任务。
- **`MEMORY.md` 与长期记忆层分工**：`core_files.MEMORY.md` 是**用户/agent 手写的持久小抄**（工具设置、经验教训，agent 主动写），而 `memory.py` 的 `memory_vault` 是**引擎自动抽取的事实保险库**（对话驱动）。两者是不同层（见 §6 表），文档需区分"MEMORY.md 文件"与"memory_vault 索引"。

### 9.3 图提供者 `graph_provider.py`
构建并缓存编译好的 LangGraph 图（单例 + 请求级图），把 `ContextManager`、`MemoryManager`、`CoreFilesManager`、模式系统提示、工具集与共享 checkpointer 注入图，实现按用户/按模式隔离。

- **多用户隔离的 ContextManager**（`graph_provider.py:1001`）：图按 `(model, mode)` 编译为单例，不能把某用户 cm 编进图，故 `get_context_manager(user_id)` 按用户取/建 cm 并缓存，构造时注入 `tool_result_store` 与语义召回用的 `embedding_client`（复用 `get_memory_manager(uid).vault.embedding`，`graph_provider.py:1014-1018`）。
- **chat = 只读由 allowlist 保证**（`graph_provider.py:513-536`）：chat 模式只放显式 allowlist 工具（`calculator/read_file/list_dir/view_image/view_video/get_current_time/web_search/recall/memory_search`），任何未列出的（含未来新增带副作用工具）一律不进 chat；其余模式全开。比 denylist 更稳。
- **演示模型兜底**（`graph_provider.py:409`）：无 `llm.api_key` 时用 `DemoAgentModel`（确定性、走通工具+流式+人工裁决），保证无 key 也能演示完整产品形态。
- **配置热更新**：`get_graph` 用 `cfg.version` 判断重建（`graph_provider.py:1137`）；`get_request_graph`（`graph_provider.py:1160`）改用影响编译字段的签名做失效，支持前端每次请求携带 model/reasoning/api_key/mode 即时切换。
- **recall 工具不绑具体 cm**（`graph_provider.py:964-966`）：`make_recall_tool()` 无参，运行时由 `get_context_manager()` 按当前用户解析——解决"图单例 vs 多用户"冲突的关键。
- **checkpointer 必须异步初始化**（`graph_provider.py:1070`）：`AsyncSqliteSaver` 绑定事件循环，必须在 lifespan 调 `init_shared_checkpointer()`；未初始化时显式抛 `RuntimeError`，避免静默回退内存导致"以为持久化其实没持久化"。WAL + `busy_timeout=30s`（`graph_provider.py:1083-1087`）防止多实例锁死。

---

## 10. 服务端 / HTTP（app.py / config.py / scheduler.py / plugins.py 等）

### 10.1 应用生命周期 `app.py`
`lifespan`（`app.py:92-110`）启动顺序不可调换：
1. `migrate_from_workbuddy()`：把旧目录数据挪到独立 `DATA_HOME`，**必须在 `init_shared_checkpointer()` 之前**（否则迁移一个已被打开的 sqlite）。
2. `start_scheduler()` + `_register_scheduler_agent_runner()`。
3. `get_hub().start_poller()`：审批中枢后台轮询。
4. `await init_shared_checkpointer()`：异步初始化全局 checkpointer。
5. `apply_envs_on_startup()`：把持久化环境变量注入进程。

### 10.2 SSE 自研信封（token 增量字段为 `delta`）
核心在 `_run_envelope_impl`（`app.py:732-1011`）。事件契约（`app.py:735-744`）：

| 事件 | payload 关键字段 |
|------|------------------|
| `meta` | `{thread_id, model, reasoning, mode}` |
| `reasoning` | `{delta, phase}` |
| `token` | `{delta}` ← **回答增量** |
| `tool` | `{id, name, args, status:'start'|'result', result?}` |
| `approval` | `{id, question}` |
| `metrics` | 首字节/首思考/首答案/生成耗时 |
| `error` / `done` | — |

增量计算 `_delta`（`app.py:717-723`）兼容上游"增量"与"累积"两种载荷，避免前端重复拼接。非显而易见细节：
- **双层 chunk 去重**（`app.py:670-671`、`882-884`）：LangGraph 同时吐出内层模型与 AgentNode 两层 chunk（内容相同），按 `ls_provider` 过滤，否则 token/reasoning 重复。
- **system 消息拦截**（`app.py:891-892`）：图内部注入的 system 消息绝不泄漏为回答 token。
- **工具调用分片累积**（`app.py:925-957`）：`tool_call_chunks` 分片到达按 `id/index` 累积，避免畸形拼接。
- **`KEEPALIVE_INTERVAL=8s`**（`app.py:74`）：图在该秒数内无事件流出时主动发 `status` 事件，避免 pre-LLM 准备/上下文加载看起来像冻结。

### 10.3 Checkpoint 时间旅行原语（同一实现覆盖所有 mode）
- **`GET /threads/{id}/history`（`app.py:431-488`）**：返回倒序 checkpoint 步骤。**去重**：同一用户轮次会产生 N 个 `n_messages` 相同的快照，保留连续段里"最新"的一条（`app.py:451-456`）。时间戳统一转 epoch 秒（前端 `new Date(x*1000)`，`app.py:467-477`）。
- **`POST /rollback`（`app.py:491-514`）**：软回滚，`aupdate_state(target_cfg, {})` 等价 `git reset --soft`，历史不丢只移指针；并清理旧 pending 审批避免复活失效审批。
- **`POST /fork`（`app.py:517-539`）**：从 checkpoint 复制值到新 thread（读值 + 写新 thread，不跨 thread 引用 checkpoint_id）。
- **`POST /replay`（`app.py:542-563`）**：从指定 checkpoint 重放，复用 `/api/chat` 同一信封，body 带 `checkpoint_id` + `input`，并可即时切换模型。

### 10.4 配置 `config.py`
- **Fernet 加密 at rest**（`config.py:394-402`）：`runtime.json` 加密落盘，密钥来源 `CONFIG_SECRET` 环境变量 → `.config_secret` 文件（首次自动生成并 `chmod 600`）。无密钥退化为明文并打 warning。落盘文件 `chmod 600`（`config.py:455-458`）。
- **运行时热加载**（`save_config`，`config.py:435-460`）：**必须在持锁前先 `get_config()`**——否则非重入锁自死锁（`config.py:438-439`）。`masked()`（`config.py:318`）递归脱敏 `api_key` 与 `_token` 结尾字段。
- **数据目录独立**（`config.py:35-45`）：全部运行时数据落到 `~/.agent-harness`，不再写入 IDE 数据目录；`migrate_from_workbuddy()`（`config.py:60-97`）幂等迁移白名单文件（含 `checkpoints.sqlite`、`approvals.sqlite`、`cron.json`）。

### 10.5 定时任务 `scheduler.py`
- **后台线程，不阻塞 LLM 主循环**（`scheduler.py:51`）：`BackgroundScheduler` 在独立后台线程跑，APScheduler 回调也在该线程执行。
- **后台线程 → 主事件循环的桥梁**（`app.py:120-158`）：`_run_scheduled_agent` 用 `asyncio.run_coroutine_threadsafe` 把 `graph.ainvoke` 调度回主事件循环。
- **触发器**：`_parse_cron_trigger`（`scheduler.py:225`，5/6 位 cron + 时区）；`_parse_run_at`（`scheduler.py:242`，ISO8601 → `DateTrigger`，优先标准库 `fromisoformat` 避免 dateutil 缺失）。`misfire_grace_time=3600`（`scheduler.py:366`）补跑宽限。`_record_run`（`scheduler.py:68`）仅保留最近 200 条历史；`_push_cron_inbox`（`scheduler.py:90`）投递收件箱。

### 10.6 其他服务端模块
- **`plugins.py`**：聚合所有配件管理端点的 FastAPI Router（并非动态插件加载器），覆盖 Files/Tools/Skills/MCP/Agents/Cron/Heartbeat/Sessions/Memory/Context。会话索引 `sessions.json`（`plugins.py:1023`）结构 `{threads:{tid:{updated_at,title,user_id}}}`；多用户隔离列表只返回本 user 会话；`DELETE /sessions/{tid}` 用复合键 `user:tid` 清理 checkpoint。目录沙箱 `_safe_path`（`plugins.py:69`）防越界。
- **`inbox_store.py`**：收件箱事件，`DATA_HOME/inbox_events.json`，同步实现 + 线程锁 + 原子写（tmp+replace），上限 5000 条（`inbox_store.py:21-79`）。
- **`token_usage.py`**：完全非阻塞写盘（daemon 线程，`token_usage.py:36-75`），只在 `on_llm_end` 抓最终用量，对逐 token 流式零开销（`token_usage.py:124-170`）。
- **`model_discovery.py`**：厂商目录 `PROVIDERS`（`model_discovery.py:30`）单一事实源；探测 `_discover_sync`（`model_discovery.py:155`）优先 `GET {base}/models` 校验 key，失败回退已知模型 + 最小 chat 校验。隔离线程池 `_discover_executor`（`model_discovery.py:24`，同步 urllib 最长 ~24s，必须隔离避免抢占聊天路径）。
- **`ratelimit.py`**：按 IP 固定窗口限流（`rate_limit`/分钟，0=关）。`/config` 刻意不限流（`app.py:1166-1169`）否则自锁。多实例应换 Redis。
- **`settings_api.py`**：`/settings/*` 7 个模块（envs/security/voice/token-usage/backups/debug），纯 REST 读写 JSON，在 LLM 主循环之外。**安全开关真实落盘处**是 `/settings/security`（`settings_api.py:119-161`），把 `enable_auth`/`approval_level` 同步进 `runtime.json`，关闭鉴权时自动清空 `service.api_key` 避免死锁。

### 10.7 跨模块耦合总览
| 模块 | 耦合点 |
|---|---|
| `app.py` | `graph_provider.get_graph/get_request_graph/get_shared_checkpointer`；`scheduler.set_agent_runner`；`approval_hub.start_poller`；`config.migrate_from_workbuddy`（须先于 checkpointer） |
| `plugins.py` | `scheduler.*`（cron）；`memory.get_memory_manager`（memory 路由）；`graph_provider` context 端点；`inbox_store.*` |
| `scheduler.py` | `inbox_store.append_event`（结果投递）；`memory.get_memory_manager().dream()`（dream cron） |
| `approval_hub` | `config.running.approval_timeout_seconds`（超时来源） |
| `settings_api` | `config.save_config`（enable_auth/approval_level）；`token_usage.read_*` |
| `graph_provider` | `AsyncSqliteSaver` + `checkpoints.sqlite`；`DATA_HOME` |

### 10.8 非显而易见实现细节
1. **checkpoints.sqlite 复合键契约**：所有 checkpointer 写入的 `thread_id` 实为 `f"{user_id}:{thread_id}"`（`app.py:283-285`），天然按用户隔离；任何清理/回滚都必须用同一复合键。
2. **`AsyncSqliteSaver` + WAL + busy_timeout=30000**（`graph_provider.py:1082-1085`）：规避跨进程/重启瞬间 SQLite 锁竞争导致的冻结；配合 `start.sh` 启动前杀旧进程杜绝多实例共享 sqlite。
3. **history 去重**：按连续 `n_messages` 相同去重（每个 super-step 都写 checkpoint）。
4. **SSE delta 解析**：`_delta` 兼容增量/累积；system 消息与 `agentnode` 双层 chunk 必须过滤。
5. **审批两层超时**：`approval_hub` 进程内 future 超时 + `approval_watchdog` 基于 interrupt 登记的 reaper，共用 `approval_timeout_seconds`。
6. **配置锁顺序**：`save_config` 持锁前先 `get_config()`。
7. **限流自锁防护**：`/config` 不受 `rate_limit` 约束。
8. **图构建/模型探测专属线程池**：`_graph_build_executor`、`_discover_executor` 均隔离默认线程池，根因是同池抢占导致聊天请求偶发数十秒卡顿（`app.py:63-66`、`model_discovery.py:21-23`）。
9. **inbox 原子写**：tmp + replace，避免并发/损坏。
10. **token 统计零开销**：仅 `on_llm_end` 抓用量，daemon 线程落盘。

---

## 11. 前端与服务（React + 官方 useStream）

把 harness 图以 **LangGraph Platform 兼容的 HTTP + SSE 接口** 对外暴露，前端用官方 `@langchain/langgraph-sdk/react` 的 `useStream` hook（即 `BaseChat` 内部的数据层）直接接。无需 Docker / LangGraph Platform 服务。

> 本地 `@langchain/langgraph-ui@1.4.2` 只导出 `{build, watch}`（UI 代码生成 CLI），不含 React 组件，所以前端用官方 `useStream` + 自建干净组件（对落地产品更可控）。

### 目录
```
src/server/        # 见 §10（app.py / graph_provider.py / config.py / auth.py / ratelimit.py ...）
frontend/
  index.html / vite.config.ts / tsconfig.json / package.json / .env
  src/main.tsx / src/App.tsx / src/index.css / src/panels/*
scripts/
  gen_self_signed.sh # 生成本地自签 TLS 证书（deploy/tls/）
deploy/
  Caddyfile         # 生产 TLS 终止 + 反向代理
```

### 接口契约（已对照本地 @langchain/langgraph-sdk 源码核对 + 用官方 SDK 无头验证）
- `GET  /ok`
- `GET  /assistants` · `POST /assistants/search` · `GET /assistants/{id}`
- `POST /threads` · `GET /threads/{id}` · `POST /threads/{id}/state` · `GET /threads/{id}/state`
- `POST /threads/{id}/runs/stream`（SSE：`metadata` / `messages` / `values` / `updates` / `error` / `end`）
  - 新运行：`{"input":{"messages":[{"role":"user","content":"..."}]}, "stream_mode":["messages","values"], "assistant_id":"agent-harness"}`
  - 人工裁决：`{"command":{"resume":"approved"}, "stream_mode":["messages","values"], "assistant_id":"agent-harness"}`
- `POST /threads/{id}/runs`（非流式兜底）
- `GET  /config`（读取当前配置，脱敏返回；鉴权开启时需带令牌）
- `POST /config`（合并并持久化配置：`llm.api_key`/`llm.base_url`/`llm.model`/`service.api_key`/`security.enable_auth`/`rate_limit`；保存即热生效，无需重启）
- `GET  /metrics`（可观测快照）
- `GET  /providers`（内置模型厂商目录：id/label/base_url/已知模型/reasoning 支持；无需鉴权）
- `POST /models`（发现并校验模型：body=`{base_url, api_key}`，返回 `key_valid`/`discovered`/`models`/`reasoning_models`/`error`；走 OpenAI 兼容 `/models` 探测，失败时回退已知模型并做最小 chat 完成验证 key；施加限流）
- Checkpoint 时间旅行：`GET /threads/{id}/history`、`POST /rollback`、`POST /fork`、`POST /replay`（见 §10.3）

无头验证：`cd frontend && node verify_sdk.mjs`（创建会话→流式→触发 interrupt→resume 执行工具，全绿）。

---

# 快速开始

```bash
pip install -r requirements.txt

# 1) Loop Gates 纯逻辑单测（零依赖，无需任何 key）
python -c "import tests.test_gates as t; [getattr(t,n)() for n in dir(t) if n.startswith('test_')]; print('gates OK')"

# 2) 上下文 + 安全单测
python -m pytest tests/test_context.py tests/test_security.py -q

# 3) 稳定版全链路确定性验证（无需 key，覆盖全部稳定性能力）
python examples/run_stable.py

# 4) 真实 DeepSeek 运行
export DEEPSEEK_API_KEY=sk-xxxx
export DEEPSEEK_BASE_URL=https://api.deepseek.com/v1   # 可选
python examples/run_deepseek.py
```

### 跑起来（前后端）

```bash
# 一键启动（后端 8123 + 前端 5173）。脚本会自动选用隔离 venv 的 python 与 managed node。
bash start.sh

# 或手动：
# 后端（FastAPI，端口 8123）
pip install -r requirements.txt
python -m uvicorn src.server.app:app --host 127.0.0.1 --port 8123 --log-level warning

# 前端（Vite，端口 5173）
cd frontend && npm install && npm run dev
# 浏览器打开 http://localhost:5173
```

> 想接真实模型：**无需改代码、无需重启**。打开前端左上「⚙️ 设置 / 配置 API」，填入 LLM API Key / Base URL / Model，点「保存」即生效（后端热加载，图工厂按版本号重建）。未设置 key 时后端用确定性 `DemoAgentModel`，前端仍可完整演示 流式 / 工具 / 人工裁决。

---

# 安全与配置（传输层 + 运行时热配置）

## 运行时热配置（前端「设置」面板）
前端左上角「⚙️ 设置 / 配置 API」可填：
- **LLM API Key / Base URL / Model**：保存后后端图工厂按版本号自动重建，下一轮对话即用真实模型（无需重启）。
- **模型厂商下拉 + 「发现模型」**：选厂商自动带出默认 Base URL；点发现拉取可用模型并校验密钥；支持 DeepSeek / OpenAI / 阿里云百炼(Qwen) / 智谱 / Kimi / 硅基流动 / Ollama / 自定义。
- **思考模式开关**：开启后按厂商差异化传参；前端会根据 `/models` 返回的 `reasoning_models` 自动提示/切换支持思考的模型。
- **启用访问鉴权 + 服务令牌**：开启后，所有 `/threads/.../runs`、`/config` 请求须带 `Authorization: Bearer <token>` 或 `x-api-key: <token>`（后者与官方 SDK `Client({apiKey})` 默认行为一致）。
- 配置落盘到 `config/runtime.json`，**Fernet 加密**（密钥在 `config/.config_secret`，chmod 600）；未设 `CONFIG_SECRET` 时退化为明文并打警告。
- `GET /config` 始终脱敏（`api_key` 显示为 `****`），不泄露密钥明文。

## 传输层 / 部署级安全
| 能力 | 实现 | 状态 |
|---|---|---|
| 服务级鉴权 | `auth.py`：Bearer / `x-api-key`，按 `security.enable_auth` 开关 | ✅ |
| 限流 | `ratelimit.py`：按 IP 固定窗口（`rate_limit`/分钟，0=关） | ✅ |
| 安全响应头 | `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` / `CSP` / `Permissions-Policy` | ✅ |
| 配置加密 at rest | `config.py`：Fernet 加密 `runtime.json` | ✅ |
| 传输加密 (TLS) | `scripts/gen_self_signed.sh` + uvicorn SSL，或 `deploy/Caddyfile` 反向代理 | ✅ 见下 |
| 线程状态加密 at rest | 当前默认 `MemorySaver`（内存，不落盘）；接入 Postgres/Redis checkpointer 时用其原生加密 / KMS | ⏳ 按需 |

> 默认 `security.enable_auth=false`、`rate_limit=60`，开箱即用于本地开发；暴露到网络前请开启鉴权、配置限流，并前置 TLS（见下）。

### TLS（传输加密）
**本地自签（演示）：**
```bash
bash scripts/gen_self_signed.sh          # 生成 deploy/tls/{cert.pem,key.pem}
# 启动 HTTPS 后端（前端 VITE_API_URL 改 https://127.0.0.1:8443）
PYTHONPATH=. python -m uvicorn src.server.app:app \
  --host 0.0.0.0 --port 8443 \
  --ssl-keyfile deploy/tls/key.pem --ssl-certfile deploy/tls/cert.pem
```
**生产（Caddy 自动签发受信任证书）：**
```bash
brew install caddy
caddy run --config deploy/Caddyfile      # :8443 -> localhost:8123，自动 HTTPS
```
> 未启用 TLS 时通过 HTTP 发送 API Key 有被窃听风险，请仅本地或经反向代理使用。

---

# 设计原则（稳定版）

1. **Gate 零依赖**：核心大脑（gates）不 import langgraph/langchain，可独立单测、可复用。
2. **零丢失上下文**：原始消息全文写穿 SQLite；折叠只是占位桩，召回即还原（§5）。
3. **防御纵深**：工具前 ApprovalGate（人工裁决）+ 工具内 PolicyGuardedTool（守卫拒绝执行）。
4. **fail-closed**：guardian 抛异常 → 拒绝；模型彻底失败 → 转成带错误内容的 AIMessage 不卡死循环。
5. **可观测**：每轮/每次门触发/每次折叠/每次召回/每次工具调用都有结构化日志 + 计数器（§10.6 `token_usage` / `observability`）。
6. **会话隔离 + 崩溃恢复**：checkpointer 的 `thread_id`（复合键 `user:tid`）持久化全部状态（§10.8）。

---

# 已落地能力

- [x] **Phase 0**：LangGraph 自建图 + Governor 节点骨架
- [x] **Phase 1**：IterationGate + DoomLoopGate + 优先级调度 + thread_id 会话隔离
- [x] **Phase 2**：治理/安全（PolicyGuardedTool + ApprovalGate → interrupt 真人工裁决）
- [x] **Phase 3（零件接口）**：context/security/stability 模块已就绪，DeepAgents middleware 按需 import
- [x] **Phase 4**：MissionGate 多 Mode 任务级门控（读 prd.json）
- [x] **Phase 5**：重试/熔断/可观测 + SSE 心跳保活 + 取消时由 checkpointer 保活 + HTTP 服务 + React 前端
- [x] **Phase 6 · 传输层/部署级安全**：运行时热配置 + 服务级鉴权（Bearer/x-api-key）+ 按 IP 限流 + 安全响应头 + 配置 Fernet 加密 at rest + TLS
- [x] **运行时配置面板**：前端「⚙️ 设置 / 配置 API」填 LLM Key/URL/Model 与鉴权令牌，保存即热生效
- [x] **模型厂商目录 + 发现/校验**：选厂商 → 填 Key → 发现模型并校验密钥（OpenAI 兼容 `/models` 探测 + 已知模型回退）；内置 DeepSeek/OpenAI/Qwen/智谱/Moonshot/硅基流动/Ollama 等
- [x] **思考模式开关**：配置可开启 `reasoning`（DeepSeek-R1/Qwen 思考/o1 等），按厂商差异化传参（DeepSeek 推理模型名自带能力不再误传 `thinking`；OpenAI o 系列传 `reasoning_effort=high`；Qwen3/QwQ 传 `thinking=true`；其余保守不传）；配置/参数类错误快速失败不重试
- [x] **Checkpoint 时间旅行**：历史时间轴 / 软回滚 / 分叉 / 重放（§10.3）
- [x] **上下文折叠 + 召回**（§5）、**长期语义记忆 + 混合检索**（§6）
