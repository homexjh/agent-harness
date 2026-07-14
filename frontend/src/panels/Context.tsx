import { useEffect, useState, useCallback } from "react";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8123";

function apiFetch(path: string, opts: any = {}) {
  const token = localStorage.getItem("svc_token") || "";
  const headers: any = { ...(opts.headers || {}) };
  if (token) headers["x-api-key"] = token;
  return fetch(API_URL + path, { ...opts, headers });
}

function defaultConfig() {
  return {
    budget_tokens: 8000,
    enable_recall: true,
    strip_media: true,
    max_tool_result_chars: 0,
  };
}

function Toggle({
  label,
  help,
  checked,
  onChange,
}: {
  label: string;
  help?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div
      className="form-row"
      style={{ alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}
    >
      <div style={{ flex: 1 }}>
        <div style={{ fontWeight: 600, fontSize: 13 }}>{label}</div>
        {help && <div className="card-desc" style={{ marginBottom: 0 }}>{help}</div>}
      </div>
      <label className="core-file-toggle" style={{ marginLeft: 12 }}>
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        <span className="toggle-slider" />
      </label>
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  placeholder?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        type="number"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

export function ContextPanel() {
  const [cfg, setCfg] = useState<any>(defaultConfig());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  // inspect 区域
  const [threads, setThreads] = useState<string[]>([]);
  const [threadId, setThreadId] = useState<string>("");
  const [inspect, setInspect] = useState<any>(null);
  const [inspecting, setInspecting] = useState(false);
  const [clearing, setClearing] = useState(false);

  const update = useCallback((key: string, value: any) => {
    setCfg((prev: any) => ({ ...prev, [key]: value }));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const c = await (await apiFetch("/context/config")).json();
      if (c && Object.keys(c).length) setCfg(c);
    } catch (e) {
      /* 用默认配置 */
    }
    try {
      const t = await (await apiFetch("/context/threads")).json();
      setThreads(t.threads || []);
      if (!threadId && (t.threads || []).length) setThreadId(t.threads[0]);
    } catch (e) {
      setThreads([]);
    }
    setLoading(false);
  }, [threadId]);

  useEffect(() => {
    load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      await apiFetch("/context/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: cfg }),
      });
      setMsg("配置已保存并重建 Context Manager");
      load();
    } catch (e: any) {
      setMsg("保存失败：" + (e?.message || e));
    }
    setSaving(false);
  };

  const doInspect = async () => {
    if (!threadId.trim()) return;
    setInspecting(true);
    setMsg(null);
    try {
      const r = await (await apiFetch("/context/inspect?thread_id=" + encodeURIComponent(threadId))).json();
      setInspect(r);
    } catch (e: any) {
      setInspect(null);
      setMsg("检视失败：" + (e?.message || e));
    }
    setInspecting(false);
  };

  const doClear = async () => {
    if (!threadId.trim()) {
      setMsg("请先选择要清空的 thread");
      return;
    }
    if (!window.confirm(`确认清空 thread "${threadId}" 的全部存储 turn？折叠区原文将不可恢复。`)) {
      return;
    }
    setClearing(true);
    setMsg(null);
    try {
      const r = await (
        await apiFetch("/context/clear?thread_id=" + encodeURIComponent(threadId), { method: "POST" })
      ).json();
      setMsg(`已清空 ${r.removed ?? 0} 条 turn`);
      setInspect(null);
      load();
    } catch (e: any) {
      setMsg("清空失败：" + (e?.message || e));
    }
    setClearing(false);
  };

  return (
    <div className="memory-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Workspace</span>
          <span className="bc-sep">/</span>
          <b>Context</b>
        </div>
        <div className="page-actions">
          {msg && <span className="panel-msg">{msg}</span>}
          <button className="tool-foot-btn active" onClick={save} disabled={saving}>
            {saving ? "保存中…" : "保存配置"}
          </button>
        </div>
      </div>

      {loading && <div className="panel-hint">加载中…</div>}

      {/* 主配置 */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">Context Manager</div>
        <div className="card-desc">
          折叠不摘要的上下文管理（对齐 QwenPaw LightContextCard / Scroll Context）：超出预算后最旧
          turns 折叠为召回桩，原文写穿 store 永不丢失。
        </div>
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 220 }}>
            <NumberField
              label="Context Budget (tokens)"
              value={cfg.budget_tokens}
              onChange={(v) => update("budget_tokens", v)}
            />
            <div className="card-desc" style={{ marginTop: -4 }}>
              超过后从最旧往新折叠（阈值压缩）。
            </div>
          </div>
          <div style={{ flex: 1, minWidth: 220 }}>
            <NumberField
              label="Tool result pruning (chars, 0=off)"
              value={cfg.max_tool_result_chars}
              onChange={(v) => update("max_tool_result_chars", v)}
            />
            <div className="card-desc" style={{ marginTop: -4 }}>
              工具结果超长则在窗口截断（QwenPaw ToolResultPruning 等效）；store 仍留全文，recall 可还原。
            </div>
          </div>
        </div>
        <Toggle
          label="Enable recall"
          help="允许 agent 在 thread 内显式 recall 还原被折叠的历史"
          checked={cfg.enable_recall}
          onChange={(v) => update("enable_recall", v)}
        />
        <Toggle
          label="Strip media from history"
          help="把历史里的 base64 图片/音视频从上下文剥离省 token"
          checked={cfg.strip_media}
          onChange={(v) => update("strip_media", v)}
        />
      </div>

      {/* Inspect + clear */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">Inspect & Clear</div>
        <div className="card-desc">
          检视某 thread 的折叠情况与存储全文；或清空其存储（原文不可恢复）。
        </div>
        <div className="form-row">
          <select value={threadId} onChange={(e) => setThreadId(e.target.value)} style={{ flex: 1 }}>
            {threads.length === 0 && <option value="">（暂无 thread）</option>}
            {threads.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
          <button className="tool-foot-btn active" onClick={doInspect} disabled={inspecting || !threadId}>
            {inspecting ? "检视中…" : "Inspect"}
          </button>
          <button className="tool-foot-btn" onClick={doClear} disabled={clearing || !threadId}>
            {clearing ? "清空中…" : "Clear"}
          </button>
        </div>

        {inspect && (
          <>
            <div className="stats-grid" style={{ marginTop: 12 }}>
              <div className="stat">
                <span>Total turns</span>
                <b>{inspect.total_turns ?? 0}</b>
              </div>
              <div className="stat">
                <span>Budget</span>
                <b>{inspect.budget_tokens ?? 0}</b>
              </div>
              <div className="stat">
                <span>Window size</span>
                <b>{inspect.window_size ?? 0}</b>
              </div>
              <div className="stat">
                <span>Folded</span>
                <b>{(inspect.folded_seqs || []).length}</b>
              </div>
              <div className="stat">
                <span>Recall</span>
                <b style={{ color: inspect.recall_enabled ? "var(--accent)" : "#999" }}>
                  {inspect.recall_enabled ? "on" : "off"}
                </b>
              </div>
              <div className="stat">
                <span>Strip media</span>
                <b style={{ color: inspect.strip_media ? "var(--accent)" : "#999" }}>
                  {inspect.strip_media ? "on" : "off"}
                </b>
              </div>
            </div>
            {inspect.folded_seqs && inspect.folded_seqs.length > 0 && (
              <div className="card-meta" style={{ marginTop: 8 }}>
                已折叠 seq：{inspect.folded_seqs.join(", ")}
              </div>
            )}
            {inspect.turns && inspect.turns.length > 0 && (
              <div style={{ marginTop: 12, overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                  <thead>
                    <tr style={{ textAlign: "left", color: "var(--muted)" }}>
                      <th style={{ padding: "4px 8px" }}>seq</th>
                      <th style={{ padding: "4px 8px" }}>type</th>
                      <th style={{ padding: "4px 8px" }}>chars</th>
                      <th style={{ padding: "4px 8px" }}>preview</th>
                    </tr>
                  </thead>
                  <tbody>
                    {inspect.turns.map((t: any) => (
                      <tr key={t.seq} style={{ borderTop: "1px solid var(--border)" }}>
                        <td style={{ padding: "4px 8px" }}>{t.seq}</td>
                        <td style={{ padding: "4px 8px" }}>{t.type}</td>
                        <td style={{ padding: "4px 8px" }}>{t.chars}</td>
                        <td
                          style={{
                            padding: "4px 8px",
                            maxWidth: 420,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {t.preview}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
