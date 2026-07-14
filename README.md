# agent-harness（你的新产品骨架 · 路线 A · 稳定版）

基于 **LangGraph 自建 StateGraph** 的 Agent harness，把 QwenPaw 的
**harness engineering**（Loop Gates / StopHandler / 上下文管理 / 治理）
用图节点重新实现成可落地、可观测、可崩溃恢复的稳定版本。

Governor 是真·图节点，跑在每轮"模型调用 + 工具执行"的干净边界上；
**上下文管理**用 fold-not-summarize（零丢失）独立成模块；**治理**用
PolicyGuardedTool + 工具前 ApprovalGate + `interrupt()` 真人工裁决。
DeepAgents 的 middleware（Filesystem/Memory/SubAgent）在 Phase 3+ 当零件接入。

> 为什么不直接用 `create_deep_agent`？对照本地源码发现：它的 `create_deep_agent`
> 不构建 StateGraph，返回已编译图、builder 不可见，**无法插入 Governor 节点**。
> 所以借它的"零件"，握自己的"图"。

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

- **context**：ContextManager 在每轮模型调用前折叠/召回，产出 `prompt_messages` 窗口
  （原始 `messages` 永不折叠，仅作事件日志）。
- **approval**：工具执行前拦截敏感工具，触发 ASK → `interrupt()`。
- **governor**：运行全部 Loop Gates，STOP / CONTINUE(续跑桩) / ASK。

## 目录结构

```
src/harness/
  state.py            # AgentState（messages / prompt_messages / turn / gate_state / context_state / decision / pending_after_hitl）
  gates/              # Loop Gates（零依赖，可单独单测）
    base.py           # StopAction / StopHandlerResult / StopGate / run_stop_gates
    iteration.py      # IterationGate（硬迭代上限，priority=10）
    doom_loop.py      # DoomLoopGate（滑窗相似度防鬼打墙，priority=70）
  governor.py         # Governor 节点 + route_after_governor
  agent.py            # agent 节点（模型调用 + 重试/熔断 + 用 prompt_messages）
  tools_node.py       # tools 节点（等价 ToolNode；错误回传模型；记录指标）
  context/            # 上下文管理（fold-not-summarize，零丢失）
    store.py          # TurnStore：SQLite 写穿式，原始消息全文落库
    manager.py        # ContextManager：预算触发折叠 / 召回 / #5746 防御
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
  graph.py            # build_graph()：完整拓扑装配
tests/
  test_gates.py       # Loop Gates 纯逻辑单测（零依赖）
  test_context.py     # 上下文折叠 / 召回 / 幂等 单测
  test_security.py    # 守卫引擎 / PolicyGuardedTool 单测
examples/
  run_minimal.py      # Phase0+1 演示（fake 模型，无需 key）
  run_stable.py       # 稳定版全链路确定性验证（A 失控 / B 上下文 / C 审批 / D 任务）
  run_deepseek.py     # 真实 deepseek-v4-pro 运行（需 DEEPSEEK_API_KEY）
```

## 跑起来

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

## 与 QwenPaw 的映射

| QwenPaw | 这里 |
|---|---|
| `loop/gates/base.py` StopGate | `gates/base.py` StopGate |
| `run_stop_handlers` priority 升序短路 | `run_stop_gates` |
| `_reasoning()` 每轮末尾插门 + `_gate_pending_stop` 延迟落槌 | Governor 独立节点（边界天然干净，hack 免了） |
| `_sessions` dict 会话隔离 | `gate_state` 放进 graph state，随 `thread_id` 持久化 |
| CONTINUE 注入续跑桩 | Governor 返回 `{"messages":[SystemMessage(...)]}` |
| Scroll Context（fold-not-summarize，零丢失） | `context/`：TurnStore 写穿 + ContextManager 折叠/召回 |
| ToolGuardEngine（FilePath/RuleBased/ShellEvasion） | `security/guard.py`（同款三守卫，fail-closed） |
| PolicyGuardedTool（绕过自带权限） | `security/guarded_tool.py` |
| GovernancePolicy.ASK | `security/approval.py` ApprovalGate + `hitl_node` + `interrupt()` |
| MissionGate / session 级完成判定 | `stability/modes.py` MissionGate（读 prd.json） |
| heartbeat / 取消保存 / 可观测 | `stability/`：retry/熔断 + Metrics（Phase 5 SSE 待接） |

## 设计原则（稳定版）

