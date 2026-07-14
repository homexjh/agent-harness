// ===========================================================================
// Settings 中心 —— 对齐 QwenPaw Settings 的 7 个缺失模块
//   Models / Environments / Security / Token Usage / Backups / Voice / Debug
// 全部为纯 REST 读写 ~/.agent-harness 下的 JSON，不经过 LLM 主循环，不影响聊天时延。
// ===========================================================================
import { useEffect, useState } from "react";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8123";

function apiFetch(path: string, opts: any = {}) {
  const token = localStorage.getItem("svc_token") || "";
  const headers: any = { ...(opts.headers || {}) };
  if (token) headers["x-api-key"] = token;
  return fetch(API_URL + path, { ...opts, headers });
}

function formatBytes(n: number) {
  if (!n) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function fmtNum(n: number) {
  return (n ?? 0).toLocaleString();
}

function PageHeader({ title, hint, onRefresh, refreshing }: any) {
  return (
    <div className="plugin-panel">
      <div className="page-header">
        <div>
          <h2>{title}</h2>
          {hint && <p className="panel-hint">{hint}</p>}
        </div>
        {onRefresh && (
          <button className="files-refresh" onClick={onRefresh} disabled={refreshing}>
            {refreshing ? "加载中…" : "↻ 刷新"}
          </button>
        )}
      </div>
    </div>
  );
}

// ===========================================================================
// Models
// ===========================================================================
export function ModelsPanel() {
  const [provider, setProvider] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("deepseek-chat");
  const [reasoning, setReasoning] = useState(false);
  const [providers, setProviders] = useState<any[]>([]);
  const [msg, setMsg] = useState("");
  const [testing, setTesting] = useState(false);
  const [testMsg, setTestMsg] = useState("");

  const load = async () => {
    try {
      const [cfg, pv] = await Promise.all([apiFetch("/config").then((r) => r.json()), apiFetch("/providers").then((r) => r.json())]);
      setProvider(cfg.llm?.provider || "");
      setBaseUrl(cfg.llm?.base_url || "");
      setApiKey(cfg.llm?.api_key === "****" ? "" : cfg.llm?.api_key || "");
      setModel(cfg.llm?.model || "deepseek-chat");
      setReasoning(!!cfg.llm?.reasoning);
      setProviders(pv.providers || []);
    } catch (e: any) {
      setMsg("加载失败：" + (e?.message || e));
    }
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    setMsg("");
    try {
      const cur = await (await apiFetch("/config")).json();
      const body: any = {
        llm: {
          provider: provider.trim(),
          base_url: baseUrl.trim(),
          model: model.trim() || "deepseek-chat",
          reasoning: !!reasoning,
        },
        security: cur.security || { enable_auth: false },
        language: cur.language || "zh",
        approval_level: cur.approval_level || "AUTO",
        plan: cur.plan || { enabled: false },
        rate_limiter: cur.rate_limiter || { enabled: false, requests_per_minute: 60 },
        running: cur.running || {},
        rate_limit: cur.rate_limit || 0,
      };
      if (apiKey.trim()) body.llm.api_key = apiKey.trim();
      const r = await apiFetch("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      setMsg("✓ 模型配置已保存，即时生效。");
    } catch (e: any) {
      setMsg("保存失败：" + (e?.message || e));
    }
  };

  const testConn = async () => {
    setTesting(true);
    setTestMsg("");
    try {
      const r = await apiFetch("/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: baseUrl.trim(), api_key: apiKey.trim() }),
      });
      const d = await r.json();
      setTestMsg(d.key_valid ? `✓ 连接成功${d.models?.length ? `，发现 ${d.models.length} 个模型` : ""}` : "✗ 连接失败或密钥无效");
    } catch (e: any) {
      setTestMsg("测试出错：" + (e?.message || e));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="plugin-panel">
      <div className="page-header"><h2>Models</h2><p className="panel-hint">配置 LLM 厂商、Base URL、模型与思考模式。保存后即时生效，无需重启。</p></div>
      {msg && <div className="panel-msg">{msg}</div>}
      <div className="card">
        <div className="card-head">模型接入</div>
        <label className="field"><span>模型厂商</span>
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="">— 自定义 / 手动填 Base URL —</option>
            {providers.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
        </label>
        <label className="field"><span>Base URL</span>
          <input value={baseUrl} placeholder="如 https://api.deepseek.com/v1" onChange={(e) => setBaseUrl(e.target.value)} />
        </label>
        <label className="field"><span>API Key</span>
          <input type="password" value={apiKey} placeholder="留空则保留当前值" onChange={(e) => setApiKey(e.target.value)} />
        </label>
        <label className="field"><span>模型</span>
          <input value={model} onChange={(e) => setModel(e.target.value)} />
        </label>
        <label className="field checkbox"><input type="checkbox" checked={reasoning} onChange={(e) => setReasoning(e.target.checked)} /><span>思考模式（reasoning）</span></label>
        <div className="tool-foot-btn">
          <button className="discover" onClick={testConn} disabled={testing || !apiKey.trim()}>{testing ? "测试中…" : "测试连接"}</button>
          {testMsg && <span className="panel-msg" style={{ margin: 0 }}>{testMsg}</span>}
          <button className="primary" onClick={save}>保存</button>
        </div>
      </div>
    </div>
  );
}

// ===========================================================================
// Environments
// ===========================================================================
export function EnvsPanel() {
  const [vars, setVars] = useState<{ key: string; value: string; secret: boolean }[]>([]);
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const d = await (await apiFetch("/settings/envs")).json();
      setVars(d.vars || []);
    } catch (e: any) { setMsg("加载失败：" + (e?.message || e)); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const update = (i: number, patch: any) => setVars((prev) => prev.map((v, idx) => (idx === i ? { ...v, ...patch } : v)));
  const addRow = () => setVars((prev) => [...prev, { key: "", value: "", secret: false }]);
  const delRow = (i: number) => setVars((prev) => prev.filter((_, idx) => idx !== i));

  const save = async () => {
    setMsg("");
    const clean = vars.filter((v) => v.key.trim());
    try {
      const r = await apiFetch("/settings/envs", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vars: clean }),
      });
      if (!r.ok) throw new Error(await r.text());
      setMsg("✓ 环境变量已保存并注入当前进程。");
      load();
    } catch (e: any) { setMsg("保存失败：" + (e?.message || e)); }
  };

  return (
    <div className="plugin-panel">
      <div className="page-header"><h2>Environments</h2><p className="panel-hint">环境变量（如 API_KEY、搜索密钥）。保存后注入运行进程，供工具/子进程读取。secret 项在界面上以密码框显示。</p>
        <button className="files-refresh" onClick={load} disabled={loading}>{loading ? "加载中…" : "↻ 刷新"}</button>
      </div>
      {msg && <div className="panel-msg">{msg}</div>}
      <div className="card">
        <div className="card-head">变量列表</div>
        <table className="env-table">
          <thead><tr><th>Key</th><th>Value</th><th>Secret</th><th></th></tr></thead>
          <tbody>
            {vars.map((v, i) => (
              <tr key={i}>
                <td><input value={v.key} placeholder="NAME" onChange={(e) => update(i, { key: e.target.value })} /></td>
                <td>{v.secret
                  ? <input type="password" value={v.value} placeholder="••••••" onChange={(e) => update(i, { value: e.target.value })} />
                  : <input value={v.value} onChange={(e) => update(i, { value: e.target.value })} />}</td>
                <td style={{ textAlign: "center" }}><input type="checkbox" checked={v.secret} onChange={(e) => update(i, { secret: e.target.checked })} /></td>
                <td><button className="ghost small" onClick={() => delRow(i)}>✕</button></td>
              </tr>
            ))}
            {vars.length === 0 && <tr><td colSpan={4} className="muted">暂无变量，点击下方新增。</td></tr>}
          </tbody>
        </table>
        <div className="tool-foot-btn">
          <button className="discover" onClick={addRow}>＋ 新增</button>
          <button className="primary" onClick={save}>保存</button>
        </div>
      </div>
    </div>
  );
}

