import { useCallback, useEffect, useMemo, useRef, useState, memo } from "react";
import {
  FilesPanel,
  ToolsPanel,
  SkillsPanel,
  McpPanel,
  AcpPanel,
  AgentsPanel,
  SessionsPanel,
  CronPanel,
  HeartbeatPanel,
  StatsPanel,
  ConfigPanel,
  ChannelsPanel,
} from "./panels/Plugins";
import { MemoryPanel } from "./panels/Memory";
import { ContextPanel as ContextManagerPanel } from "./panels/Context";
import {
  ModelsPanel,
  EnvsPanel,
  SecurityPanel,
  TokenUsagePanel,
  BackupsPanel,
  VoicePanel,
  DebugPanel,
} from "./panels/Settings";

// ===========================================================================
// Agent Harness · 全功能侧边栏导航 + 自研 SSE 信封
// ===========================================================================

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8123";
// 会话按用户命名空间隔离：多用户共享同一浏览器/Origin 时不串台
const AUTH_TOKEN_KEY = "ah_auth_token";
const AUTH_USER_KEY = "ah_user";
const LEGACY_SESSIONS_KEY = "harness_sessions_v1";
const SESSIONS_KEY = () =>
  `harness_sessions_v1_${localStorage.getItem(AUTH_USER_KEY) || "default"}`;
const BRAND_NAME = "Agent Harness";
const BRAND_VERSION = "v0.1.0";
const BRAND_LOGO = "🐾";

// ---------------------------------------------------------------------------
// 侧边栏 SVG 图标（QwenPaw 风格：线性、1.5px 描边）
// ---------------------------------------------------------------------------
const Icon = ({ children }: { children: React.ReactNode }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    {children}
  </svg>
);

const ChatIcon = () => <Icon><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7A8.38 8.38 0 0 1 4 11.5a8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" /></Icon>;
const ChannelsIcon = () => <Icon><path d="M4 17h6m-3-4h7m-4-4h10M5 7l14 14" /><circle cx="6.5" cy="6.5" r="2.5" /><circle cx="11.5" cy="11.5" r="2.5" /><circle cx="16.5" cy="16.5" r="2.5" /></Icon>;
const SessionsIcon = () => <Icon><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></Icon>;
const CronIcon = () => <Icon><rect x="3" y="4" width="18" height="18" rx="2" ry="2" /><line x1="16" y1="2" x2="16" y2="6" /><line x1="8" y1="2" x2="8" y2="6" /><line x1="3" y1="10" x2="21" y2="10" /></Icon>;
const HeartbeatIcon = () => <Icon><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></Icon>;
const FilesIcon = () => <Icon><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></Icon>;
const SkillsIcon = () => <Icon><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></Icon>;
const ToolsIcon = () => <Icon><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></Icon>;
const McpIcon = () => <Icon><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></Icon>;
const AcpIcon = () => <Icon><circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" /><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" /></Icon>;
const MemoryIcon = () => <Icon><path d="M9 18h6m-5 3h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.3h6c0-1 .4-1.8 1-2.3A7 7 0 0 0 12 2z" /></Icon>;
const ContextIcon = () => <Icon><polygon points="12 2 2 7 12 12 22 7 12 2" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" /></Icon>;
const ConfigIcon = () => <Icon><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" /></Icon>;
const StatsIcon = () => <Icon><line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" /></Icon>;
const AgentsIcon = () => <Icon><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><line x1="12" y1="19" x2="12" y2="23" /><line x1="8" y1="23" x2="16" y2="23" /></Icon>;
const ModelsIcon = () => <Icon><rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" /><line x1="9" y1="1" x2="9" y2="4" /><line x1="15" y1="1" x2="15" y2="4" /><line x1="9" y1="20" x2="9" y2="23" /><line x1="15" y1="20" x2="15" y2="23" /><line x1="20" y1="9" x2="23" y2="9" /><line x1="20" y1="14" x2="23" y2="14" /><line x1="1" y1="9" x2="4" y2="9" /><line x1="1" y1="14" x2="4" y2="14" /></Icon>;
const EnvsIcon = () => <Icon><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></Icon>;
const SecurityIcon = () => <Icon><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></Icon>;
const TokenUsageIcon = () => <Icon><line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" /></Icon>;
const BackupsIcon = () => <Icon><polyline points="21 8 21 21 3 21 3 8" /><rect x="1" y="3" width="22" height="5" /><line x1="10" y1="12" x2="14" y2="12" /></Icon>;
const VoiceIcon = () => <Icon><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><line x1="12" y1="19" x2="12" y2="23" /><line x1="8" y1="23" x2="16" y2="23" /></Icon>;
const DebugIcon = () => <Icon><polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" /></Icon>;

const ICONS: Record<string, React.ReactNode> = {
  chat: <ChatIcon />,
  channels: <ChannelsIcon />,
  sessions: <SessionsIcon />,
  cron: <CronIcon />,
  heartbeat: <HeartbeatIcon />,
  files: <FilesIcon />,
  skills: <SkillsIcon />,
  tools: <ToolsIcon />,
  mcp: <McpIcon />,
  acp: <AcpIcon />,
  memory: <MemoryIcon />,
  context: <ContextIcon />,
  config: <ConfigIcon />,
  stats: <StatsIcon />,
  agents: <AgentsIcon />,
  models: <ModelsIcon />,
  envs: <EnvsIcon />,
  security: <SecurityIcon />,
  tokenusage: <TokenUsageIcon />,
  backups: <BackupsIcon />,
  voice: <VoiceIcon />,
  debug: <DebugIcon />,
};

type ToolCall = {
  id?: string;
  name?: string;
  args?: any;
  status: "start" | "result";
  result?: string;
};

type Msg = {
  id: string;
  role: "user" | "ai" | "tool";
  content: string;
  // reasoning 改为数组：每个元素对应一次 LLM 调用产生的思考步骤，
  // 与工具调用交错展示，避免所有思考连成一大段。
  reasoning?: string[];
  toolCalls: ToolCall[];
  status: "streaming" | "paused" | "done" | "error";
  error?: string;
  approval?: { id?: string; question: string };
  // 后端保活：pre-LLM 准备 / 上下文加载等偶发阻塞时，后端发 status 事件，
  // 前端显示「处理中」提示，避免看起来像卡死。
  statusNote?: string;
};

type Session = {
  id: string;
  title: string;
  messages: Msg[];
  updatedAt: number;
  // 创建时间（createdAt）用于识别"主会话"，inbox 等全局通知只在这里显示
  createdAt?: number;
};

type Settings = {
  provider: string;
  apiKey: string;
  baseUrl: string;
  model: string;
  reasoning: boolean;
  enableAuth: boolean;
  serviceKey: string;
  trustMode: boolean;
};

// 对齐 QwenPaw 真实 agent-config 结构（字段名/嵌套与 qwenpaw.config 一致）
type RuntimeConfig = {
  language: string;
  approval_level: string; // STRICT | SMART | AUTO | OFF
  plan: { enabled: boolean };
  rate_limiter: { enabled: boolean; requests_per_minute: number };
  running: {
    max_iters: number;
    loop: {
      iteration: { enabled: boolean; max_iterations: number | null };
      doom_loop: { enabled: boolean; window_size: number; similarity_threshold: number };
      rubric: { enabled: boolean };
    };
    llm_retry_enabled: boolean;
    llm_max_retries: number;
    llm_backoff_base: number;
    shell_command_timeout: number;
    max_input_length: number;
    context_manager_backend: string;
    memory_manager_backend: string;
    approval_timeout_seconds: number;
    reme_light_memory_config: { dream_cron: string; auto_memory_interval: number };
  };
};

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------
function uid() {
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

// 把后端 LangGraph 序列化的消息（human/ai/tool）转成前端 Msg[]。
// 工具结果（ToolMessage）按 tool_call_id 合并进对应 AI 消息的 toolCalls.result；
// 思考（additional_kwargs.reasoning_content）降级成单段 reasoning。
// 用于「回到这里 / 分叉」后把后端真相源同步回前端本地消息。
function textOf(c: any): string {
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.map((p: any) => (p && p.text ? p.text : "")).join("");
  if (c && typeof c === "object") return JSON.stringify(c);
  return "";
}
function backendMessagesToMsgs(raw: any[]): Msg[] {
  const toolResults = new Map<string, string>();
  for (const r of raw || []) {
    if (r?.type === "tool" || r?.type === "tool_message") {
      const tcid = r.tool_call_id;
      if (tcid != null) toolResults.set(tcid, textOf(r.content));
    }
  }
  const out: Msg[] = [];
  for (const r of raw || []) {
    const t = r?.type;
    if (t === "human" || t === "user") {
      out.push({ id: uid(), role: "user", content: textOf(r.content), toolCalls: [], status: "done" });
    } else if (t === "ai" || t === "assistant") {
      const tcs = (r.tool_calls || []).map((tc: any) => ({
        id: tc.id,
        name: tc.name,
        args: tc.args,
        status: "result" as const,
        result: tc.id ? toolResults.get(tc.id) : undefined,
      }));
      const rk = r.additional_kwargs?.reasoning_content;
      const reasoning = rk
        ? [rk]
        : Array.isArray(r.reasoning)
          ? r.reasoning
          : r.reasoning
            ? [r.reasoning]
            : [];
      out.push({
        id: uid(),
        role: "ai",
        content: textOf(r.content),
        reasoning: reasoning.length ? reasoning : [],
        toolCalls: tcs,
        status: "done",
      });
    }
  }
  return out;
}

// 读取当前用户的本地会话缓存（含消息，用于显示持久化），兼容旧版全局 key
function readLocalSessions(): Session[] {
  try {
    const user = localStorage.getItem(AUTH_USER_KEY) || "default";
    let raw = localStorage.getItem(`harness_sessions_v1_${user}`);
    if ((!raw || raw === "[]") && user === "default") {
      raw = localStorage.getItem(LEGACY_SESSIONS_KEY);
    }
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed && Array.isArray(parsed)) {
      // 兼容旧格式：reasoning 从 string 迁移为 string[]
      for (const s of parsed) {
        for (const m of s.messages || []) {
          if (m.role === "ai" && typeof m.reasoning === "string") {
            m.reasoning = m.reasoning ? [m.reasoning] : [];
          }
        }
        // 兼容旧格式：createdAt 缺失时退化为 updatedAt
        if (!s.createdAt && s.updatedAt) s.createdAt = s.updatedAt;
      }
      return parsed;
    }
  } catch {}
  return [];
}