1. **Gate 零依赖**：核心大脑（gates）不 import langgraph/langchain，可独立单测、可复用。
2. **零丢失上下文**：原始消息全文写穿 SQLite；折叠只是占位桩，召回即还原。
3. **防御纵深**：工具前 ApprovalGate（人工裁决）+ 工具内 PolicyGuardedTool（守卫拒绝执行）。
4. **fail-closed**：guardian 抛异常 → 拒绝；模型彻底失败 → 转成带错误内容的 AIMessage 不卡死循环。
5. **可观测**：每轮/每次门触发/每次折叠/每次召回/每次工具调用都有结构化日志 + 计数器。
6. **会话隔离 + 崩溃恢复**：checkpointer 的 `thread_id` 持久化全部状态。

## 已落地 / 待办

- [x] **Phase 0**：LangGraph 自建图 + Governor 节点骨架
- [x] **Phase 1**：IterationGate + DoomLoopGate + 优先级调度 + thread_id 会话隔离
- [x] **Phase 2**：治理/安全（PolicyGuardedTool + ApprovalGate → interrupt 真人工裁决）
- [x] **Phase 3（零件接口）**：context/security/stability 模块已就绪，DeepAgents middleware 按需 import
- [x] **Phase 4**：MissionGate 多 Mode 任务级门控（读 prd.json）
- [x] **Phase 5**：重试/熔断/可观测 + SSE 心跳保活 + 取消时由 checkpointer 保活 + HTTP 服务 + React 前端（LangGraph 官方 SDK）
- [x] **前端**：React + 官方 `@langchain/langgraph-sdk/react` 的 `useStream`（BaseChat 同一套数据层），
      自带会话/流式上屏/人工裁决表单/可观测面板。后端用 LangGraph Platform 兼容接口对外暴露。
- [x] **Phase 6 · 传输层/部署级安全**：运行时热配置（`/config` + 前端设置面板）+ 服务级鉴权（Bearer/x-api-key）
      + 按 IP 限流 + 安全响应头 + 配置 Fernet 加密 at rest + TLS（自签脚本 / Caddy 反向代理）。
- [x] **运行时配置面板**：前端「⚙️ 设置 / 配置 API」填 LLM Key/URL/Model 与鉴权令牌，保存即热生效。
- [x] **模型厂商目录 + 发现/校验（Phase 7）**：前端「选厂商 → 填 Key → 发现模型」一键拉取可用模型并校验密钥是否有效（OpenAI 兼容 `/models` 探测 + 已知模型回退）；内置 DeepSeek/OpenAI/Qwen/智谱/Moonshot/硅基流动/Ollama 等厂商。
- [x] **思考模式开关（Phase 7）**：配置可开启 `reasoning`（DeepSeek-R1/Qwen 思考/o1 等）。
      **按厂商差异化传参**：DeepSeek 推理模型名自带能力（如 `deepseek-reasoner`），不再误传 `thinking`；
      OpenAI o 系列传 `reasoning_effort=high`；Qwen3/QwQ 传 `thinking=true`；其余保守不传，避免把请求打挂。
      配置/参数类错误会快速失败、不重试，避免用户空等。

## 前端与服务（HTTP + LangGraph 前端）

把 harness 图以 **LangGraph Platform 兼容的 HTTP + SSE 接口** 对外暴露，前端用官方
`@langchain/langgraph-sdk/react` 的 `useStream` hook（即 `BaseChat` 内部的数据层）直接接。
无需 Docker / LangGraph Platform 服务。

> 注：本地 `@langchain/langgraph-ui@1.4.2` 只导出 `{build, watch}`（UI 代码生成 CLI），
> 不含 React 组件，所以前端用官方 `useStream` + 自建干净组件（对落地产品更可控）。

### 目录

