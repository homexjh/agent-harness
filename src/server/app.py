"""FastAPI 服务：把 harness 图以 LangGraph Platform 兼容的 HTTP + SSE 接口对外暴露。

这样前端可以直接用官方 @langchain/langgraph-sdk 的 useStream（BaseChat 同一套数据层）接上来，
无需 Docker / LangGraph Platform 服务。

契约（已对照本地安装的 @langchain/langgraph-sdk 源码核对）：
- GET  /ok
- GET  /assistants | POST /assistants/search | GET /assistants/{id}
- POST /threads | GET /threads/{id} | POST /threads/search
- GET  /threads/{id}/state | POST /threads/{id}/state
- POST /threads/{id}/runs            (非流式 create)
- POST /threads/{id}/runs/stream      (SSE 流式：metadata/messages/values/updates/error)
      新运行：body={"input":{"messages":[...]}, "stream_mode":[...], "assistant_id":...}
      人工裁决：body={"command":{"resume":<value>}, "stream_mode":[...], "assistant_id":...}
- GET  /metrics                        (可观测快照)
- GET  /providers                       (内置模型厂商目录)
- POST /models                          (发现/校验模型：给定 base_url+api_key 返回可用模型与 key 有效性)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from langgraph.types import Command
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .graph_provider import (
    get_assistant_id,
    get_context_manager,
    get_graph,
    get_metrics,
    get_request_graph,
    get_shared_checkpointer,
    init_shared_checkpointer,
)
from .sse_utils import interrupt_to_dict, jsonable, jsonable_state, sse, sse_comment
from .config import get_config, save_config
from .auth import require_auth
from .ratelimit import rate_limit
from .model_discovery import discover_models, list_providers
from .plugins import router as plugins_router, _touch_session
from .scheduler import start_scheduler, stop_scheduler
from .approval_hub import get_hub, set_emitter, clear_emitter


# 流式抽取哨兵：图在后台任务跑，主循环从 out_q 抽事件（含内联审批事件）。
_SENTINEL_DONE = object()

# 图构建专属线程池：get_request_graph 是同步重活（缓存命中通常 <1s，但缓存失效时会
# 编译图）。隔离在默认线程池之外，避免与模型探测（discover_models）或其他
# run_in_executor 任务互相抢占，导致聊天请求偶发卡数十秒（"卡住很久"症状的一类根因）。
_graph_build_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="graph-build")

# 图构建超时：超过此值视为异常（正常应 <1s，缓存失效编译也应在数秒内），
# 避免任何同步阻塞把聊天请求无限挂起。超时后回退到错误事件而非静默卡死。
GRAPH_BUILD_TIMEOUT = 30.0

# 保活间隔：图在此秒内无任何 SSE 事件流出时，后端主动发 status 事件，
# 让前端显示「处理中」，避免 pre-LLM 准备 / 上下文加载等偶发阻塞看起来像卡死。
KEEPALIVE_INTERVAL = 8.0


async def _pump_graph(graph, input_val, config, out_q: "asyncio.Queue") -> None:
    """后台任务：把 graph.astream 的 chunk 推入 out_q；结束推哨兵；异常推错误元组。

    这样 hitl 节点在 await future 期间，图任务只是**挂起**在 out_q.put 之外，
    主循环仍能抽到其他事件（如内联的 approval 事件），实现「原地 await」式审批。
    """
    try:
        async for item in graph.astream(input_val, config, stream_mode="messages"):
            await out_q.put(item)
    except Exception as exc:  # noqa: BLE001
        await out_q.put(("__error__", exc))
    finally:
        await out_q.put(_SENTINEL_DONE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动后台定时任务调度器与审批中枢 poller，退出时关闭。"""
    start_scheduler()
    get_hub().start_poller()
    await init_shared_checkpointer()
    yield
    stop_scheduler()


app = FastAPI(title="Agent Harness API", version="0.1.0", lifespan=lifespan)
app.include_router(plugins_router)

logger = logging.getLogger("agent_harness")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.propagate = False

# CORS：前端 dev server (5173) 跨域访问 API (8123)。生产环境请收紧 origins。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """类 Helmet 的安全响应头（传输层纵深防御之一）。"""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'",
    )
    resp.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    return resp