function apiFetch(path: string, opts: any = {}) {
  const authToken = localStorage.getItem("ah_auth_token") || "";
  const svcToken = localStorage.getItem("svc_token") || "";
  const headers: any = { ...(opts.headers || {}) };
  // 多用户鉴权：优先携带登录签发的 Bearer 令牌；未登录时退回旧的 service key 模式。
  if (authToken) headers["Authorization"] = "Bearer " + authToken;
  else if (svcToken) headers["x-api-key"] = svcToken;
  return fetch(API_URL + path, { ...opts, headers });
}

// 解析单条 SSE block（服务器始终单行 JSON）
function parseBlock(block: string): { event: string; data: any } | null {
  let event = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).replace(/^ /, "");
  }
  if (!data) return null;
  try {
    return { event, data: JSON.parse(data) };
  } catch {
    return { event, data: null };
  }
}

// 解析整个 SSE 流，逐事件回调
async function streamChat(
  url: string,
  body: any,
  onEvent: (ev: { event: string; data: any }) => void
): Promise<void> {
  let terminalEmitted = false;
  const emit = (ev: { event: string; data: any }) => {
    if (ev.event === "error" || ev.event === "done") terminalEmitted = true;
    if (ev.event === "approval") {
      // 审批是预期的长时间暂停（等用户裁决），不要因空闲超时断开 SSE，
      // 否则用户思考超过 90s 后原图继续跑完、前端却收不到续流。
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => controller.abort(), 3_600_000); // 1h 宽限
    }
    onEvent(ev);
  };
  // 空闲超时：如果 90 秒内没有收到任何数据，主动断开连接并报错，
  // 防止后端 hang 住时前端输入框永久锁死。
  const controller = new AbortController();
  let idleTimer: ReturnType<typeof setTimeout> | null = null;
  const resetIdle = () => {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => controller.abort(), 90_000);
  };
  resetIdle();
  try {
    const res = await apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      const txt = await res.text().catch(() => res.statusText);
      emit({ event: "error", data: { message: txt || res.statusText } });
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      resetIdle();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const ev = parseBlock(block);
        if (ev) emit(ev);
      }
    }
    if (buf.trim()) {
      const ev = parseBlock(buf);
      if (ev) emit(ev);
    }
  } catch (err: any) {
    if (controller.signal.aborted) {
      emit({ event: "error", data: { message: "连接超时（90秒无数据），请重试" } });
    } else {
      emit({ event: "error", data: { message: err?.message || String(err) || "流式连接失败" } });
    }
  } finally {
    if (idleTimer) clearTimeout(idleTimer);
    // 兜底：如果服务端未正常发送 done/error，确保输入框能重新解锁
    if (!terminalEmitted) {
      emit({ event: "done", data: {} });
    }
  }
}