// ===========================================================================
// Security
// ===========================================================================
export function SecurityPanel() {
  const [data, setData] = useState<any>({
    tool_guard: { enabled: true, block_destructive: true },
    file_guard: { enabled: true, block_path_escape: true },
    skill_scanner: { enabled: true, scan_on_load: true },
    enable_auth: false,
    approval_level: "AUTO",
    allow_no_auth_hosts: [],
  });
  const [hostsText, setHostsText] = useState("");
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const d = await (await apiFetch("/settings/security")).json();
      setData(d);
      setHostsText((d.allow_no_auth_hosts || []).join(", "));
    } catch (e: any) { setMsg("加载失败：" + (e?.message || e)); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const toggle = (group: string, key: string, val: boolean) =>
    setData((prev: any) => ({ ...prev, [group]: { ...(prev[group] || {}), [key]: val } }));

  const save = async () => {
    setMsg("");
    try {
      const body = {
        tool_guard: data.tool_guard,
        file_guard: data.file_guard,
        skill_scanner: data.skill_scanner,
        enable_auth: !!data.enable_auth,
        approval_level: data.approval_level,
        allow_no_auth_hosts: hostsText.split(",").map((s) => s.trim()).filter(Boolean),
      };
      const r = await apiFetch("/settings/security", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(await r.text());
      setMsg("✓ 安全策略已保存；鉴权与审批级别即时生效。");
      load();
    } catch (e: any) { setMsg("保存失败：" + (e?.message || e)); }
  };

  return (
    <div className="plugin-panel">
      <div className="page-header"><h2>Security</h2><p className="panel-hint">工具守卫、文件守卫、技能扫描开关，以及服务鉴权与审批级别（即时生效）。</p>
        <button className="files-refresh" onClick={load} disabled={loading}>{loading ? "加载中…" : "↻ 刷新"}</button>
      </div>
      {msg && <div className="panel-msg">{msg}</div>}
      <div className="card">
        <div className="card-head">守卫策略</div>
        <label className="field checkbox"><input type="checkbox" checked={!!data.tool_guard?.enabled} onChange={(e) => toggle("tool_guard", "enabled", e.target.checked)} /><span>工具守卫（Tool Guard）</span></label>
        <label className="field checkbox"><input type="checkbox" checked={!!data.tool_guard?.block_destructive} onChange={(e) => toggle("tool_guard", "block_destructive", e.target.checked)} /><span>拦截破坏性命令（rm -rf / dd 等）</span></label>
        <label className="field checkbox"><input type="checkbox" checked={!!data.file_guard?.enabled} onChange={(e) => toggle("file_guard", "enabled", e.target.checked)} /><span>文件守卫（File Guard）</span></label>
        <label className="field checkbox"><input type="checkbox" checked={!!data.file_guard?.block_path_escape} onChange={(e) => toggle("file_guard", "block_path_escape", e.target.checked)} /><span>禁止越界访问工作区外路径</span></label>
        <label className="field checkbox"><input type="checkbox" checked={!!data.skill_scanner?.enabled} onChange={(e) => toggle("skill_scanner", "enabled", e.target.checked)} /><span>技能扫描（Skill Scanner）</span></label>
      </div>
      <div className="card">
        <div className="card-head">访问与审批</div>
        <label className="field checkbox"><input type="checkbox" checked={!!data.enable_auth} onChange={(e) => setData((p: any) => ({ ...p, enable_auth: e.target.checked }))} /><span>启用服务鉴权（Bearer / x-api-key）</span></label>
        <label className="field"><span>审批级别</span>
          <select value={data.approval_level} onChange={(e) => setData((p: any) => ({ ...p, approval_level: e.target.value }))}>
            <option value="OFF">OFF（全部自动放行）</option>
            <option value="SMART">SMART（安全操作自动放行）</option>
            <option value="AUTO">AUTO（默认）</option>
            <option value="STRICT">STRICT（全部人工审批）</option>
          </select>
        </label>
        <label className="field"><span>免鉴权主机（逗号分隔）</span>
          <input value={hostsText} placeholder="localhost, 127.0.0.1" onChange={(e) => setHostsText(e.target.value)} />
        </label>
        <div className="tool-foot-btn"><button className="primary" onClick={save}>保存</button></div>
      </div>
    </div>
  );
}

// ===========================================================================
// Token Usage
// ===========================================================================
export function TokenUsagePanel() {
  const [summary, setSummary] = useState<any>(null);
  const [details, setDetails] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const [s, d] = await Promise.all([
        apiFetch("/settings/token-usage").then((r) => r.json()),
        apiFetch("/settings/token-usage/details?limit=50").then((r) => r.json()),
      ]);
      setSummary(s);
      setDetails(d.details || []);
    } catch (e: any) { /* 忽略轮询错误 */ }
    finally { setLoading(false); }
  };
  useEffect(() => {
    load();
    if (!auto) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [auto]);

  return (
    <div className="plugin-panel">
      <div className="page-header"><h2>Token Usage</h2><p className="panel-hint">每次 LLM 调用结束后的 token 用量统计（非阻塞记录）。每 5 秒自动刷新。</p>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label className="field checkbox" style={{ margin: 0 }}><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /><span>自动刷新</span></label>
          <button className="files-refresh" onClick={load} disabled={loading}>{loading ? "…" : "↻"}</button>
        </div>
      </div>
      {summary && (
        <div className="card">
          <div className="card-head">汇总（共 {fmtNum(summary.count)} 次调用）</div>
          <div className="stats-grid">
            <div className="stat"><span>输入 tokens</span><b>{fmtNum(summary.total_prompt_tokens)}</b></div>
            <div className="stat"><span>输出 tokens</span><b>{fmtNum(summary.total_completion_tokens)}</b></div>
            <div className="stat"><span>合计 tokens</span><b>{fmtNum(summary.total_tokens)}</b></div>
          </div>
          <div className="stat-cols">
            <div><div className="stat-sub">按模型</div>
              {Object.entries(summary.by_model || {}).map(([k, v]) => <div key={k} className="stat-row"><span>{k}</span><b>{fmtNum(v as number)}</b></div>)}
              {Object.keys(summary.by_model || {}).length === 0 && <div className="muted">暂无数据</div>}
            </div>
            <div><div className="stat-sub">按厂商</div>
              {Object.entries(summary.by_provider || {}).map(([k, v]) => <div key={k} className="stat-row"><span>{k}</span><b>{fmtNum(v as number)}</b></div>)}
              {Object.keys(summary.by_provider || {}).length === 0 && <div className="muted">暂无数据</div>}
            </div>
            <div><div className="stat-sub">按日期</div>
              {Object.entries(summary.by_date || {}).map(([k, v]) => <div key={k} className="stat-row"><span>{k}</span><b>{fmtNum(v as number)}</b></div>)}
              {Object.keys(summary.by_date || {}).length === 0 && <div className="muted">暂无数据</div>}
            </div>
          </div>
        </div>
      )}
      <div className="card">
        <div className="card-head">最近明细</div>
        <table className="env-table">
          <thead><tr><th>时间</th><th>模型</th><th>厂商</th><th>输入</th><th>输出</th><th>合计</th></tr></thead>
          <tbody>
            {details.map((r, i) => (
              <tr key={i}>
                <td>{new Date((r.ts || 0) * 1000).toLocaleString()}</td>
                <td>{r.model}</td><td>{r.provider}</td>
                <td>{fmtNum(r.prompt_tokens)}</td><td>{fmtNum(r.completion_tokens)}</td><td>{fmtNum(r.total_tokens)}</td>
              </tr>
            ))}
            {details.length === 0 && <tr><td colSpan={6} className="muted">还没有用量记录，先发几条消息试试。</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ===========================================================================
// Backups
// ===========================================================================
export function BackupsPanel() {
  const [backups, setBackups] = useState<any[]>([]);
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const d = await (await apiFetch("/settings/backups")).json();
      setBackups(d.backups || []);
    } catch (e: any) { setMsg("加载失败：" + (e?.message || e)); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const create = async () => {
    setCreating(true); setMsg("");
    try {
      const r = await apiFetch("/settings/backups", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "" }) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "创建失败");
      setMsg("✓ 已创建备份：" + d.name);
      load();
    } catch (e: any) { setMsg("创建失败：" + (e?.message || e)); }
    finally { setCreating(false); }
  };

  const restore = async (id: string) => {
    if (!window.confirm(`确定从备份 ${id} 恢复配置与状态？`)) return;
    setMsg("");
    try {
      const r = await apiFetch(`/settings/backups/${id}/restore`, { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      setMsg("✓ 已从 " + id + " 恢复。部分配置需重启后完全生效。");
    } catch (e: any) { setMsg("恢复失败：" + (e?.message || e)); }
  };

  const del = async (id: string) => {
    if (!window.confirm(`删除备份 ${id}？`)) return;
    try {
      await apiFetch(`/settings/backups/${id}`, { method: "DELETE" });
      setMsg("✓ 已删除 " + id);
      load();
    } catch (e: any) { setMsg("删除失败：" + (e?.message || e)); }
  };

  const download = (id: string) => { window.open(API_URL + `/settings/backups/${id}/download`, "_blank"); };

  return (
    <div className="plugin-panel">
      <div className="page-header"><h2>Backups</h2><p className="panel-hint">备份配置目录（runtime.json）与 ~/.agent-harness 下的运行状态（cron、agents、skills、记忆等）。可下载、恢复或删除。</p>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="files-refresh" onClick={load} disabled={loading}>{loading ? "加载中…" : "↻ 刷新"}</button>
          <button className="primary" onClick={create} disabled={creating}>{creating ? "备份中…" : "＋ 创建备份"}</button>
        </div>
      </div>
      {msg && <div className="panel-msg">{msg}</div>}
      <div className="card">
        <div className="card-head">备份列表</div>
        <table className="env-table">
          <thead><tr><th>名称</th><th>大小</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>
            {backups.map((b) => (
              <tr key={b.id}>
                <td>{b.name}</td>
                <td>{formatBytes(b.size)}</td>
                <td>{new Date(b.created * 1000).toLocaleString()}</td>
                <td className="row-actions">
                  <button className="ghost small" onClick={() => download(b.id)}>下载</button>
                  <button className="ghost small" onClick={() => restore(b.id)}>恢复</button>
                  <button className="ghost small" onClick={() => del(b.id)}>删除</button>
                </td>
              </tr>
            ))}
            {backups.length === 0 && <tr><td colSpan={4} className="muted">暂无备份，点击右上角创建。</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ===========================================================================
// Voice Transcription
// ===========================================================================
export function VoicePanel() {
  const [data, setData] = useState<any>({ enabled: false, audio_mode: "off", stt_provider: "whisper-local", tts_provider: "none", language: "zh" });
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try { const d = await (await apiFetch("/settings/voice")).json(); setData(d); }
    catch (e: any) { setMsg("加载失败：" + (e?.message || e)); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    setMsg("");
    try {
      const r = await apiFetch("/settings/voice", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!r.ok) throw new Error(await r.text());
      setMsg("✓ 语音配置已保存。");
      load();
    } catch (e: any) { setMsg("保存失败：" + (e?.message || e)); }
  };

  return (
    <div className="plugin-panel">
      <div className="page-header"><h2>Voice Transcription</h2><p className="panel-hint">语音输入/转写与语音合成配置。转录依赖 STT 提供方（whisper-local 需本地 whisper 服务）。</p>
        <button className="files-refresh" onClick={load} disabled={loading}>{loading ? "加载中…" : "↻ 刷新"}</button>
      </div>
      {msg && <div className="panel-msg">{msg}</div>}
      <div className="card">
        <div className="card-head">语音设置</div>
        <label className="field checkbox"><input type="checkbox" checked={!!data.enabled} onChange={(e) => setData((p: any) => ({ ...p, enabled: e.target.checked }))} /><span>启用语音</span></label>
        <label className="field"><span>音频模式</span>
          <select value={data.audio_mode} onChange={(e) => setData((p: any) => ({ ...p, audio_mode: e.target.value }))}>
            <option value="off">off（关闭）</option>
            <option value="push-to-talk">push-to-talk（按键说话）</option>
            <option value="always-on">always-on（持续监听）</option>
          </select>
        </label>
        <label className="field"><span>STT 提供方</span>
          <select value={data.stt_provider} onChange={(e) => setData((p: any) => ({ ...p, stt_provider: e.target.value }))}>
            <option value="whisper-local">whisper-local</option>
            <option value="openai">openai</option>
            <option value="none">none</option>
          </select>
        </label>
        <label className="field"><span>TTS 提供方</span>
          <select value={data.tts_provider} onChange={(e) => setData((p: any) => ({ ...p, tts_provider: e.target.value }))}>
            <option value="none">none</option>
            <option value="openai">openai</option>
            <option value="edge">edge</option>
          </select>
        </label>
        <label className="field"><span>语言</span>
          <input value={data.language} placeholder="zh" onChange={(e) => setData((p: any) => ({ ...p, language: e.target.value }))} />
        </label>
        {data.note && <p className="panel-hint">{data.note}</p>}
        <div className="tool-foot-btn"><button className="primary" onClick={save}>保存</button></div>
      </div>
    </div>
  );
}

// ===========================================================================
// Debug
// ===========================================================================
export function DebugPanel() {
  const [lines, setLines] = useState<string[]>([]);
  const [exists, setExists] = useState(true);
  const [loading, setLoading] = useState(false);
  const [auto, setAuto] = useState(false);
  const [err, setErr] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const d = await (await apiFetch("/settings/debug/logs?lines=400")).json();
      setLines(d.lines || []);
      setExists(d.exists);
      setErr(d.error || "");
    } catch (e: any) { setErr(e?.message || String(e)); }
    finally { setLoading(false); }
  };
  useEffect(() => {
    load();
    if (!auto) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [auto]);

  return (
    <div className="plugin-panel">
      <div className="page-header"><h2>Debug</h2><p className="panel-hint">后端运行日志（末尾 400 行）。可开启自动刷新实时观察。</p>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label className="field checkbox" style={{ margin: 0 }}><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /><span>自动刷新</span></label>
          <button className="files-refresh" onClick={load} disabled={loading}>{loading ? "…" : "↻ 刷新"}</button>
        </div>
      </div>
      {!exists && <div className="panel-msg">日志文件尚不存在（后端启动后生成）。</div>}
      {err && <div className="panel-msg">读取错误：{err}</div>}
      <pre className="debug-log">{lines.join("\n")}</pre>
    </div>
  );
}