def _thread_obj(thread_id: str) -> dict:
    now = int(time.time())
    return {
        "thread_id": thread_id,
        "created_at": now,
        "updated_at": now,
        "metadata": {},
        "status": "idle",
        "values": None,
    }


@app.get("/ok")
async def ok():
    return {"ok": True}


@app.get("/assistants")
@app.post("/assistants/search")
async def list_assistants():
    a = {
        "assistant_id": get_assistant_id(),
        "graph_id": "agent-harness",
        "name": "Agent Harness",
        "config": {},
        "metadata": {},
        "public": True,
        "version": 1,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    return {"assistants": [a], "total": 1, "limit": 10, "offset": 0}


@app.get("/assistants/{assistant_id}")
async def get_assistant(assistant_id: str):
    if assistant_id != get_assistant_id():
        return JSONResponse(status_code=404, content={"detail": "assistant not found"})
    return {
        "assistant_id": assistant_id,
        "graph_id": "agent-harness",
        "name": "Agent Harness",
        "config": {},
        "metadata": {},
        "public": True,
        "version": 1,
    }


@app.post("/threads")
async def create_thread(payload: dict = {}):
    tid = payload.get("thread_id") or uuid.uuid4().hex
    return _thread_obj(tid)


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str):
    return _thread_obj(thread_id)


@app.post("/threads/search")
async def search_threads(payload: dict = {}):
    return {"threads": [], "total": 0, "limit": payload.get("limit", 10), "offset": payload.get("offset", 0)}


@app.post("/threads/{thread_id}/reset")
async def reset_thread(thread_id: str):
    """重置一个卡死的会话线程（对齐 QwenPaw 的文件态可恢复）。

    彻底清除该 thread 的：
    - 未决审批登记（看门狗不再管它）；
    - checkpointer 中的 checkpoint（包括挂起的 HITL interrupt）；
    - 上下文 store 中的历史 turn。
    让卡住的 thread 能原地救活，无需开新对话。
    """
    # 1) 清未决审批登记（解除可能挂起的 await future，并清共享表）
    try:
        get_hub().clear_thread(thread_id)
    except Exception:  # noqa: BLE001
        logger.warning("reset_thread clear_approval failed thread=%s", thread_id)
    # 2) 删 checkpoint（含挂起 interrupt）—— MemorySaver.delete_thread 等价“丢弃该线程状态”
    cp = get_shared_checkpointer()
    try:
        await cp.adelete_thread(thread_id)
    except Exception:  # noqa: BLE001
        logger.warning("reset_thread delete_checkpoint failed thread=%s", thread_id)
    # 3) 清上下文 store 历史
    try:
        cm = get_context_manager()
        cm.clear(thread_id)
    except Exception:  # noqa: BLE001
        logger.warning("reset_thread clear_context failed thread=%s", thread_id)
    logger.info("thread reset thread=%s", thread_id)
    return {"ok": True, "thread_id": thread_id}


@app.get("/threads/{thread_id}/state")
@app.post("/threads/{thread_id}/state")
async def get_state(thread_id: str):
    graph = get_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    try:
        snap = await graph.aget_state(cfg)
    except Exception:
        snap = None
    if snap is None or snap.values is None:
        return {
            "values": {},
            "next": [],
            "tasks": [],
            "metadata": None,
            "created_at": None,
            "checkpoint": {"thread_id": thread_id, "checkpoint_id": None, "checkpoint_ns": "", "checkpoint_map": None},
            "parent_checkpoint": None,
        }
    # 重建 __interrupt__：LangGraph 把中断存在 tasks[].interrupts，
    # aget_state().values 不会回写，但前端(useStream)重载暂停线程时需要它。
    interrupts = []
    for t in (snap.tasks or []):
        for i in getattr(t, "interrupts", []) or []:
            interrupts.append(i)
    values = jsonable_state(snap.values)
    if interrupts:
        values["__interrupt__"] = [interrupt_to_dict(i) for i in interrupts]
    return {
        "values": values,
        "next": list(snap.next or []),
        "tasks": [
            {"id": t.id, "name": getattr(t, "name", None)} for t in (snap.tasks or [])
        ],
        "metadata": snap.metadata,
        "created_at": None,
        "checkpoint": snap.config or {"thread_id": thread_id},
        "parent_checkpoint": snap.parent_config,
    }