// ---------------------------------------------------------------------------
// 主应用
// ---------------------------------------------------------------------------
export default function App() {
  // 主导航视图
  const [view, setView] = useState<string>(() => localStorage.getItem("harness_view") || "chat");
  const [agents, setAgents] = useState<any[]>([]);
  const [currentAgent, setCurrentAgent] = useState<any>({ id: "default", name: "Default Agent", emoji: "🤖" });

  // 会话与状态（按当前用户命名空间持久化在 localStorage）
  const [sessions, setSessions] = useState<Session[]>(() => readLocalSessions());
  const [activeId, setActiveId] = useState<string>("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [metrics, setMetrics] = useState<any>(null);
  // 收件箱：定时任务/后台任务结果投递（对齐 QwenPaw 的 /inbox/events，默认弹出在会话窗口）
  const [inboxEvents, setInboxEvents] = useState<any[]>([]);
  const [inboxUnread, setInboxUnread] = useState<number>(0);
  // AgentMode（chat/coding/mission）
  const [mode, setMode] = useState<string>(() => localStorage.getItem("harness_mode") || "chat");
  const [modes, setModes] = useState<{ name: string; description: string }[]>([]);
  // 上下文检视面板
  const [ctxOpen, setCtxOpen] = useState(false);
  const [ctxInfo, setCtxInfo] = useState<any>(null);
  const [ctxLoading, setCtxLoading] = useState(false);
  // 检查点时间轴面板（checkpoint 回滚 / 分叉 / 重放）
  const [histOpen, setHistOpen] = useState(false);
  const [histSteps, setHistSteps] = useState<any[]>([]);
  const [histLoading, setHistLoading] = useState(false);
  const [histError, setHistError] = useState<string | null>(null);
  const [rollingBack, setRollingBack] = useState(false);
  // 逃生舱提示
  const [toast, setToast] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings>({
    provider: "",
    apiKey: "",
    baseUrl: "",
    model: "deepseek-chat",
    reasoning: false,
    enableAuth: false,
    serviceKey: "",
    trustMode: false,
  });
  const DEFAULT_RUNTIME_CONFIG: RuntimeConfig = {
    language: "zh",
    approval_level: "AUTO",
    plan: { enabled: false },
    rate_limiter: { enabled: false, requests_per_minute: 60 },
    running: {
      max_iters: 100,
      loop: {
        iteration: { enabled: true, max_iterations: null },
        doom_loop: { enabled: true, window_size: 3, similarity_threshold: 1.0 },
        rubric: { enabled: false },
      },
      llm_retry_enabled: true,
      llm_max_retries: 3,
      llm_backoff_base: 1.0,
      shell_command_timeout: 60,
      max_input_length: 131072,
      context_manager_backend: "light",
      memory_manager_backend: "remelight",
      approval_timeout_seconds: 300,
      reme_light_memory_config: { dream_cron: "0 23 * * *", auto_memory_interval: 5 },
    },
  };
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig>(DEFAULT_RUNTIME_CONFIG);

  // 厂商目录 + 发现结果
  const [providers, setProviders] = useState<any[]>([]);
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);
  const [reasoningModels, setReasoningModels] = useState<string[]>([]);
  const [discoverState, setDiscoverState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [discoverMsg, setDiscoverMsg] = useState<string | null>(null);
  const [keyValid, setKeyValid] = useState<boolean | null>(null);
  const [settingsMsg, setSettingsMsg] = useState<string | null>(null);
  const [authEnabled, setAuthEnabled] = useState(false);

  // 多用户鉴权状态
  const [authToken, setAuthToken] = useState<string>(() => localStorage.getItem(AUTH_TOKEN_KEY) || "");
  const [authUser, setAuthUser] = useState<string>(() => localStorage.getItem(AUTH_USER_KEY) || "default");
  const [showLogin, setShowLogin] = useState<boolean>(false);
  const [loginMsg, setLoginMsg] = useState<string | null>(null);
  const [loginLoading, setLoginLoading] = useState(false);
  // 登录成功后触发配置/模型等重新拉取
  const [cfgReload, setCfgReload] = useState(0);

  // 流式相关
  const [streaming, setStreaming] = useState(false);
  const activeIdRef = useRef(activeId);
  const streamingRef = useRef<string | null>(null);
  const pendingApprovalRef = useRef<string | null>(null);
  const approvalRequestIdRef = useRef<string | null>(null);
  const modeRef = useRef(mode);
  const resumeRef = useRef<((d: string) => void) | null>(null);
  activeIdRef.current = activeId;
  modeRef.current = mode;

  // 持久化当前视图
  useEffect(() => {
    localStorage.setItem("harness_view", view);
  }, [view]);

  // 初始化：探测鉴权开关；必要时展示登录页；否则确保至少有一个会话
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await apiFetch("/auth/status");
        const d = await r.json().catch(() => ({ enable_auth: false }));
        if (cancelled) return;
        const enabled = !!d.enable_auth;
        setAuthEnabled(enabled);
        if (enabled && !localStorage.getItem(AUTH_TOKEN_KEY)) {
          setShowLogin(true);
          return;
        }
      } catch {
        // 后端不可达：按无鉴权处理，允许纯本地使用
      }
      if (cancelled) return;
      const local = readLocalSessions();
      if (local.length) {
        setSessions(local);
        if (!activeId) setActiveId(local[0].id);
      } else {
        await newSession();
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 持久化会话（防抖：流式输出时每 token 都会改 sessions，
  // 如果每次都 JSON.stringify + localStorage.setItem 会阻塞主线程数秒，
  // 导致 UI 卡死。改为延迟 800ms 合并写入。）
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!sessions.length) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      try {
        localStorage.setItem(SESSIONS_KEY(), JSON.stringify(sessions));
      } catch {}
    }, 800);
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, [sessions]);

  const activeSession = useMemo(
    () => sessions.find((s) => s.id === activeId) || null,
    [sessions, activeId]
  );

  // "主会话"：创建时间最早的会话，inbox 等全局通知只在这里显示，
  // 避免新建会话里冒出无关的定时任务结果。
  const homeSessionId = useMemo(() => {
    if (!sessions.length) return "";
    return [...sessions].sort((a, b) => (a.createdAt || a.updatedAt) - (b.createdAt || b.updatedAt))[0].id;
  }, [sessions]);

  // 新建会话：先向后端注册（归属当前用户，写入 sessions 索引），
  // 用后端返回的 thread_id 作为本地会话 id，保证聊天 thread 与后端一致。
  const newSession = useCallback(async (): Promise<string> => {
    let id = uid();
    try {
      const r = await apiFetch("/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (r.ok) {
        const d = await r.json().catch(() => ({}));
        if (d.id) id = d.id;
      }
    } catch {
      // 后端不可达时退化为纯本地会话（仍可用，但不进后端索引）
    }
    const s: Session = { id, title: "新对话", messages: [], updatedAt: Date.now(), createdAt: Date.now() };
    setSessions((prev) => [s, ...prev.filter((x) => x.id !== id)]);
    setActiveId(id);
    setView("chat");
    return id;
  }, []);

  // 登录：校验用户名/密码，成功后保存令牌并重新加载该用户的会话
  const doLogin = async (username: string, password: string) => {
    setLoginLoading(true);
    setLoginMsg(null);
    try {
      const r = await fetch(API_URL + "/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.token) {
        localStorage.setItem(AUTH_TOKEN_KEY, d.token);
        localStorage.setItem(AUTH_USER_KEY, d.user_id || username);
        setAuthToken(d.token);
        setAuthUser(d.user_id || username);
        setShowLogin(false);
        setCfgReload((x) => x + 1); // 重新拉取配置/模型（带新令牌）
        const local = readLocalSessions();
        if (local.length) {
          setSessions(local);
          setActiveId(local[0].id);
        } else {
          await newSession();
        }
      } else {
        setLoginMsg(d.error || "登录失败");
      }
    } catch (e: any) {
      setLoginMsg("登录请求失败：" + String(e?.message || e));
    } finally {
      setLoginLoading(false);
    }
  };

  // 退出登录：清除令牌，回到登录页
  const doLogout = () => {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    localStorage.removeItem(AUTH_USER_KEY);
    setAuthToken("");
    setAuthUser("default");
    setSessions([]);
    setShowLogin(true);
  };

  // 删除会话：同步清除本地缓存 + 通知后端
  const deleteSession = useCallback((id: string) => {
    setSessions((prev) => prev.filter((s) => s.id !== id));
    apiFetch(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
  }, []);

  // 批量更新：流式 token 事件频率很高（每 token 一次 updateMsg），
  // 如果每次都 setSessions 会触发全量 re-render。改为累积到 ref，
  // 每 40ms 合并 flush 一次，把 200 次 render 降到 ~25 次/秒。
  const pendingUpdates = useRef<Map<string, (m: Msg) => Msg>>(new Map());
  const flushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const updateMsg = useCallback((msgId: string, fn: (m: Msg) => Msg) => {
    // 组合同一消息的多个更新函数（token 逐字追加）
    const existing = pendingUpdates.current.get(msgId);
    if (existing) {
      pendingUpdates.current.set(msgId, (m) => fn(existing(m)));
    } else {
      pendingUpdates.current.set(msgId, fn);
    }
    if (flushTimer.current) return; // 已有定时器在等
    flushTimer.current = setTimeout(() => {
      flushTimer.current = null;
      const updates = pendingUpdates.current;
      if (!updates.size) return;
      pendingUpdates.current = new Map();
      const sid = activeIdRef.current;
      setSessions((prev) =>
        prev.map((s) =>
          s.id === sid
            ? {
                ...s,
                messages: s.messages.map((m) => {
                  const u = updates.get(m.id);
                  return u ? u(m) : m;
                }),
              }
            : s
        )
      );
    }, 80);  // 80ms 合并窗口：减少流式重渲染频率（原 40ms=25次/s → 80ms=12次/s），人眼无感知差异但 UI 不抖
  }, []);

  // 拉取配置回填 + 厂商目录（登录后 cfgReload 变化会重新拉取）
  useEffect(() => {
    apiFetch("/config")
      .then((r) => r.json())
      .then((d: any) => {
        setSettings((f) => ({
          ...f,
          provider: d.llm?.provider || "",
          apiKey: d.llm?.api_key === "****" ? "" : d.llm?.api_key || "",
          baseUrl: d.llm?.base_url || "",
          model: d.llm?.model || "deepseek-chat",
          reasoning: !!d.llm?.reasoning,
          enableAuth: !!d.security?.enable_auth,
          trustMode: d.approval_level === "OFF",
          serviceKey: d.service?.api_key === "****" ? "" : d.service?.api_key || "",
        }));
        setRuntimeConfig({
          language: d.language || "zh",
          approval_level: d.approval_level || "AUTO",
          plan: { ...DEFAULT_RUNTIME_CONFIG.plan, ...(d.plan || {}) },
          rate_limiter: { ...DEFAULT_RUNTIME_CONFIG.rate_limiter, ...(d.rate_limiter || {}) },
          running: { ...DEFAULT_RUNTIME_CONFIG.running, ...(d.running || {}) },
        });
        setAuthEnabled(!!d.security?.enable_auth);
      })
      .catch(() => {});
  }, [cfgReload]);

  // 指标轮询
  useEffect(() => {
    let alive = true;
    const load = () =>
      apiFetch("/metrics")
        .then((r) => r.json())
        .then((d) => alive && setMetrics(d))
        .catch(() => {});
    load();
    const t = setInterval(load, 2000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // 收件箱轮询：拉取 cron 结果事件，渲染进会话窗口（对齐 QwenPaw /inbox/events 默认弹出）
  const loadInbox = useCallback(() => {
    apiFetch("/inbox/events?source_type=cron&limit=30")
      .then((r) => r.json())
      .then((d) => {
        const evs = Array.isArray(d.events) ? d.events : [];
        setInboxEvents(evs);
        setInboxUnread(typeof d.unread === "number" ? d.unread : 0);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadInbox();
    const t = setInterval(loadInbox, 3000);
    return () => clearInterval(t);
  }, [loadInbox]);

  const markInboxRead = useCallback((ids: string[]) => {
    if (!ids.length) return;
    apiFetch("/inbox/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    })
      .then(() => loadInbox())
      .catch(() => {});
  }, [loadInbox]);

  const markAllInboxRead = useCallback(() => {
    apiFetch("/inbox/read/all", { method: "POST" })
      .then(() => loadInbox())
      .catch(() => {});
  }, [loadInbox]);

  const dismissInbox = useCallback((id: string) => {
    apiFetch("/inbox/events/" + id, { method: "DELETE" })
      .then(() => loadInbox())
      .catch(() => {});
  }, [loadInbox]);

  // 拉取可用 AgentMode 列表
  useEffect(() => {
    apiFetch("/modes")
      .then((r) => r.json())
      .then((d) => setModes(d.modes || []))
      .catch(() => {});
  }, []);

  // 拉取 Agent 列表
  useEffect(() => {
    apiFetch("/agents")
      .then((r) => r.json())
      .then((d) => {
        setAgents(d.agents || []);
        const def = (d.agents || []).find((a: any) => a.id === currentAgent.id) || (d.agents || [])[0];
        if (def) setCurrentAgent(def);
      })
      .catch(() => {});
  }, [view]);

  // 持久化当前模式
  useEffect(() => {
    localStorage.setItem("harness_mode", mode);
  }, [mode]);

  // 轻量 toast
  const flash = useCallback((text: string) => {
    setToast(text);
    window.setTimeout(() => setToast(null), 2400);
  }, []);

  // 拉取上下文检视
  const loadContext = useCallback(async () => {
    const sid = activeIdRef.current;
    if (!sid) return;
    setCtxLoading(true);
    try {
      const r = await apiFetch(`/context/${sid}`);
      const d = await r.json();
      setCtxInfo(d);
    } catch {
      setCtxInfo(null);
    } finally {
      setCtxLoading(false);
    }
  }, []);

  useEffect(() => {
    if (ctxOpen) loadContext();
  }, [ctxOpen, activeId, loadContext]);

  useEffect(() => {
    if (!streaming && ctxOpen) loadContext();
  }, [streaming, ctxOpen, loadContext]);

  // 事件分发
  const handleEvent = useCallback(
    (ev: { event: string; data: any }, msgId: string) => {
      switch (ev.event) {
        case "meta":
          // 后端可能在绑定工具时强制关闭 reasoning，同步到本地开关
          if (ev.data?.reasoning === false && settings.reasoning) {
            setSettings((f: Settings) => ({ ...f, reasoning: false }));
          }
          break;
        case "warning":
          flash(ev.data?.message || "后端警告");
          break;
        case "status":
          // 后端保活：图在数秒内无事件流出（pre-LLM 准备等偶发阻塞），
          // 显示「处理中」提示，避免看起来像卡死。
          updateMsg(msgId, (m) => ({ ...m, statusNote: ev.data?.message || "处理中…" }));
          break;
        case "reasoning": {
          const phase = typeof ev.data?.phase === "number" ? ev.data.phase : 0;
          updateMsg(msgId, (m) => {
            const arr = m.reasoning ? [...m.reasoning] : [];
            arr[phase] = (arr[phase] || "") + (ev.data?.delta || "");
            return { ...m, reasoning: arr };
          });
          break;
        }
        case "token":
          updateMsg(msgId, (m) => ({ ...m, content: (m.content || "") + (ev.data?.delta || "") }));
          break;
        case "tool":
          updateMsg(msgId, (m) => {
            const tcs = [...(m.toolCalls || [])];
            const i = tcs.findIndex((t) => t.id && t.id === ev.data?.id);
            if (i >= 0) tcs[i] = { ...tcs[i], ...ev.data };
            else tcs.push({ id: ev.data?.id, name: ev.data?.name, args: ev.data?.args, status: ev.data?.status, result: ev.data?.result });
            return { ...m, toolCalls: tcs };
          });
          break;
        case "approval":
          // question 可能是 string 或 dict（如 {"question":"...","pending":"tools"}），统一提取为纯字符串
          {
            const rawQ = ev.data?.question;
            const qStr = typeof rawQ === "string" ? rawQ : (rawQ?.question || "需要人工裁决");
            updateMsg(msgId, (m) => ({
              ...m,
              approval: { id: ev.data?.id, question: qStr },
              status: "paused",
            }));
          }
          setStreaming(false);
          streamingRef.current = null;
          pendingApprovalRef.current = msgId;
          approvalRequestIdRef.current = ev.data?.id || null;
          break;
        case "metrics":
          setMetrics(ev.data);
          break;
        case "error":
          updateMsg(msgId, (m) => ({ ...m, status: "error", error: ev.data?.message || "未知错误" }));
          setStreaming(false);
          streamingRef.current = null;
          break;
        case "done":
          // 不要覆盖已有的 error / paused 状态：
          // 若后端在流式过程中报错（如 No module named 'server'），error 事件已把
          // status 置为 error 并展示红字；done 事件若再覆盖成 done，错误会被抹掉，
          // 气泡变成「(无内容)」让问题看起来像静默失败。paused（审批挂起）同理需保留。
          updateMsg(msgId, (m) => ({ ...m, status: (m.status === "error" || m.status === "paused") ? m.status : "done" }));
          setStreaming(false);
          streamingRef.current = null;
          break;
      }
    },
    [updateMsg, flash, setSettings, settings]
  );

  // 逃生舱 slash 命令
  const handleSlash = useCallback(
    (text: string): boolean => {
      if (!text.startsWith("/")) return false;
      const [cmd, ...rest] = text.slice(1).trim().split(/\s+/);
      const arg = rest.join(" ");
      switch (cmd.toLowerCase()) {
        case "mode": {
          const target = (arg || "").toLowerCase();
          if (["chat", "coding", "mission"].includes(target)) {
            setMode(target);
            flash(`已切换到「${target}」模式`);
          } else {
            flash("用法：/mode chat|coding|mission");
          }
          return true;
        }
        case "context":
        case "ctx":
          setCtxOpen(true);
          loadContext();
          flash("已打开上下文面板");
          return true;
        case "metrics":
          apiFetch("/metrics").then((r) => r.json()).then(setMetrics).catch(() => {});
          flash("已刷新性能指标");
          return true;
        case "new":
          newSession();
          flash("已新建会话");
          return true;
        case "clear": {
          // 真清空：后端清持久上下文（POST /context/clear）+ 前端清可见会话
          const sid = activeIdRef.current;
          if (sid) {
            apiFetch("/context/clear", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ thread_id: sid }),
            })
              .then(() => flash("已清空本会话上下文"))
              .catch(() => flash("清空失败（后端不可用）"));
            setSessions((prev) =>
              prev.map((s) => (s.id === sid ? { ...s, messages: [], title: "新对话" } : s))
            );
          } else {
            flash("没有可清空的会话");
          }
          return true;
        }
        case "reject":
        case "approve":
          if (pendingApprovalRef.current) {
            resumeRef.current?.(cmd.toLowerCase() === "reject" ? "rejected" : "approved");
          } else {
            flash("当前没有待裁决的审批");
          }
          return true;
        case "help":
          // 交给后端返回完整帮助（AI 消息形式展示）
          return false;
        default:
          // 其余（/skills /compact /skill /<id> /未知命令）→ 交给后端 SSE 处理
          return false;
      }
    },
    [flash, loadContext, newSession, activeIdRef]
  );

  const sendMessage = useCallback(
    async (text: string) => {
      const sid = activeIdRef.current;
      if (!sid || streaming) {
        if (streaming) flash("当前回复进行中，请稍候再发送");
        return;
      }
      if (handleSlash(text)) return;
      const userMsg: Msg = { id: uid(), role: "user", content: text, toolCalls: [], status: "done" };
      const aiMsg: Msg = { id: uid(), role: "ai", content: "", reasoning: [], toolCalls: [], status: "streaming" };
      setSessions((prev) =>
        prev.map((s) =>
          s.id === sid
            ? {
                ...s,
                title: s.messages.length ? s.title : text.slice(0, 24),
                messages: [...s.messages, userMsg, aiMsg],
                updatedAt: Date.now(),
              }
            : s
        )
      );
      streamingRef.current = aiMsg.id;
      pendingApprovalRef.current = null;
      setStreaming(true);

      const body = {
        thread_id: sid,
        message: text,
        mode: modeRef.current,
        model: settings.model,
        reasoning: settings.reasoning,
        api_key: settings.apiKey,
        base_url: settings.baseUrl,
        provider: settings.provider,
      };
      try {
        await streamChat(`/api/chat/${sid}`, body, (ev) => handleEvent(ev, aiMsg.id));
      } catch (err: any) {
        updateMsg(aiMsg.id, (m) => ({ ...m, status: "error", error: err?.message || "发送失败" }));
      } finally {
        // 兜底：确保任何情况下输入框都能解锁
        streamingRef.current = null;
        setStreaming(false);
      }
    },
    [streaming, settings, handleEvent, handleSlash, flash]
  );

  const resume = useCallback(
    async (decision: string) => {
      const sid = activeIdRef.current;
      const msgId = pendingApprovalRef.current;
      const reqId = approvalRequestIdRef.current;
      if (!sid || !msgId || !reqId) return;
      // 对齐 QwenPaw「阻塞点原地 Future 超时」：审批在原 SSE 流内原地解除，
      // 不再另开一条 stream（旧实现会重消耗 token / 可能触发新工具）。
      // 这里只把决议 POST 给后端 hub.resolve(request_id, decision)，
      // 后端 set 该 future，原图**同一执行流**继续，SSE 流继续吐 token/done。
      pendingApprovalRef.current = null;
      approvalRequestIdRef.current = null;
      updateMsg(msgId, (m) => ({ ...m, approval: undefined, status: "streaming" }));
      setStreaming(true);
      try {
        const res = await apiFetch(`/api/approval/${sid}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request_id: reqId, decision }),
        });
        if (!res.ok) {
          const txt = await res.text().catch(() => res.statusText);
          throw new Error(txt || res.statusText);
        }
      } catch (err: any) {
        updateMsg(msgId, (m) => ({ ...m, status: "error", error: err?.message || "审批决议失败" }));
        setStreaming(false);
        streamingRef.current = null;
      }
      // 注意：成功时**不**在此 setStreaming(false) —— 原 SSE 流仍打开，
      // 会在后端图继续跑完后的 done 事件里自动收尾。
    },
    [updateMsg]
  );
  resumeRef.current = resume;

  // ---------------------------------------------------------------------------
  // 检查点时间轴：拉取 / 回滚 / 分叉 / 重放
  // ---------------------------------------------------------------------------
  const loadHistory = useCallback(async () => {
    const sid = activeIdRef.current;
    if (!sid) return;
    setHistLoading(true);
    setHistError(null);
    try {
      const r = await apiFetch(`/threads/${encodeURIComponent(sid)}/history`);
      if (!r.ok) throw new Error((await r.text().catch(() => r.statusText)) || r.statusText);
      const d = await r.json().catch(() => ({ steps: [] }));
      setHistSteps(d.steps || []);
    } catch (e: any) {
      setHistError(e?.message || "加载失败");
      setHistSteps([]);
    } finally {
      setHistLoading(false);
    }
  }, []);

  // 软回滚：移动后端指针到指定检查点（不丢历史），再把后端状态同步回前端本地消息
  const doRollback = useCallback(
    async (cid: string) => {
      const sid = activeIdRef.current;
      if (!sid || streamingRef.current) return;
      setRollingBack(true);
      try {
        const r = await apiFetch(`/threads/${encodeURIComponent(sid)}/rollback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ checkpoint_id: cid }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) throw new Error(d.error || r.statusText);
        const st = await apiFetch(`/threads/${encodeURIComponent(sid)}/state`);
        const sd = await st.json().catch(() => ({ values: { messages: [] } }));
        const msgs = backendMessagesToMsgs(sd.values?.messages || []);
        setSessions((prev) =>
          prev.map((s) => (s.id === sid ? { ...s, messages: msgs, updatedAt: Date.now() } : s))
        );
        flash("已回滚到该检查点 · 已写入的外部文件不会自动撤销");
      } catch (e: any) {
        flash("回滚失败：" + (e?.message || "未知错误"));
      } finally {
        setRollingBack(false);
        // 回滚后刷新时间轴（指针已移动）
        loadHistory();
      }
    },
    [flash, loadHistory]
  );

  // 分叉：从指定检查点读出完整状态，写入新 thread，并切换到新会话
  const doFork = useCallback(
    async (cid: string) => {
      const sid = activeIdRef.current;
      if (!sid) return;
      try {
        const r = await apiFetch(`/threads/${encodeURIComponent(sid)}/fork`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ checkpoint_id: cid }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.ok) throw new Error(d.error || r.statusText);
        const newTid = d.thread_id;
        // 注册新 thread 到后端 sessions 索引（归属当前用户），否则前端切不进去
        await apiFetch("/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ thread_id: newTid }),
        }).catch(() => {});
        const st = await apiFetch(`/threads/${encodeURIComponent(newTid)}/state`);
        const sd = await st.json().catch(() => ({ values: { messages: [] } }));
        const msgs = backendMessagesToMsgs(sd.values?.messages || []);
        setSessions((prev) => [
          {
            id: newTid,
            title: "分叉自 " + sid.slice(0, 8),
            messages: msgs,
            updatedAt: Date.now(),
            createdAt: Date.now(),
          },
          ...prev.filter((x) => x.id !== newTid),
        ]);
        setActiveId(newTid);
        setHistOpen(false);
        flash("已分叉为新会话，可继续聊");
      } catch (e: any) {
        flash("分叉失败：" + (e?.message || "未知错误"));
      }
    },
    []
  );

  // 重放：取该检查点的状态，用当时的最后一条用户输入重新跑一遍图
  const doReplay = useCallback(
    async (cid: string) => {
      const sid = activeIdRef.current;
      if (!sid || streamingRef.current) return;
      try {
        const st = await apiFetch(
          `/threads/${encodeURIComponent(sid)}/state?checkpoint_id=${encodeURIComponent(cid)}`
        );
        const sd = await st.json().catch(() => ({ values: { messages: [] } }));
        const msgs = sd.values?.messages || [];
        let lastHuman = "";
        for (const m of msgs) {
          if (m?.type === "human" || m?.type === "user") lastHuman = textOf(m.content);
        }
        if (!lastHuman) {
          flash("该检查点没有可重放的用户输入");
          return;
        }
        // 先把本地消息重建为该检查点状态（同回滚），再追加重放的输入与回复
        const rebuilt = backendMessagesToMsgs(msgs);
        const aiMsg: Msg = { id: uid(), role: "ai", content: "", reasoning: [], toolCalls: [], status: "streaming" };
        const userMsg: Msg = { id: uid(), role: "user", content: lastHuman, toolCalls: [], status: "done" };
        setSessions((prev) =>
          prev.map((s) =>
            s.id === sid
              ? { ...s, messages: [...rebuilt, userMsg, aiMsg], updatedAt: Date.now() }
              : s
          )
        );
        streamingRef.current = aiMsg.id;
        setStreaming(true);
        await streamChat(
          `/threads/${encodeURIComponent(sid)}/replay`,
          {
            checkpoint_id: cid,
            input: { messages: [{ role: "user", content: lastHuman }] },
            model: settings.model,
            reasoning: settings.reasoning,
            api_key: settings.apiKey,
            base_url: settings.baseUrl,
            provider: settings.provider,
            mode: modeRef.current,
          },
          (ev) => handleEvent(ev, aiMsg.id)
        );
      } catch (e: any) {
        flash("重放失败：" + (e?.message || "未知错误"));
        setStreaming(false);
        streamingRef.current = null;
      } finally {
        streamingRef.current = null;
        setStreaming(false);
      }
    },
    [flash, handleEvent, settings]
  );

  // 打开时间轴面板时自动拉取检查点列表
  useEffect(() => {
    if (histOpen) loadHistory();
  }, [histOpen, activeId, loadHistory]);

  // 设置面板逻辑
  const set = (k: string, v: any) => setSettings((f) => ({ ...f, [k]: v }));
  const setRuntime = <G extends keyof RuntimeConfig, K extends keyof RuntimeConfig[G]>(
    group: G,
    key: K,
    value: RuntimeConfig[G][K]
  ) => setRuntimeConfig((c) => ({ ...c, [group]: { ...(c[group] as any), [key]: value } }));

  // 写入 running 下的嵌套字段（如 loop.iteration.max_iterations）
  const setRunning = (path: string[], value: any) =>
    setRuntimeConfig((c) => {
      const running: any = { ...(c.running as any) };
      let node: any = running;
      for (let i = 0; i < path.length - 1; i++) {
        node[path[i]] = { ...(node[path[i]] || {}) };
        node = node[path[i]];
      }
      node[path[path.length - 1]] = value;
      return { ...c, running };
    });

  // 写入顶层标量字段（language / approval_level 等）
  const setScalar = (key: string, value: any) =>
    setRuntimeConfig((c) => ({ ...c, [key]: value }));

  useEffect(() => {
    if (!settingsOpen && view !== "config") return;
    apiFetch("/providers")
      .then((r) => r.json())
      .then((d: any) => setProviders(d.providers || []))
      .catch(() => {});
    setDiscoverState("idle");
    setDiscoveredModels([]);
    setReasoningModels([]);
    setDiscoverMsg(null);
    setKeyValid(null);
  }, [settingsOpen, view]);

  const onProviderChange = (pid: string) => {
    const p = providers.find((x) => x.id === pid);
    set("provider", pid);
    set("baseUrl", p?.base_url || settings.baseUrl);
    if (p?.models?.length) {
      setDiscoveredModels(p.models);
      setDiscoverState("idle");
      setDiscoverMsg(null);
    }
  };

  const discover = async () => {
    setDiscoverState("loading");
    setDiscoverMsg(null);
    setKeyValid(null);
    try {
      const r = await apiFetch("/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: settings.baseUrl.trim(), api_key: settings.apiKey.trim() }),
      });
      const d = await r.json();
      if (!r.ok || d.ok === false) {
        setDiscoverState("error");
        setDiscoverMsg("发现失败：" + (d.error || r.statusText));
        return;
      }
      setKeyValid(!!d.key_valid);
      const models = d.models || [];
      setDiscoveredModels(models);
      setReasoningModels(d.reasoning_models || []);
      setDiscoverState("done");
      setDiscoverMsg(
        d.discovered
          ? `✓ 密钥有效，发现 ${models.length} 个可用模型` +
            (d.reasoning_models?.length ? `（其中 ${d.reasoning_models.length} 个支持思考）` : "")
          : `✓ 密钥有效；未开放模型列举，已显示常用模型（${models.length} 个）`
      );
      if (d.key_valid && models.length && !models.includes(settings.model)) {
        set("model", models[0]);
      }
    } catch (e: any) {
      setDiscoverState("error");
      setDiscoverMsg("发现出错：" + String(e?.message || e));
    }
  };

  const toggleReasoning = (checked: boolean) => {
    if (!checked) {
      set("reasoning", false);
      return;
    }
    const autoModel = !reasoningModels.includes(settings.model) && reasoningModels[0] ? reasoningModels[0] : settings.model;
    set("reasoning", true);
    set("model", autoModel);
  };

  const saveSettings = async () => {
    setSettingsMsg(null);
    const body: any = {
      llm: {
        provider: settings.provider,
        base_url: settings.baseUrl.trim(),
        model: settings.model.trim() || "deepseek-chat",
        reasoning: !!settings.reasoning,
      },
      security: { enable_auth: settings.enableAuth },
      // 对齐 QwenPaw 真实 agent-config 结构
      language: runtimeConfig.language,
      approval_level: settings.trustMode ? "OFF" : runtimeConfig.approval_level,
      plan: runtimeConfig.plan,
      rate_limiter: runtimeConfig.rate_limiter,
      running: runtimeConfig.running,
      rate_limit: runtimeConfig.rate_limiter.enabled ? runtimeConfig.rate_limiter.requests_per_minute : 0,
    };
    if (settings.apiKey.trim()) body.llm.api_key = settings.apiKey.trim();
    if (settings.enableAuth) {
      if (!settings.serviceKey.trim()) {
        setSettingsMsg("启用访问鉴权必须设置服务令牌");
        return;
      }
      body.service = { api_key: settings.serviceKey.trim() };
    }
    try {
      const r = await apiFetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({ detail: r.statusText }));
        setSettingsMsg("保存失败：" + (e.detail || r.status));
        return;
      }
      if (settings.enableAuth) {
        localStorage.setItem("svc_token", settings.serviceKey.trim());
        setAuthEnabled(true);
      } else {
        localStorage.removeItem("svc_token");
        setAuthEnabled(false);
      }
      setSettingsMsg("已保存，即时生效。" + (settings.reasoning ? " 已开启思考模式。" : ""));
    } catch (e: any) {
      setSettingsMsg("保存出错：" + String(e?.message || e));
    }
  };

  // 进入会话时切回聊天视图
  const enterSession = (id: string) => {
    setActiveId(id);
    setView("chat");
  };

  // 侧边栏导航分组
  const NavItem = ({ id, label, active }: { id: string; label: string; active?: boolean }) => (
    <div className={"nav-item " + (active ? "active" : "")} onClick={() => setView(id)}>
      <span className="nav-icon">{ICONS[id]}</span>
      <span className="nav-label">{label}</span>
    </div>
  );

  const NavGroup = ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div className="nav-group">
      <div className="nav-group-title">{title}</div>
      {children}
    </div>
  );

  return (
    <>
      {showLogin && (
        <LoginScreen loading={loginLoading} msg={loginMsg} onLogin={doLogin} />
      )}
      <div className="app">
      {/* ---------------- QwenPaw 风格全侧边栏 ---------------- */}
      <aside className="qwen-sidebar">
        <div className="qwen-brand">
          <div className="qwen-logo">{BRAND_LOGO}</div>
          <div>
            <div className="qwen-title">{BRAND_NAME}</div>
            <div className="qwen-version">{BRAND_VERSION}</div>
          </div>
        </div>

        {authEnabled && (
          <div className="user-chip">
            <span className="user-avatar">👤</span>
            <span className="user-name">{authUser}</span>
            <button className="user-logout" onClick={doLogout} title="退出登录">退出</button>
          </div>
        )}

        <div className="sidebar-top-sticky">
          <div className="agent-picker">
            <div className="agent-picker-label">Current Agent ({agents.length})</div>
            <select
              value={currentAgent.id}
              onChange={(e) => {
                const a = agents.find((x) => x.id === e.target.value) || agents[0];
                if (a) {
                  setCurrentAgent(a);
                  if (a.default_mode) setMode(a.default_mode);
                  if (a.default_model) set("model", a.default_model);
                }
              }}
            >
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.emoji || "🤖"} {a.name}
                </option>
              ))}
            </select>
          </div>

          <button
            className={"chat-button " + (view === "chat" ? "active" : "")}
            onClick={() => setView("chat")}
          >
            <span className="nav-icon"><ChatIcon /></span>
            <span>Chat</span>
            {inboxUnread > 0 && (
              <span className="nav-badge" title={`${inboxUnread} 条未读定时任务结果`}>
                {inboxUnread > 99 ? "99+" : inboxUnread}
              </span>
            )}
          </button>
        </div>

        <div className="qwen-nav">
          <NavGroup title="General">
            <NavItem id="channels" label="Channels" active={view === "channels"} />
            <NavItem id="sessions" label="Sessions" active={view === "sessions"} />
            <NavItem id="cron" label="Cron Jobs" active={view === "cron"} />
            <NavItem id="heartbeat" label="Heartbeat" active={view === "heartbeat"} />
          </NavGroup>

          <NavGroup title="Workspace">
            <NavItem id="files" label="Files" active={view === "files"} />
            <NavItem id="skills" label="Skills" active={view === "skills"} />
            <NavItem id="tools" label="Tools" active={view === "tools"} />
            <NavItem id="mcp" label="MCP" active={view === "mcp"} />
            <NavItem id="acp" label="ACP" active={view === "acp"} />
            <NavItem id="memory" label="Memory" active={view === "memory"} />
      <NavItem id="context" label="Context" active={view === "context"} />
          </NavGroup>

          <NavGroup title="Settings">
            <NavItem id="agents" label="Agent Management" active={view === "agents"} />
            <NavItem id="models" label="Models" active={view === "models"} />
            <NavItem id="skills" label="Skill Pool" active={view === "skills"} />
            <NavItem id="envs" label="Environments" active={view === "envs"} />
            <NavItem id="security" label="Security" active={view === "security"} />
            <NavItem id="tokenusage" label="Token Usage" active={view === "tokenusage"} />
            <NavItem id="backups" label="Backups" active={view === "backups"} />
            <NavItem id="voice" label="Voice Transcription" active={view === "voice"} />
            <NavItem id="debug" label="Debug" active={view === "debug"} />
            <NavItem id="config" label="Configuration" active={view === "config"} />
            <NavItem id="stats" label="Agent Statistics" active={view === "stats"} />
          </NavGroup>
        </div>

        <div className="mini-metrics">
          <div className="mini-metrics-title">Agent Status</div>
          <div className="mini-metric">
            <span>Mode</span>
            <b>{mode}</b>
          </div>
          <div className="mini-metric">
            <span>Model</span>
            <b>{settings.model || "demo"}</b>
          </div>
          <div className="mini-metric">
            <span>Turns</span>
            <b>{metrics?.turns ?? 0}</b>
          </div>
        </div>
      </aside>

      {/* ---------------- 主内容区 ---------------- */}
      <main className="main-area">
        {view === "chat" && (
          <ChatView
            activeSession={activeSession}
            streaming={streaming}
            settings={settings}
            discoveredModels={discoveredModels}
            mode={mode}
            modes={modes}
            ctxOpen={ctxOpen}
            setCtxOpen={setCtxOpen}
            setMode={setMode}
            set={set}
            toggleReasoning={toggleReasoning}
            newSession={newSession}
            setSettingsOpen={setSettingsOpen}
            resume={resume}
            sendMessage={sendMessage}
            loadContext={loadContext}
            ctxInfo={ctxInfo}
            ctxLoading={ctxLoading}
            metrics={metrics}
            inboxEvents={inboxEvents}
            inboxUnread={inboxUnread}
            homeSessionId={homeSessionId}
            onMarkInboxRead={markInboxRead}
            onMarkAllInboxRead={markAllInboxRead}
            onDismissInbox={dismissInbox}
            histOpen={histOpen}
            setHistOpen={setHistOpen}
            histSteps={histSteps}
            histLoading={histLoading}
            histError={histError}
            rollingBack={rollingBack}
            onLoadHistory={loadHistory}
            onRollback={doRollback}
            onFork={doFork}
            onReplay={doReplay}
          />
        )}
        {view === "channels" && <ChannelsPanel />}
        {view === "sessions" && <SessionsPanel onSelect={enterSession} onDelete={deleteSession} />}
        {view === "cron" && <CronPanel />}
        {view === "heartbeat" && <HeartbeatPanel />}
        {view === "files" && <FilesPanel />}
        {view === "skills" && <SkillsPanel />}
        {view === "tools" && <ToolsPanel />}
        {view === "mcp" && <McpPanel />}
        {view === "acp" && <AcpPanel />}
        {view === "memory" && <MemoryPanel />}
        {view === "context" && <ContextManagerPanel />}
        {view === "agents" && <AgentsPanel />}
        {view === "models" && <ModelsPanel />}
        {view === "envs" && <EnvsPanel />}
        {view === "security" && <SecurityPanel />}
        {view === "tokenusage" && <TokenUsagePanel />}
        {view === "backups" && <BackupsPanel />}
        {view === "voice" && <VoicePanel />}
        {view === "debug" && <DebugPanel />}
        {view === "stats" && <StatsPanel metrics={metrics} />}
        {view === "config" && (
          <ConfigPanel
            form={settings}
            setForm={set}
            runtimeConfig={runtimeConfig}
            setRuntime={setRuntime}
            setRunning={setRunning}
            setScalar={setScalar}
            providers={providers}
            discoveredModels={discoveredModels}
            reasoningModels={reasoningModels}
            discoverState={discoverState}
            discoverMsg={discoverMsg}
            keyValid={keyValid}
            onProviderChange={onProviderChange}
            onDiscover={discover}
            onToggleReasoning={toggleReasoning}
            msg={settingsMsg}
            onSave={saveSettings}
          />
        )}
      </main>

      {toast && <div className="toast">{toast}</div>}

      {settingsOpen && (
        <SettingsModal
          form={settings}
          setForm={set}
          providers={providers}
          discoveredModels={discoveredModels}
          reasoningModels={reasoningModels}
          discoverState={discoverState}
          discoverMsg={discoverMsg}
          keyValid={keyValid}
          onProviderChange={onProviderChange}
          onDiscover={discover}
          onToggleReasoning={toggleReasoning}
          msg={settingsMsg}
          onClose={() => setSettingsOpen(false)}
          onSave={saveSettings}
        />
      )}
      </div>
    </>
  );
}

