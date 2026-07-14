# 排查报告：qwen3.6-plus 响应慢 vs QwenPaw

## 完成的工作
1. 修正了 Qwen3 系列（qwen3.6-plus）的 thinking/reasoning 参数格式。
2. 前端新增 thinking（思考链）实时展示与折叠查看。
3. 修复了 metrics 全局单例导致的累计值误导问题。
4. 把首 token 指标拆成首字节 / 首思考 / 首答案 / 生成总耗时，便于定位慢根因。
5. **关键修复**：将 `AgentNode` 从 `Runnable` 改为继承 `BaseChatModel`，让 LangGraph `stream_mode="messages"` 能真正捕获 token 级事件；前端现在能像 QwenPaw 一样实时看到思考和答案逐字出现，左下角指标也不再为 0。

## 关键发现
- qwen3.6-plus 在 `reasoning=false` 时：首答案约 2.6s，总耗时约 2.7s。
- qwen3.6-plus 在 `reasoning=true` 时：首思考约 1.7s 可见，首答案约 5.2s。
- 之前慢的主要原因是：
  - 开启 thinking 时参数错误，导致请求异常或 fallback。
  - agent 节点不是 `BaseChatModel`，LangGraph 无法产生 `messages` 流式事件；前端只能等整段思考+答案生成完才一次性显示，造成"咣"一下卡住的错觉。
  - 指标只在 `messages` 事件中统计，因此首字节/首思考/首答案全部为 0。

## 修改的文件
- `src/harness/models.py`：qwen3 用 `reasoning={"type": "enabled"}`。
- `src/harness/agent.py`：`AgentNode` 改为 `BaseChatModel` 子类，实现标准 ChatModel 接口；入口方法从 state dict 提取 messages；流式时累积 `reasoning_content`。
- `src/harness/graph.py`：`context` 节点用 `Overwrite(window)` 替换 `messages`，让 chat model 节点读到折叠后的窗口。
- `src/harness/stability/observability.py`：新增 reset() 与更细粒度时间指标。
- `src/server/app.py`：过滤底层 LLM 原始 `messages` 事件，只保留 AgentNode 归一化事件；`_extract_stream_bits` 同时读取 `additional_kwargs.reasoning_content`；每次请求重置 metrics，SSE 层实时统计时间；统一在 stream/create_run 中调用 `metrics.turn()`。
- `frontend/src/App.tsx`：解析 Responses API 内容列表、默认展开显示 thinking、修复 toggleReasoning 传参、添加 "思考中…" 脉冲提示。
- `frontend/src/index.css`：thinking 展示样式与脉冲动画。

## 运行方式
```bash
cd /Users/xjh/Documents/projects/merge/agent-harness
bash start.sh
```
然后打开 http://localhost:5173 。在「设置 / 配置 API」中可切换思考模式。

## 验证结果
- 26 个 pytest 用例全部通过。
- 前端 `npm run build` 成功。
- SSE 实测 `messages` 事件逐 reasoning/answer chunk 到达，`/metrics` 首字节、首思考、首答案、生成耗时均非 0。

## 后续建议
- 若仍追求更快首答案，可换用 `qwen-turbo` 等轻量模型。
- 工具调用链路目前仍走 ChatOpenAI 非流式，后续若工具调用也慢，可考虑自定义模型层完全绕过 Responses API。