def _extract_stream_bits(msg_chunk: Any):
    """从流式消息 chunk 中提取答案文本和思考文本。"""
    content = getattr(msg_chunk, "content", None)
    additional = dict(getattr(msg_chunk, "additional_kwargs", {}) or {})
    reasoning_from_kwargs = additional.get("reasoning_content", "")

    if not isinstance(content, list):
        return {
            "answer": str(content) if content else "",
            "reasoning": reasoning_from_kwargs,
        }

    answer_parts, reasoning_parts = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            answer_parts.append(block.get("text", ""))
        elif btype == "reasoning":
            for s in block.get("summary", []) or []:
                if isinstance(s, dict) and s.get("type") == "summary_text":
                    reasoning_parts.append(s.get("text", ""))
    reasoning = "".join(reasoning_parts) or reasoning_from_kwargs
    return {"answer": "".join(answer_parts), "reasoning": reasoning}


async def _run_stream(thread_id: str, body: dict):
    """SSE 生成器：把图运行过程以 Platform 兼容事件流吐出。"""
    graph = get_graph()
    run_id = uuid.uuid4().hex
    # 注意：不再在请求开始时 reset() 指标。reset 会让「正在发送新消息」时
    # Agent Statistics 面板闪现全 0，看起来像统计失败。改为累积统计（轮次/工具调用/
    # 门触发等累计），时间类指标（首字节/生成耗时）每次请求会被覆盖为最新值。
    metrics = get_metrics()
    t0 = time.perf_counter()
    first_byte_recorded = False
    first_reasoning_recorded = False
    first_answer_recorded = False

    command = body.get("command")
    if isinstance(command, dict) and any(k in command for k in ("resume", "goto", "update")):
        input_val: Any = Command(
            resume=command.get("resume"),
            goto=command.get("goto"),
            update=command.get("update"),
        )
    else:
        input_val = body.get("input") or {}

    modes = body.get("stream_mode") or []
    if not modes:
        modes = ["messages", "values"]
    if "values" not in modes:
        modes = list(modes) + ["values"]

    config: dict = {"configurable": {"thread_id": thread_id}, "recursion_limit": 200}
    bcfg = body.get("config") or {}
    if isinstance(bcfg, dict):
        config["configurable"].update(bcfg.get("configurable", {}))
        if "recursion_limit" in bcfg:
            config["recursion_limit"] = bcfg["recursion_limit"]

    queue: asyncio.Queue = asyncio.Queue()

    async def producer():
        try:
            async for item in graph.astream(input_val, config, stream_mode=modes):
                # 多模式时 item 为 (mode, data)；单模式时为 data
                if isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], str):
                    mode, chunk = item
                else:
                    mode, chunk = "values", item
                await queue.put(("chunk", mode, chunk))
        except Exception as e:  # noqa: BLE001
            await queue.put(("error", str(e)))
        finally:
            await queue.put(("done", None))

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(15)
                await queue.put(("ping", None))
        except asyncio.CancelledError:
            return

    prod = asyncio.create_task(producer())
    hb = asyncio.create_task(heartbeat())
    try:
        yield sse("metadata", {"run_id": run_id, "thread_id": thread_id})
        while True:
            item = await queue.get()
            kind = item[0]
            if kind == "chunk":
                mode, chunk = item[1], item[2]
                if mode == "messages":
                    try:
                        msg_chunk, meta = chunk[0], chunk[1]
                    except Exception:
                        msg_chunk, meta = chunk, {}
                    # 过滤掉底层 LLM 原始事件（ls_provider=openai 等），
                    # 只保留 AgentNode 归一化后的事件，避免前端收到重复消息。
                    ls_provider = meta.get("ls_provider", "")
                    if ls_provider and ls_provider not in ("agentnode", "demo-agent-model"):
                        continue
                    # 记录流式时间指标（LangGraph 的 messages 模式会直接捕获底层模型事件）
                    now_ms = round((time.perf_counter() - t0) * 1000, 2)
                    if not first_byte_recorded:
                        first_byte_recorded = True
                        metrics.last_first_byte_ms = now_ms
                    extracted = _extract_stream_bits(msg_chunk)
                    if extracted["reasoning"] and not first_reasoning_recorded:
                        first_reasoning_recorded = True
                        metrics.last_first_reasoning_ms = now_ms
                    if extracted["answer"] and not first_answer_recorded:
                        first_answer_recorded = True
                        metrics.last_first_answer_ms = now_ms
                        metrics.last_ttft_ms = now_ms
                    yield sse("messages", [jsonable(msg_chunk), jsonable(meta)])
                elif mode == "values":
                    yield sse("values", jsonable_state(chunk))
                elif mode == "updates":
                    yield sse("updates", jsonable_state(chunk))
                else:
                    yield sse(mode, jsonable_state(chunk))
            elif kind == "ping":
                yield sse_comment()
            elif kind == "error":
                metrics.error("stream", "exception")
                yield sse("error", {"message": item[1]})
            elif kind == "done":
                metrics.turn()
                metrics.last_generation_ms = round((time.perf_counter() - t0) * 1000, 2)
                if metrics.last_ttft_ms == 0.0 and first_byte_recorded:
                    metrics.last_ttft_ms = metrics.last_first_byte_ms
                yield sse("end", {"run_id": run_id})
                break
    finally:
        hb.cancel()
        prod.cancel()
        # LangGraph checkpointer 已在每个 superstep 落盘线程状态，
        # 客户端断开时状态不丢，恢复后可继续（即“取消时保存”的保证）。