// 聊天消息区自动滚动到底部（用户手动上滑时不打断）
function useAutoScroll<T extends HTMLElement>(deps: any[], live = false) {
  const ref = useRef<T | null>(null);
  const [isSticky, setIsSticky] = useState(true);
  const scroll = (smooth = false) => {
    const el = ref.current;
    if (!el) return;
    if (smooth) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    else el.scrollTop = el.scrollHeight;
  };
  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    const threshold = 80;
    const sticky = el.scrollHeight - el.scrollTop - el.clientHeight < threshold;
    setIsSticky(sticky);
  };
  // 新消息加入时平滑滚动到底
  useEffect(() => {
    if (isSticky) scroll(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  // 流式生成中实时跟随（用户手动上滑后不再打断）
  // 旧实现用 requestAnimationFrame 60fps 强制 scrollTop=scrollHeight，导致 UI 严重抖动。
  // 改用 MutationObserver：只在 DOM 真正变化（新 token/内容更新）时滚动一次。
  useEffect(() => {
    if (!live || !isSticky) return;
    const el = ref.current;
    if (!el) return;
    const mo = new MutationObserver(() => {
      el.scrollTop = el.scrollHeight;
    });
    mo.observe(el, { childList: true, subtree: true, characterData: true });
    return () => mo.disconnect();
  }, [live, isSticky]); // scroll/onScroll 稳定
  return { ref, onScroll, scroll, isSticky };
}

// ---------------------------------------------------------------------------
// 收件箱卡片：定时任务 / 后台任务结果，渲染进会话窗口
// （对齐 QwenPaw：cron 结果默认「弹出」在会话里，而不是只进独立管理面板）
// ---------------------------------------------------------------------------
function InboxCard({
  event,
  onMarkRead,
  onDismiss,
}: {
  event: any;
  onMarkRead: () => void;
  onDismiss: () => void;
}) {
  const isUnread = !event.read;
  const ok = event.status === "success";
  const ts = event.created_at ? new Date(event.created_at * 1000) : null;
  const timeStr = ts ? ts.toLocaleString() : "";
  const body = typeof event.body === "string" ? event.body : JSON.stringify(event.body || "");
  return (
    <div className={"inbox-card " + (ok ? "ok" : "err") + (isUnread ? " unread" : " read")}>
      <div className="inbox-card-head">
        <span className="inbox-card-icon">{ok ? "✅" : "❌"}</span>
        <span className="inbox-card-title">{event.title || "定时任务结果"}</span>
        {isUnread && <span className="inbox-card-unread">未读</span>}
        <span className="inbox-card-time">{timeStr}</span>
      </div>
      <div className="inbox-card-body">{body}</div>
      <div className="inbox-card-actions">
        {isUnread && (
          <button className="ghost small" onClick={onMarkRead}>
            标记已读
          </button>
        )}
        <button className="ghost small" onClick={onDismiss}>
          删除
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chat 视图（原主聊天区）
// ---------------------------------------------------------------------------
function ChatView({
  activeSession,
  streaming,
  settings,
  discoveredModels,
  mode,
  modes,
  ctxOpen,
  setCtxOpen,
  setMode,
  set,
  toggleReasoning,
  newSession,
  setSettingsOpen,
  resume,
  sendMessage,
  loadContext,
  ctxInfo,
  ctxLoading,
  metrics,
  inboxEvents,
  inboxUnread,
  homeSessionId,
  onMarkInboxRead,
  onMarkAllInboxRead,
  onDismissInbox,
  // checkpoint 时间轴
  histOpen,
  setHistOpen,
  histSteps,
  histLoading,
  histError,
  rollingBack,
  onLoadHistory,
  onRollback,
  onFork,
  onReplay,
}: any) {
  const { ref: scrollRef, onScroll, scroll, isSticky } = useAutoScroll<HTMLDivElement>([activeSession?.messages.length], streaming);
  const showInbox = activeSession?.id === homeSessionId;
  return (
    <div className="chat">
      <header className="chat-header">
        <div className="chat-model">
          <select value={settings.model} onChange={(e: any) => set("model", e.target.value)} title="当前模型（每次请求生效）">
            {!discoveredModels.includes(settings.model) && <option value={settings.model}>{settings.model || "（未选模型）"}</option>}
            {discoveredModels.map((m: string) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </div>
        <div className="mode-select" title="AgentMode：切换 Loop Gates 束 + 系统提示 + 工具面">
          <select value={mode} onChange={(e: any) => setMode(e.target.value)}>
            {(modes.length ? modes : [{ name: "chat", description: "" }]).map((mo: any) => (
              <option key={mo.name} value={mo.name} title={mo.description}>
                {mo.name === "chat" ? "💬 对话" : mo.name === "coding" ? "🛠 编码" : mo.name === "mission" ? "🎯 任务" : mo.name}
              </option>
            ))}
          </select>
        </div>
        <label className="reason-toggle">
          <input type="checkbox" checked={settings.reasoning} onChange={(e: any) => toggleReasoning(e.target.checked)} />
          <span>思考模式</span>
        </label>
        <button className="newchat small" onClick={newSession}>＋ 新对话</button>
        <button className="gear small" onClick={() => setSettingsOpen(true)} title="设置">⚙</button>
        <button className={"ctx-btn" + (ctxOpen ? " active" : "")} onClick={() => setCtxOpen((v: boolean) => !v)} title="上下文检视">
          🧠 上下文
        </button>
        <button className={"ctx-btn" + (histOpen ? " active" : "")} onClick={() => setHistOpen((v: boolean) => !v)} title="检查点时间轴 · 回滚 / 分叉 / 重放">
          🕒 历史
        </button>
        {streaming && <span className="live-dot" title="生成中" />}
        {inboxUnread > 0 && (
          <button className="inbox-markall ghost small" onClick={onMarkAllInboxRead} title="标记全部定时任务结果为已读">
            ✓ 全部已读 ({inboxUnread})
          </button>
        )}
      </header>

      <div className="messages" id="msg-scroll" ref={scrollRef} onScroll={onScroll}>
        {(!activeSession || activeSession.messages.length === 0) && !streaming && !(showInbox && inboxEvents.length > 0) && (
          <div className="empty">
            <div className="empty-logo">{BRAND_LOGO}</div>
            <p>发送一条消息开始。试试「桌面上有什么」体验桌面截图 + 看图流程。</p>
          </div>
        )}
        {activeSession?.messages.map((m: Msg) => <MessageBubble key={m.id} m={m} onResume={resume} />)}
        {showInbox && inboxEvents.length > 0 && (
          <div className="inbox-stack">
            <div className="inbox-stack-head">📨 定时任务结果</div>
            {inboxEvents.map((ev: any) => (
              <InboxCard
                key={ev.id}
                event={ev}
                onMarkRead={() => onMarkInboxRead([ev.id])}
                onDismiss={() => onDismissInbox(ev.id)}
              />
            ))}
          </div>
        )}
        {!isSticky && (
          <button className="scroll-to-bottom" onClick={() => scroll(true)} title="回到底部">
            ↓ 回到底部
          </button>
        )}
      </div>

      <Composer streaming={streaming || rollingBack} onSend={sendMessage} />

      {ctxOpen && <ContextPanel info={ctxInfo} loading={ctxLoading} onRefresh={loadContext} onClose={() => setCtxOpen(false)} metrics={metrics} />}
      <HistoryPanel
        open={histOpen}
        onClose={() => setHistOpen(false)}
        steps={histSteps}
        loading={histLoading}
        error={histError}
        rollingBack={rollingBack}
        onRefresh={onLoadHistory}
        onRollback={onRollback}
        onFork={onFork}
        onReplay={onReplay}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 上下文检视面板
// ---------------------------------------------------------------------------
function ContextPanel({
  info,
  loading,
  onRefresh,
  onClose,
  metrics,
}: {
  info: any;
  loading: boolean;
  onRefresh: () => void;
  onClose: () => void;
  metrics: any;
}) {
  const foldedSet = new Set<number>((info?.folded_seqs || []) as number[]);
  return (
    <div className="ctx-drawer">
      <div className="ctx-head">
        <div className="ctx-title">🧠 上下文管理</div>
        <div className="ctx-head-actions">
          <button className="ghost small" onClick={onRefresh} title="刷新">↻</button>
          <button className="ghost small" onClick={onClose} title="关闭">✕</button>
        </div>
      </div>
      {loading && <div className="muted ctx-pad">加载中…</div>}
      {!loading && info && (
        <>
          <div className="ctx-stats">
            <div className="ctx-stat"><span>总轮次</span><b>{info.total_turns}</b></div>
            <div className="ctx-stat"><span>窗口</span><b>{info.window_size}</b></div>
            <div className="ctx-stat"><span>已折叠</span><b>{(info.folded_seqs || []).length}</b></div>
            <div className="ctx-stat"><span>预算 tok</span><b>{info.budget_tokens}</b></div>
          </div>
          <div className="ctx-flags">
            <span className={"flag " + (info.recall_enabled ? "on" : "off")}>召回 {info.recall_enabled ? "启用" : "关闭"}</span>
            {metrics && <span className="flag on">折叠 tok {metrics.folded_tokens || 0}</span>}
          </div>
          <div className="ctx-note">折叠 = 零丢失：折叠区全文已写穿到 store，模型可用 <code>recall("关键词")</code> 还原。</div>
          <div className="ctx-turns">
            {(info.turns || []).map((t: any) => (
              <div key={t.seq} className={"ctx-turn " + t.type + (foldedSet.has(t.seq) ? " folded" : "")}>
                <div className="ctx-turn-head">
                  <span className="ctx-seq">#{t.seq}</span>
                  <span className={"ctx-role " + t.type}>{t.type}</span>
                  <span className="ctx-chars">{t.chars} 字</span>
                  {foldedSet.has(t.seq) && <span className="ctx-folded-tag">已折叠</span>}
                </div>
                <div className="ctx-preview">{t.preview || "（空）"}</div>
              </div>
            ))}
          </div>
        </>
      )}
      {!loading && !info && <div className="muted ctx-pad">暂无上下文数据。</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 检查点时间轴面板：列出 checkpoint 步骤，支持回滚 / 分叉 / 重放
// ---------------------------------------------------------------------------
function HistoryPanel({
  open,
  onClose,
  steps,
  loading,
  error,
  rollingBack,
  onRefresh,
  onRollback,
  onFork,
  onReplay,
}: any) {
  if (!open) return null;
  return (
    <div className="ctx-drawer hist-drawer">
      <div className="ctx-head">
        <div className="ctx-title">🕒 检查点时间轴</div>
        <div className="ctx-head-actions">
          <button className="ghost small" onClick={onRefresh} title="刷新">↻</button>
          <button className="ghost small" onClick={onClose} title="关闭">✕</button>
        </div>
      </div>
      <div className="hist-note">回滚只还原对话状态，已写入的外部文件 / 记忆不会自动撤销。</div>
      {loading && <div className="muted ctx-pad">加载中…</div>}
      {!loading && error && <div className="hist-error ctx-pad">⚠️ {error}</div>}
      {!loading && !error && steps.length === 0 && <div className="muted ctx-pad">暂无检查点（先发几条消息）。</div>}
      <div className="hist-list">
        {(steps || []).map((s: any, i: number) => (
          <div key={s.checkpoint_id} className="hist-step">
            <div className="hist-step-head">
              <span className="hist-step-idx">#{i + 1}</span>
              <span className={"hist-step-role " + (s.last_role || "")}>{s.last_role || "—"}</span>
              <span className="hist-step-msg">{s.n_messages} 条</span>
              {s.created_at ? (
                <span className="hist-step-time">{new Date(s.created_at * 1000).toLocaleTimeString()}</span>
              ) : null}
            </div>
            <div className="hist-step-preview">{s.preview || "（空）"}</div>
            <div className="hist-step-actions">
              <button
                className="hist-btn rollback"
                disabled={rollingBack}
                onClick={() => onRollback(s.checkpoint_id)}
                title="软回滚到这一步（移动指针，不丢历史）"
              >
                ↺ 回到这里
              </button>
              <button
                className="hist-btn fork"
                disabled={rollingBack}
                onClick={() => onFork(s.checkpoint_id)}
                title="从该检查点分叉出新会话"
              >
                ⑂ 分叉
              </button>
              <button
                className="hist-btn replay"
                disabled={rollingBack}
                onClick={() => onReplay(s.checkpoint_id)}
                title="用当时的输入从该检查点重放一次"
              >
                ▶ 重放
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 工具结果渲染（支持图片/视频/JSON/文本）
// ---------------------------------------------------------------------------
function ToolResult({ result }: { result: string }) {
  // 尝试解析 JSON，如果失败则按文本展示
  let parsed: any = null;
  try {
    parsed = JSON.parse(result);
  } catch {
    parsed = null;
  }
  if (parsed && parsed.image_url) {
    return (
      <div className="tool-media">
        <img src={API_URL + parsed.image_url} alt={parsed.message || "image"} loading="lazy" />
        {parsed.message && <div className="tool-media-caption">{parsed.message}</div>}
      </div>
    );
  }
  if (parsed && parsed.video_url) {
    return (
      <div className="tool-media">
        <video src={API_URL + parsed.video_url} controls preload="metadata" />
        {parsed.message && <div className="tool-media-caption">{parsed.message}</div>}
      </div>
    );
  }
  return <pre className="tool-result">{String(result)}</pre>;
}

// ---------------------------------------------------------------------------
// 消息气泡
// ---------------------------------------------------------------------------
function ReasoningPhase({
  content,
  phase,
  isStreaming,
  isActive,
}: {
  content: string;
  phase: number;
  isStreaming: boolean;
  isActive: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="reasoning-block">
      <button className="reasoning-toggle" onClick={() => setOpen((s) => !s)}>
        {open ? "🔽 收起思考" : "🔍 思考过程"}
      </button>
      {!open && isStreaming && isActive && (
        <span className="reasoning-pulse reasoning-preview">思考中…</span>
      )}
      {open && (
        <div className="reasoning-content">
          {content}
        </div>
      )}
    </div>
  );
}

// memo 化：SSE token 更新只重渲染正在变化的消息，不重渲染整个消息列表。
const MessageBubble = memo(function MessageBubble({ m, onResume }: { m: Msg; onResume: (d: string) => void }) {
  const isStreaming = m.status === "streaming";
  const isPaused = m.status === "paused";
  const reasoning = m.reasoning || [];
  const toolCalls = m.toolCalls || [];
  // 正在接收思考增量的步骤：流式且最后一条思考非空时，认为该步骤仍在生成
  const activeReasoningIndex = isStreaming
    ? reasoning.length - 1
    : -1;

  if (m.role === "user") {
    return (
      <div className="row right">
        <div className="bubble human">{m.content}</div>
      </div>
    );
  }

  return (
    <div className="row left">
      <div className="bubble ai">
        {/* 把每个思考步骤和它对应的工具调用交错展示，避免所有思考连成一段。 */}
        {toolCalls.map((tc, i) => (
          <div key={tc.id || i} className="step-group">
            {reasoning[i] && (
              <ReasoningPhase
                content={reasoning[i]}
                phase={i}
                isStreaming={isStreaming}
                isActive={i === activeReasoningIndex}
              />
            )}
            <div className={"tool-card " + tc.status}>
              <div className="tool-head">
                <span className="tool-icon">🔧</span>
                <b>{tc.name || "tool"}</b>
                <span className={"tool-status " + tc.status}>{tc.status === "start" ? "调用中" : "完成"}</span>
              </div>
              {tc.args != null && (
                <pre className="tool-args">{typeof tc.args === "string" ? tc.args : JSON.stringify(tc.args, null, 2)}</pre>
              )}
              {tc.status === "result" && tc.result != null && <ToolResult result={String(tc.result)} />}
            </div>
          </div>
        ))}

        {/* 最后一段思考（通常位于最终答案之前） */}
        {reasoning[toolCalls.length] && (
          <ReasoningPhase
            content={reasoning[toolCalls.length]}
            phase={toolCalls.length}
            isStreaming={isStreaming}
            isActive={toolCalls.length === activeReasoningIndex}
          />
        )}

        {m.content ? (
          <div className="answer">{m.content}</div>
        ) : (
          !reasoning.length && (isStreaming ? <span className="typing">思考中…</span> : <span className="muted">（无内容）</span>)
        )}

        {m.status === "error" && <div className="msg-error">⚠️ {m.error}</div>}

        {m.statusNote && isStreaming && !m.content && (
          <div className="status-note">⏳ {m.statusNote}</div>
        )}

        {isPaused && m.approval && (
          <div className="approval-card">
            <div className="approval-q">🛑 需要人工裁决：{String(m.approval.question || "")}</div>
            <div className="approval-actions">
              <button className="approve" onClick={() => onResume("approved")}>✓ 批准并继续</button>
              <button className="reject" onClick={() => onResume("rejected")}>✕ 拒绝</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
});

// ---------------------------------------------------------------------------
// 输入区
// ---------------------------------------------------------------------------
function Composer({ streaming, onSend }: { streaming: boolean; onSend: (t: string) => void }) {
  const [input, setInput] = useState("");
  const [showSuggest, setShowSuggest] = useState(false);
  const [suggestIdx, setSuggestIdx] = useState(0);
  const [suggestions, setSuggestions] = useState<{ cmd: string; desc: string }[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const skillsCache = useRef<string[] | null>(null);

  const SLASH_COMMANDS: { cmd: string; desc: string }[] = [
    { cmd: "/skills", desc: "列出可用技能命令" },
    { cmd: "/skill", desc: "<id> <任务> 调用技能" },
    { cmd: "/clear", desc: "清空本会话上下文" },
    { cmd: "/compact", desc: "压缩会话为摘要" },
    { cmd: "/help", desc: "显示帮助" },
    { cmd: "/mode", desc: "chat|coding|mission 切换模式" },
    { cmd: "/context", desc: "打开上下文面板" },
    { cmd: "/metrics", desc: "刷新性能指标" },
    { cmd: "/approve", desc: "通过当前审批" },
    { cmd: "/reject", desc: "拒绝当前审批" },
    { cmd: "/new", desc: "新建会话" },
  ];

  // 懒加载一次技能列表，用于补全 /<id> 技能命令
  const loadSkills = async () => {
    if (skillsCache.current) return;
    skillsCache.current = [];
    try {
      const r = await apiFetch("/skills");
      const data = await r.json();
      if (data && Array.isArray(data.skills)) skillsCache.current = data.skills.map((s: any) => s.id).filter(Boolean);
    } catch {
      skillsCache.current = [];
    }
  };

  const updateSuggestions = (val: string) => {
    if (!val.startsWith("/") || val.includes(" ")) {
      setShowSuggest(false);
      return;
    }
    const q = val.toLowerCase();
    const builtins = SLASH_COMMANDS.filter((c) => c.cmd.startsWith(q));
    let list = builtins;
    // 仅当用户输入了多于一个字符（/x）时才混入技能命令，避免一上来列 40+ 条
    if (q.length > 1 && skillsCache.current) {
      const skillHits = skillsCache.current
        .filter((id) => ("/" + id).startsWith(q))
        .map((id) => ({ cmd: "/" + id, desc: "技能命令" }));
      list = [...builtins, ...skillHits];
    }
    setSuggestions(list);
    setShowSuggest(list.length > 0);
    setSuggestIdx(0);
  };

  const submit = (override?: string) => {
    const t = (override ?? input).trim();
    if (!t) return;
    setShowSuggest(false);
    if (streaming) {
      // 让 sendMessage 统一提示，输入框保留文字
      onSend(t);
      return;
    }
    setInput("");
    onSend(t);
  };

  const acceptSuggestion = (s: string) => {
    setInput(s + " ");
    setShowSuggest(false);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (showSuggest && suggestions.length) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSuggestIdx((i) => (i + 1) % suggestions.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSuggestIdx((i) => (i - 1 + suggestions.length) % suggestions.length);
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        acceptSuggestion(suggestions[suggestIdx].cmd);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const exact = suggestions.length === 1 && suggestions[0].cmd === input.trim();
        if (exact) submit();
        else acceptSuggestion(suggestions[suggestIdx].cmd);
        return;
      }
      if (e.key === "Escape") {
        setShowSuggest(false);
        return;
      }
    }
    if (e.key === "Enter") submit();
  };

  const onChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value;
    setInput(v);
    if (v.startsWith("/")) loadSkills();
    updateSuggestions(v);
  };

  return (
    <div className="composer">
      {showSuggest && (
        <ul className="slash-suggest">
          {suggestions.map((s, i) => (
            <li
              key={s.cmd}
              className={i === suggestIdx ? "active" : ""}
              onMouseDown={(e) => {
                e.preventDefault();
                acceptSuggestion(s.cmd);
              }}
              onMouseEnter={() => setSuggestIdx(i)}
            >
              <span className="sc-cmd">{s.cmd}</span>
              <span className="sc-desc">{s.desc}</span>
            </li>
          ))}
        </ul>
      )}
      <input
        ref={inputRef}
        value={input}
        placeholder={streaming ? "AI 回复中，可继续输入下一条…" : "输入消息，回车发送…（输入 / 查看命令）"}
        onChange={onChange}
        onKeyDown={onKeyDown}
      />
      <button onClick={() => submit()} disabled={streaming || !input.trim()}>发送</button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 登录页（服务级鉴权开启时展示）
// ---------------------------------------------------------------------------
function LoginScreen({
  loading,
  msg,
  onLogin,
}: {
  loading: boolean;
  msg: string | null;
  onLogin: (username: string, password: string) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const submit = () => {
    if (!username.trim() || !password) return;
    onLogin(username.trim(), password);
  };
  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-logo">{BRAND_LOGO}</div>
        <div className="login-title">{BRAND_NAME}</div>
        <div className="login-sub">请登录以继续（多用户隔离）</div>
        {msg && <div className="login-msg">{msg}</div>}
        <label className="login-field">
          <span>用户名</span>
          <input
            value={username}
            autoFocus
            onChange={(e) => setUsername(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="用户名"
          />
        </label>
        <label className="login-field">
          <span>密码</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="密码"
          />
        </label>
        <button className="login-submit" onClick={submit} disabled={loading || !username.trim() || !password}>
          {loading ? "登录中…" : "登录"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 设置弹窗
// ---------------------------------------------------------------------------
function SettingsModal({
  form,
  setForm,
  providers,
  discoveredModels,
  reasoningModels,
  discoverState,
  discoverMsg,
  keyValid,
  onProviderChange,
  onDiscover,
  onToggleReasoning,
  msg,
  onClose,
  onSave,
}: {
  form: Settings;
  setForm: (k: string, v: any) => void;
  providers: any[];
  discoveredModels: string[];
  reasoningModels: string[];
  discoverState: "idle" | "loading" | "done" | "error";
  discoverMsg: string | null;
  keyValid: boolean | null;
  onProviderChange: (pid: string) => void;
  onDiscover: () => void;
  onToggleReasoning: (checked: boolean) => void;
  msg: string | null;
  onClose: () => void;
  onSave: () => void;
}) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">⚙️ 配置 API 与安全</div>
        <p className="modal-hint">选厂商 → 填 Key → 发现模型 → 保存。保存后即时生效。</p>
        {msg && <div className="modal-msg">{msg}</div>}

        <label className="field">
          <span>模型厂商</span>
          <select value={form.provider} onChange={(e) => onProviderChange(e.target.value)}>
            <option value="">— 自定义 / 手动填 Base URL —</option>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>{p.label}{p.reasoning ? " · 支持思考" : ""}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>LLM API Key</span>
          <input type="password" value={form.apiKey} placeholder="留空则保留当前值 / 用演示模型" onChange={(e) => setForm("apiKey", e.target.value)} />
        </label>
        <label className="field">
          <span>Base URL</span>
          <input value={form.baseUrl} placeholder="如 https://api.deepseek.com/v1" onChange={(e) => setForm("baseUrl", e.target.value)} />
        </label>

        <div className="discover-row">
          <button className="discover" onClick={onDiscover} disabled={discoverState === "loading" || !form.apiKey.trim()}>
            {discoverState === "loading" ? "发现中…" : "🔍 发现模型"}
          </button>
          {keyValid === true && <span className="badge-ok">✓ 密钥有效</span>}
          {keyValid === false && <span className="badge-bad">✗ 密钥无效</span>}
        </div>
        {discoverMsg && <div className={`discover-msg ${keyValid ? "ok" : "bad"}`}>{discoverMsg}</div>}

        <label className="field">
          <span>模型</span>
          <select value={form.model} onChange={(e) => setForm("model", e.target.value)}>
            {discoveredModels.length === 0 && <option value={form.model}>{form.model || "（先点发现模型）"}</option>}
            {discoveredModels.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <label className="field checkbox">
          <input type="checkbox" checked={form.reasoning} onChange={(e) => onToggleReasoning(e.target.checked)} />
          <span>思考模式</span>
        </label>
        <label className="field checkbox">
          <input type="checkbox" checked={form.enableAuth} onChange={(e) => setForm("enableAuth", e.target.checked)} />
          <span>启用访问鉴权</span>
        </label>
        <label className="field checkbox" title="开启后，除 rm -rf / dd / 管道到 sh 等破坏性命令外，所有工具（写文件、执行命令等）自动放行，不再每步都弹人工审批。让 agent 任务流畅闭环。对应 approval_level=OFF。">
          <input type="checkbox" checked={form.trustMode} onChange={(e) => setForm("trustMode", e.target.checked)} />
          <span>信任模式（免逐步审批，自动放行工具）</span>
        </label>
        {form.enableAuth && (
          <label className="field">
            <span>服务令牌</span>
            <input type="password" value={form.serviceKey} onChange={(e) => setForm("serviceKey", e.target.value)} placeholder="前端将保存到 localStorage" />
          </label>
        )}
        <div className="modal-actions">
          <button onClick={onClose}>取消</button>
          <button className="primary" onClick={onSave}>保存</button>
        </div>
      </div>
    </div>
  );
}