```
src/server/
  app.py            # FastAPI：Platform 兼容 REST + SSE + /config + 安全头 + 鉴权/限流依赖
  graph_provider.py # 编译图单例：模型(DeepSeek 或 确定性 DemoAgentModel) + 工具 + ContextManager（按运行时配置热重建）
  sse_utils.py      # SSE 序列化（消息/Interrupt/状态 -> JSON；心跳注释行）
  config.py         # RuntimeConfig 单例：热加载 + Fernet 加密落盘（config/runtime.json）
  auth.py           # 服务级鉴权依赖（Bearer / x-api-key，按 config.security.enable_auth 开关）
  ratelimit.py      # 固定窗口限流依赖（按 IP，按 config.rate_limit）
frontend/
  index.html / vite.config.ts / tsconfig.json / package.json / .env
  src/main.tsx / src/App.tsx / src/index.css
  verify_sdk.mjs    # 用官方 JS SDK 无头验证接口契约（与前端同一套客户端）
scripts/
  gen_self_signed.sh # 生成本地自签 TLS 证书（deploy/tls/）
deploy/
  Caddyfile         # 生产 TLS 终止 + 反向代理（自动签发受信任证书）
examples/run_deepseek.py / run_stable.py / run_minimal.py   # 已改为 async API
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

> 想接真实 DeepSeek：**无需改代码、无需重启**。打开前端左上「⚙️ 设置 / 配置 API」，
> 填入 LLM API Key / Base URL / Model，点「保存」即生效（后端热加载，图工厂按版本号重建）。
> 也可在后端进程 `export DEEPSEEK_API_KEY=...`（可选 `DEEPSEEK_BASE_URL`）。
> 未设置 key 时后端用确定性 `DemoAgentModel`，前端仍可完整演示 流式 / 工具 / 人工裁决。

### 接口契约（已对照本地 @langchain/langgraph-sdk 源码核对 + 用官方 SDK 无头验证）

- `GET  /ok`
- `GET  /assistants` · `POST /assistants/search` · `GET /assistants/{id}`
- `POST /threads` · `GET /threads/{id}` · `POST /threads/{id}/state` · `GET /threads/{id}/state`
- `POST /threads/{id}/runs/stream`（SSE：`metadata` / `messages` / `values` / `updates` / `error` / `end`）
  - 新运行：`{"input":{"messages":[{"role":"user","content":"..."}]}, "stream_mode":["messages","values"], "assistant_id":"agent-harness"}`
  - 人工裁决：`{"command":{"resume":"approved"}, "stream_mode":["messages","values"], "assistant_id":"agent-harness"}`
- `POST /threads/{id}/runs`（非流式兜底）
- `GET  /config`（读取当前配置，脱敏返回；鉴权开启时需带令牌）
- `POST /config`（合并并持久化配置：`llm.api_key`/`llm.base_url`/`llm.model`/`service.api_key`/
  `security.enable_auth`/`rate_limit`；保存即热生效，无需重启）
- `GET  /metrics`（可观测快照）
- `GET  /providers`（内置模型厂商目录：id/label/base_url/已知模型/reasoning 支持；无需鉴权）
- `POST /models`（发现并校验模型：body=`{base_url, api_key}`，返回 `key_valid`/`discovered`/`models`/`reasoning_models`/`error`；
  走 OpenAI 兼容 `/models` 探测，失败时回退已知模型并做最小 chat 完成验证 key；施加限流）

无头验证：`cd frontend && node verify_sdk.mjs`（创建会话→流式→触发 interrupt→resume 执行工具，全绿）。

## 安全与配置（传输层 + 运行时热配置）

### 运行时热配置（前端「设置」面板）
前端左上角「⚙️ 设置 / 配置 API」可填：
- **LLM API Key / Base URL / Model**：保存后后端图工厂按版本号自动重建，下一轮对话即用真实模型（无需重启）。
- **模型厂商下拉 + 「发现模型」**：选厂商自动带出默认 Base URL；点发现拉取可用模型并校验密钥；支持 DeepSeek / OpenAI / 阿里云百炼(Qwen) / 智谱 / Kimi / 硅基流动 / Ollama / 自定义。
- **思考模式开关**：开启后按厂商差异化传参；前端会根据 `/models` 返回的 `reasoning_models` 自动提示/切换支持思考的模型。
- **启用访问鉴权 + 服务令牌**：开启后，所有 `/threads/.../runs`、`/config` 请求须带 `Authorization: Bearer <token>` 或 `x-api-key: <token>`（后者与官方 SDK `Client({apiKey})` 默认行为一致）。
- 配置落盘到 `config/runtime.json`，**Fernet 加密**（密钥在 `config/.config_secret`，chmod 600）；未设 `CONFIG_SECRET` 时退化为明文并打警告。
- `GET /config` 始终脱敏（`api_key` 显示为 `****`），不泄露密钥明文。

### 传输层 / 部署级安全（Phase 6）
| 能力 | 实现 | 状态 |
|---|---|---|
| 服务级鉴权 | `auth.py`：Bearer / `x-api-key`，按 `security.enable_auth` 开关 | ✅ |
| 限流 | `ratelimit.py`：按 IP 固定窗口（`rate_limit`/分钟，0=关） | ✅ |
| 安全响应头 | `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` / `CSP` / `Permissions-Policy` | ✅ |
| 配置加密 at rest | `config.py`：Fernet 加密 `runtime.json` | ✅ |
| 传输加密 (TLS) | `scripts/gen_self_signed.sh` + uvicorn SSL，或 `deploy/Caddyfile` 反向代理 | ✅ 见下 |
| 线程状态加密 at rest | 当前默认 `MemorySaver`（内存，不落盘）；接入 Postgres/Redis checkpointer 时用其原生加密 / KMS | ⏳ 按需 |

> 默认 `security.enable_auth=false`、`rate_limit=60`，开箱即用于本地开发；
> 暴露到网络前请开启鉴权、配置限流，并前置 TLS（见下）。

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