def _delta(prev: str, cur: str) -> str:
    """计算 cur 相对 prev 的新增后缀，兼容“增量”与“累积”两种上游载荷，避免前端重复拼接。"""
    if not cur:
        return ""
    if prev and cur.startswith(prev):
        return cur[len(prev):]
    return cur


async def _run_envelope(thread_id: str, body: dict):
    """QwenPaw 式自研 SSE 信封：后端自己解析每个 chunk，增量吐事件，前端只管 append。

    事件：
      meta      {thread_id, model, reasoning}
      reasoning {delta}            思考增量（实时）
      token     {delta}            回答增量（实时）
      tool      {id,name,args,status:'start'|'result',result?}  工具调用可视化
      approval  {id, question}      HITL 人工裁决请求（出现即暂停等待）
      metrics   {...}              首字节/首思考/首答案/生成耗时
      error     {message}
      done      {}
    """
    llm = {
        "model": body.get("model"),
        "api_key": body.get("api_key"),
        "base_url": body.get("base_url"),
        "reasoning": body.get("reasoning"),
        "provider": body.get("provider"),
        "mode": body.get("mode") or "chat",  # chat/coding/mission，切换 Loop Gates 束与系统提示
    }
    try:
        # 图编译（build_graph().compile()）是重 CPU 活；放到线程池执行，
        # 避免阻塞事件循环导致其他聊天请求被卡住（A1 性能修复）。
        loop = asyncio.get_running_loop()
        _t_graph = time.perf_counter()
        try:
            graph, actual_reasoning = await asyncio.wait_for(
                loop.run_in_executor(_graph_build_executor, lambda: get_request_graph(llm)),
                timeout=GRAPH_BUILD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("GRAPH_BUILD_TIMEOUT thread=%s (>%.0fs)", thread_id, GRAPH_BUILD_TIMEOUT)
            yield sse("error", {"message": "图构建超时，请稍后重试或重启后端。"})
            yield sse("done", {})
            return
        logger.info("GRAPH_READY thread=%s dt=%.2fs", thread_id, time.perf_counter() - _t_graph)
    except Exception as exc:
        logger.exception("GRAPH_BUILD_FAILED thread=%s", thread_id)
        yield sse("error", {"message": f"Graph build failed: {exc}"})
        yield sse("done", {})
        return
    requested_reasoning = bool(body.get("reasoning"))
    # 同 _run_stream：不在此处 reset()，避免新消息发送瞬间统计面板闪现全 0。
    metrics = get_metrics()
    t0 = time.perf_counter()
    first_byte = first_reasoning = first_answer = False
    prev_reasoning = ""
    prev_answer = ""
    # 把不同 LLM 调用产生的 reasoning 拆成多个“思考步骤”，避免所有思考连成一大段。
    # 每当一次工具结果返回后、下一次 LLM 思考开始时，reasoning_phase 递增。
    reasoning_phase = 0
    after_tool_result = False

    # 审批走 hub（原地 Future）：前端通过 /api/approval/{thread_id} 决议，不再用 command.resume。
    input_val = {"messages": [HumanMessage(content=body.get("message", ""))]}

    # 会话索引：用于侧边栏 Sessions 面板
    _touch_session(thread_id, title=body.get("message", "")[:30])

    config: dict = {"configurable": {"thread_id": thread_id}, "recursion_limit": 200}

    # 若上一轮审批仍挂起（用户没裁决就发了新消息）：先以「拒绝」解除旧 graph 任务的挂起，
    # 再开始新消息；否则旧任务会一直停在 await future 上。
    try:
        _hub = get_hub()
        _rid = _hub.pending_for_thread(thread_id)
        if _rid:
            _hub.resolve(_rid, "rejected")
    except Exception:  # noqa: BLE001
        logger.exception("eager approval deny failed thread=%s", thread_id)

    logger.info(
        "STREAM_START thread=%s mode=%s reasoning=%s model=%s msg=%s",
        thread_id, llm.get("mode"), actual_reasoning, llm.get("model") or "demo",
        (body.get("message", "") or "")[:60],
    )
    yield sse("meta", {"thread_id": thread_id, "model": llm.get("model") or "demo", "reasoning": actual_reasoning, "mode": llm.get("mode")})
    if requested_reasoning and not actual_reasoning:
        yield sse(
            "warning",
            {"message": "思考模式已自动关闭：当前模型绑定工具时不支持同时推理，已切回普通模式保证工具可执行。"},
        )
    seen_tool_start: set = set()
    seen_tool_result: set = set()
    tool_acc: dict = {}

    # 图在后台任务跑，主循环从 out_q 抽事件（含内联审批事件），实现「原地 await」式审批。
    out_q: "asyncio.Queue" = asyncio.Queue()

    async def _emit(request_id: str, question: Any) -> None:
        await out_q.put(("__approval__", request_id, question))

    set_emitter(thread_id, _emit)

    graph_task = None
    try:
        # 仅用 messages 模式：token/思考/工具调用/工具结果都从消息块里解析，
        # 避免 updates 模式节点返回值结构（dict vs list）带来的解析不一致。
        graph_task = asyncio.create_task(_pump_graph(graph, input_val, config, out_q))
        # 保活：图在 KEEPALIVE_INTERVAL 秒内无任何事件流出（pre-LLM 准备 / 上下文加载 /
        # 工具执行等偶发阻塞），主动发 status 让前端显示「处理中」，避免看起来冻结。
        _approval_pending = False
        while True:
            try:
                _item = await asyncio.wait_for(out_q.get(), timeout=KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                if not _approval_pending:
                    yield sse("status", {"message": "正在准备上下文与工具…"})
                continue
            if _item is _SENTINEL_DONE:
                break
            if isinstance(_item, tuple) and len(_item) == 3 and _item[0] == "__approval__":
                # hitl 节点在 await future 之前内联推送的审批事件（三元组：标记 + request_id + question）
                # question 可能是 dict（如 {"question":"...","pending":"tools"}），拍平为纯字符串，
                # 否则前端 React 渲染 {m.approval.question} 会因 "Objects are not valid as a React child" 崩溃。
                _approval_pending = True
                _q = _item[2]
                if isinstance(_q, dict):
                    _q = _q.get("question") or str(_q)
                yield sse("approval", {"id": _item[1], "question": _q})
                continue
            if isinstance(_item, tuple) and len(_item) == 2 and _item[0] == "__error__":
                metrics.error("stream", "exception")
                exc_obj = _item[1]
                err_msg = str(exc_obj) or repr(exc_obj) or "unknown stream error"
                logger.exception("STREAM_ERROR thread=%s error=%s", thread_id, err_msg)
                yield sse("error", {"message": err_msg})
                break
            item = _item
            if isinstance(item, tuple) and len(item) == 2:
                msg_chunk, meta = item[0], item[1]
            else:
                msg_chunk, meta = item, {}
            # AgentNode 包装了真实 LLM，LangGraph 会同时吐出内层模型和 AgentNode 两层 chunk，
            # 内容相同，只处理内层模型即可避免 reasoning/token 重复。
            ls_provider = (meta or {}).get("ls_provider", "")
            if ls_provider == "agentnode":
                continue

            mtype = getattr(msg_chunk, "type", None)
            node = (meta or {}).get("langgraph_node")
            # 关键：图内部注入的 system 消息绝不能作为回答 token 泄漏给用户——
            # 包括 context 节点的「模式系统提示 / 折叠桩」和 governor 的「续跑提示」。
            # LangGraph 的 messages 流会把节点返回的 BaseMessage 一并吐出，这里显式拦掉。
            if mtype == "system" or node == "context":
                continue

            now_ms = round((time.perf_counter() - t0) * 1000, 2)
            if not first_byte:
                first_byte = True
                metrics.last_first_byte_ms = now_ms

            # 工具结果（ToolMessage）
            if mtype == "tool" or isinstance(msg_chunk, ToolMessage):
                tid = getattr(msg_chunk, "tool_call_id", None)
                if tid:
                    if tid not in seen_tool_start and tid in tool_acc:
                        seen_tool_start.add(tid)
                        yield sse("tool", {
                            "id": tid,
                            "name": tool_acc[tid]["name"],
                            "args": tool_acc[tid]["args"],
                            "status": "start",
                        })
                    if tid not in seen_tool_result:
                        seen_tool_result.add(tid)
                        yield sse("tool", {
                            "id": tid,
                            "status": "result",
                            "args": tool_acc.get(tid, {}).get("args", ""),
                            "result": str(getattr(msg_chunk, "content", "")),
                        })
                        # 工具结果意味着下一轮 LLM 思考即将开始，标记需要切分思考步骤
                        after_tool_result = True
                continue

            # 工具调用：流式 tool_call_chunks 会分片到达（name / arguments 逐步补全），
            # 按 id/index 累积，名称出现且未发过才发一次 start 事件。
            raw_tcs = getattr(msg_chunk, "tool_call_chunks", None)
            if not raw_tcs:
                raw_tcs = getattr(msg_chunk, "tool_calls", None) or []
            for tc in raw_tcs:
                if not isinstance(tc, dict):
                    tc = getattr(tc, "__dict__", {}) or {}
                tid = tc.get("id") or getattr(msg_chunk, "id", None)
                tidx = tc.get("index", tid)
                key = tid or tidx or "__none__"
                acc = tool_acc.setdefault(key, {"name": "", "args": "", "emitted": False})
                nm = tc.get("name")
                ar = tc.get("args")
                if nm:
                    acc["name"] = nm
                if ar is not None:
                    if isinstance(ar, str):
                        acc["args"] = acc["args"] + ar
                    elif isinstance(acc["args"], str) and acc["args"]:
                        # We already have string fragments; a complete dict arriving now
                        # likely means a custom model emitted the finished call. Replace
                        # with the structured args to avoid malformed string concatenation.
                        acc["args"] = ar
                    else:
                        acc["args"] = ar
                if acc["name"] and not acc["emitted"]:
                    acc["emitted"] = True
                    seen_tool_start.add(key)
                    yield sse("tool", {
                        "id": tid,
                        "name": acc["name"],
                        "args": acc["args"],
                        "status": "start",
                    })

            # 思考 / 回答
            content = getattr(msg_chunk, "content", None)
            if isinstance(content, str) and content:
                if not first_answer:
                    first_answer = True
                    metrics.last_first_answer_ms = now_ms
                    metrics.last_ttft_ms = now_ms
                d = _delta(prev_answer, content)
                if d:
                    prev_answer = content if content.startswith(prev_answer) else prev_answer + d
                    yield sse("token", {"delta": d})

            raw_reason = dict(getattr(msg_chunk, "additional_kwargs", {}) or {}).get("reasoning_content", "")
            if raw_reason:
                # 工具结果之后首次出现 reasoning，说明新一步 LLM 思考开始，切分步骤
                if after_tool_result:
                    reasoning_phase += 1
                    after_tool_result = False
                    prev_reasoning = ""  # 新步骤的 reasoning 是独立流，需要重置基准
                if not first_reasoning:
                    first_reasoning = True
                    metrics.last_first_reasoning_ms = now_ms
                d = _delta(prev_reasoning, raw_reason)
                if d:
                    prev_reasoning = raw_reason if raw_reason.startswith(prev_reasoning) else prev_reasoning + d
                    yield sse("reasoning", {"delta": d, "phase": reasoning_phase})

        # 图结束后收尾：审批事件已在流式过程中内联推给前端，此处无需再检查 interrupt。
        clear_emitter(thread_id)
        try:
            await graph_task
        except Exception:  # noqa: BLE001
            pass

        metrics.turn()
        metrics.last_generation_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.info(
            "STREAM_DONE thread=%s gen_ms=%.0f turns=%d gate_triggers=%s errors=%d",
            thread_id, metrics.last_generation_ms, metrics.turns,
            dict(metrics.gate_triggers or {}), metrics.errors,
        )
        yield sse("metrics", metrics.snapshot())
        yield sse("done", {})
    except Exception as e:  # noqa: BLE001
        metrics.error("stream", "exception")
        err_msg = str(e) or repr(e) or "unknown stream error"
        logger.exception("STREAM_ERROR thread=%s error=%s", thread_id, err_msg)
        yield sse("error", {"message": err_msg})
    finally:
        # 无论正常/异常，都要清理 emitter 并取消可能仍在 await future 的后台图任务
        clear_emitter(thread_id)
        if graph_task is not None and not graph_task.done():
            graph_task.cancel()


@app.post("/api/chat/{thread_id}")
async def api_chat(
    thread_id: str,
    request: Request,
    _auth: str | None = Depends(require_auth),
    _rl: None = Depends(rate_limit),
):
    """QwenPaw 式流式聊天端点。body 可携带 model/reasoning/api_key/base_url/provider 即时切换模型。"""
    body = await request.json()
    return StreamingResponse(
        _run_envelope(thread_id, body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/approval/{thread_id}")
async def api_approval(
    thread_id: str,
    request: Request,
    _auth: str | None = Depends(require_auth),
    _rl: None = Depends(rate_limit),
):
    """审批决议端点（对齐 QwenPaw 的 approve/deny）。

    前端在收到 ``approval`` SSE 事件后，调用本端点以 ``request_id`` 决议：
    - decision="approved" -> 原地解除 hitl 节点的 await future，原图**同一执行流**继续；
    - decision="rejected"（或 "timeout"）-> 同样解除，但走 rejected 分支（工具不执行）。

    决议写入共享 SQLite（多 worker 可见），持有该 future 的 worker 的 poller 会落地到
    本地 future；若就在同一 worker，直接 set_result。超时由 hub 的 wait_for 自动否决。
    """
    body = await request.json()
    request_id = body.get("request_id") or body.get("id")
    decision = str(body.get("decision") or "approved").strip().lower()
    if decision not in {"approved", "rejected", "timeout"}:
        return JSONResponse(
            {"ok": False, "error": "decision must be approved, rejected or timeout"},
            status_code=400,
        )
    hub = get_hub()
    if not request_id:
        return JSONResponse({"ok": False, "error": "missing request_id"}, status_code=400)
    # 校验该 request_id 确实属于本 thread 的未决审批（防止越权决议）
    if hub.pending_for_thread(thread_id) != request_id:
        # 跨 worker 场景：本 worker 不持有该 future，但共享表里有，resolve 仍生效
        rows = [r for r in hub._store_fetch(thread_id) if r["request_id"] == request_id and r["status"] == "pending"]
        if not rows:
            return JSONResponse({"ok": False, "error": "unknown or expired request_id"}, status_code=404)
    hub.resolve(request_id, decision)
    return JSONResponse({"ok": True, "request_id": request_id, "decision": decision})


@app.post("/threads/{thread_id}/runs/stream")
async def run_stream(
    thread_id: str,
    request: Request,
    _auth: str | None = Depends(require_auth),
    _rl: None = Depends(rate_limit),
):
    body = await request.json()
    return StreamingResponse(
        _run_stream(thread_id, body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/threads/{thread_id}/runs")
async def create_run(
    thread_id: str,
    request: Request,
    _auth: str | None = Depends(require_auth),
    _rl: None = Depends(rate_limit),
):
    """非流式 create（兜底；UI 主要走 stream）。"""
    body = await request.json()
    graph = get_graph()
    command = body.get("command")
    if isinstance(command, dict) and any(k in command for k in ("resume", "goto", "update")):
        input_val = Command(resume=command.get("resume"), goto=command.get("goto"), update=command.get("update"))
    else:
        input_val = body.get("input") or {}
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 200}
    result = await graph.ainvoke(input_val, config)
    get_metrics().turn()
    return {
        "run_id": uuid.uuid4().hex,
        "thread_id": thread_id,
        "status": "success",
        "result": jsonable_state(result),
    }


@app.get("/metrics")
async def metrics():
    return get_metrics().snapshot()


@app.post("/metrics/reset")
async def metrics_reset():
    """手动清空累积统计（统计现在跨请求累积，提供重置入口）。"""
    get_metrics().reset()
    return {"ok": True}


@app.get("/modes")
async def modes():
    """返回可用的 AgentMode 列表（chat/coding/mission），供前端模式选择器渲染。"""
    from ..harness.agentmode import list_modes

    return {"modes": list_modes()}


@app.get("/context/{thread_id}")
async def context_inspect(thread_id: str, preview: int = 200):
    """上下文检视：返回某会话已写穿的全部轮次、折叠情况、预算与召回开关。

    供前端「上下文面板」渲染 fold-not-summarize 的真实状态（哪些 seq 被折叠、
    窗口大小、总轮次、是否启用召回），让工业级上下文管理可观测、可解释。
    """
    cm = get_context_manager()
    return cm.inspect(thread_id, preview_chars=preview)


# ---------------------------------------------------------------------------
# 运行时配置：前端设置面板读写（保存即生效，无需重启）
# ---------------------------------------------------------------------------
@app.get("/config")
async def get_config_endpoint(
    _auth: str | None = Depends(require_auth),
):
    """返回当前配置（脱敏）。鉴权开启时须携带令牌。

    注意：本端点不受 rate_limiter 约束，否则一旦限流误设过严，
    /config 自身也会被 429，导致再也无法 POST 关闭限流（自锁）。
    """
    return get_config().masked()


@app.post("/config")
async def post_config(
    payload: dict = {},
    _auth: str | None = Depends(require_auth),
):
    """合并并持久化配置（api_key/base_url/model/service token/enable_auth/rate_limit）。

    保存后图工厂按版本号自动重建，新的 LLM 配置即时生效。
    注意：未启用 TLS 时通过 HTTP 发送 api_key 有被窃听风险，请仅本地或经反向代理使用。
    """
    cfg = save_config(payload or {})
    return {"ok": True, "config": cfg.masked()}


@app.get("/")
async def index():
    return {
        "service": "agent-harness",
        "assistant_id": get_assistant_id(),
        "endpoints": ["/ok", "/assistants", "/threads", "/threads/{id}/runs/stream", "/metrics", "/providers", "/models",
                      "/workspace", "/tools", "/skills", "/mcp", "/acp", "/agents",
                      "/cron", "/cron/{id}/run", "/cron/history", "/cron/validate", "/heartbeat", "/sessions"],
    }


# ---------------------------------------------------------------------------
# 模型厂商目录 + 发现 / 校验
# ---------------------------------------------------------------------------
@app.get("/providers")
async def providers():
    """返回内置厂商目录（id/label/base_url/已知模型/reasoning 支持）。无需鉴权。"""
    return {"providers": list_providers()}


@app.post("/models")
async def models_discover(
    request: Request,
    _rl: None = Depends(rate_limit),
):
    """发现并校验模型：给定 base_url + api_key，返回可用模型列表 + 密钥是否有效。

    body: {"base_url": str, "api_key": str}
    返回: {"ok","provider","key_valid","discovered","supports_reasoning","models":[...],"error"}
    """
    body = await request.json()
    base_url = (body.get("base_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "缺少 api_key（无法校验）"},
        )
    result = await discover_models(base_url, api_key)
    return result
