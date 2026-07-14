import { useEffect, useMemo, useState } from "react";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8123";

function apiFetch(path: string, opts: any = {}) {
  const token = localStorage.getItem("svc_token") || "";
  const headers: any = { ...(opts.headers || {}) };
  if (token) headers["x-api-key"] = token;
  return fetch(API_URL + path, { ...opts, headers });
}

function formatBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(ts: number) {
  return new Date(ts * 1000).toLocaleString();
}

// ---------------------------------------------------------------------------
// Files 工作区面板（QwenPaw Workspace 风格）
// ---------------------------------------------------------------------------
function FileIcon({ type }: { type: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      {type === "dir" ? (
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      ) : (
        <>
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="16" y1="13" x2="8" y2="13" />
          <line x1="16" y1="17" x2="8" y2="17" />
          <polyline points="10 9 9 9 8 9" />
        </>
      )}
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 4 23 10 17 10" />
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
    </svg>
  );
}

function formatTimeAgo(ts: number): string {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 604800) return `${Math.floor(d / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function FilesPanel() {
  const [files, setFiles] = useState<any[]>([]);
  const [enabledFiles, setEnabledFiles] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [initLoading, setInitLoading] = useState(false);
  const [file, setFile] = useState<{ name: string; content: string; exists: boolean } | null>(null);
  const [msg, setMsg] = useState("");
  const [workspacePath, setWorkspacePath] = useState("");

  const loadFiles = async () => {
    setLoading(true);
    try {
      const [filesRes, enabledRes] = await Promise.all([
        apiFetch("/core-files"),
        apiFetch("/core-files/enabled"),
      ]);
      const fileList = await filesRes.json();
      const enabled = await enabledRes.json();
      setFiles(fileList || []);
      setEnabledFiles(Array.isArray(enabled) ? enabled : []);
    } catch (e: any) {
      setMsg("Failed to load files: " + (e?.message || e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadFiles();
    apiFetch("/heartbeat")
      .then((r) => r.json())
      .then((d) => setWorkspacePath(d.workspace || ""))
      .catch(() => {});
  }, []);

  const isEnabled = (name: string) => enabledFiles.includes(name);

  const openFile = async (name: string) => {
    try {
      const r = await apiFetch(`/core-files/${encodeURIComponent(name)}`);
      const d = await r.json();
      setFile({ name: d.filename, content: d.content, exists: true });
    } catch (e: any) {
      setMsg(`Failed to read ${name}: ${e?.message || e}`);
    }
  };

  const save = async () => {
    if (!file) return;
    try {
      await apiFetch(`/core-files/${encodeURIComponent(file.name)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: file.content }),
      });
      setMsg(`Saved ${file.name}`);
      setFile({ ...file, exists: true });
      loadFiles();
    } catch (e: any) {
      setMsg(`Failed to save ${file.name}: ${e?.message || e}`);
    }
  };

  const toggle = async (name: string, enabled: boolean) => {
    const next = enabled
      ? [...enabledFiles, name]
      : enabledFiles.filter((n) => n !== name);
    setEnabledFiles(next);
    try {
      await apiFetch("/core-files/enabled", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files: next }),
      });
      setMsg(`${name} ${enabled ? "enabled" : "disabled"}`);
      loadFiles();
    } catch (e: any) {
      setMsg(`Failed to update ${name}: ${e?.message || e}`);
    }
  };

  const initialize = async () => {
    if (!window.confirm("Initialize core files with bundled templates? Existing files will be kept.")) return;
    setInitLoading(true);
    try {
      const r = await apiFetch("/core-files/initialize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language: "zh" }),
      });
      const d = await r.json();
      setMsg(`Initialized: ${(d.created || []).join(", ") || "none"}`);
      loadFiles();
    } catch (e: any) {
      setMsg(`Failed to initialize: ${e?.message || e}`);
    }
    setInitLoading(false);
  };

  return (
    <div className="plugin-panel files-panel">
      <div className="files-header">
        <div className="files-breadcrumb">
          <span className="files-bc-parent">Workspace</span>
          <span className="files-bc-sep">/</span>
          <span className="files-bc-current">Files</span>
          <code className="files-workspace-path">Workspace: {workspacePath || "loading…"}</code>
        </div>
        <div className="files-actions">
          <button className="files-refresh" onClick={initialize} disabled={initLoading}>
            {initLoading ? "Initializing…" : "Initialize"}
          </button>
          <button className="files-refresh" onClick={loadFiles} disabled={loading}>
            <RefreshIcon /> Refresh
          </button>
        </div>
      </div>
      <p className="panel-hint">Bootstrap persona, identity, and tool guidance. Toggle files to include/exclude them from the agent context.</p>
      {msg && <div className="panel-msg">{msg}</div>}

      <div className="files-layout core-files-layout">
        <div className="core-files-list">
          <div className="core-files-section-title">
            <b>Core Files</b>
            <button className="core-files-refresh-icon" onClick={loadFiles} disabled={loading}><RefreshIcon /></button>
          </div>
          <div className="core-files-desc">Bootstrap persona, identity, and tool guidance.</div>

          {files.map((f) => (
            <div
              key={f.filename}
              className={`core-file-card ${isEnabled(f.filename) ? "enabled" : ""} ${file?.name === f.filename ? "active" : ""}`}
              onClick={() => openFile(f.filename)}
            >
              <span className="core-file-drag">⋮⋮</span>
              <div className="core-file-info">
                <span className={`core-file-dot ${isEnabled(f.filename) ? "on" : ""}`} />
                <b className={isEnabled(f.filename) ? "" : "disabled"}>{f.filename}</b>
              </div>
              <div className="core-file-meta">
                {f.size > 0 ? `${formatBytes(f.size)} · ${formatTimeAgo(new Date(f.modified_time).getTime() / 1000)}` : "Not created"}
              </div>
              <label className="core-file-toggle" onClick={(e) => e.stopPropagation()}>
                <input type="checkbox" checked={isEnabled(f.filename)} onChange={(e) => toggle(f.filename, e.target.checked)} />
                <span className="toggle-slider" />
              </label>
            </div>
          ))}
        </div>

        <div className="files-editor">
          {file ? (
            <>
              <div className="editor-bar">
                <div>
                  <div className="editor-file-name">{file.name}</div>
                  <div className="editor-file-path">{workspacePath}/{file.name}</div>
                </div>
                <button onClick={save}>{file.exists ? "Save" : "Create"}</button>
              </div>
              <textarea
                value={file.content}
                onChange={(e) => setFile({ ...file, content: e.target.value })}
                placeholder={file.exists ? "" : `Create ${file.name}...`}
              />
            </>
          ) : (
            <div className="files-empty-state">
              <FileIcon type="file" />
              <p>Select a file to edit</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tools 面板（QwenPaw Built-in Tools 风格）
// ---------------------------------------------------------------------------
const ICON_PALETTE = ["#f56a00", "#7265e6", "#ffbf00", "#00a2ae", "#87d068", "#1890ff", "#eb2f96", "#722ed1"];
function hashStringToIndex(value: string, mod: number): number {
  let hash = 0;
  for (let i = 0; i < value.length; i++) hash = (hash * 31 + value.charCodeAt(i)) | 0;
  return Math.abs(hash) % mod;
}
function ToolIcon({ name }: { name: string }) {
  const letter = name.charAt(0).toUpperCase();
  const bg = ICON_PALETTE[hashStringToIndex(name, ICON_PALETTE.length)];
  return (
    <span className="tool-icon-fallback" style={{ backgroundColor: bg }}>{letter}</span>
  );
}

function ToolConfigModal({
  tool,
  open,
  onClose,
  onSave,
}: {
  tool: any;
  open: boolean;
  onClose: () => void;
  onSave: (values: Record<string, string>) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open || !tool) return;
    const initial: Record<string, string> = {};
    (tool.config_fields || []).forEach((f: any) => {
      const v = tool.config_values?.[f.name];
      initial[f.name] = Array.isArray(v) ? v.join("\n") : typeof v === "string" ? v : "";
    });
    setValues(initial);
  }, [open, tool]);

  if (!open || !tool) return null;

  const save = async () => {
    setSaving(true);
    const out: Record<string, any> = {};
    (tool.config_fields || []).forEach((f: any) => {
      const raw = values[f.name] || "";
      if (f.type === "textarea") {
        out[f.name] = raw.split("\n").map((s) => s.trim()).filter(Boolean);
      } else {
        out[f.name] = raw.trim();
      }
    });
    await onSave(out);
    setSaving(false);
    onClose();
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-box" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <b>Configure {tool.name}</b>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          {(tool.config_fields || []).map((f: any) => (
            <label className="field" key={f.name}>
              <span>{f.label}</span>
              {f.type === "textarea" ? (
                <textarea
                  rows={4}
                  value={values[f.name] || ""}
                  onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                  placeholder={f.placeholder}
                />
              ) : (
                <input
                  value={values[f.name] || ""}
                  onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                  placeholder={f.placeholder}
                />
              )}
              {f.help && <span className="field-help">{f.help}</span>}
            </label>
          ))}
        </div>
        <div className="modal-foot">
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        </div>
      </div>
    </div>
  );
}

export function ToolsPanel() {
  const [tools, setTools] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [configTool, setConfigTool] = useState<any>(null);

  const load = async () => {
    setLoading(true);
    try {
      const r = await apiFetch("/tools");
      setTools(await r.json());
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const { enabled, disabled } = useMemo(() => {
    const e = tools.filter((t) => t.enabled);
    const d = tools.filter((t) => !t.enabled);
    return { enabled: e, disabled: d };
  }, [tools]);

  const allEnabled = tools.length > 0 && disabled.length === 0;

  const toggle = async (tool: any) => {
    await apiFetch(`/tools/${encodeURIComponent(tool.name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !tool.enabled }),
    });
    load();
  };

  const toggleAsync = async (tool: any) => {
    await apiFetch(`/tools/${encodeURIComponent(tool.name)}/async`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ async_execution: !tool.async_execution }),
    });
    load();
  };

  const saveConfig = async (values: Record<string, any>) => {
    await apiFetch(`/tools/${encodeURIComponent(configTool.name)}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: values }),
    });
    load();
  };

  const toggleAll = async () => {
    if (allEnabled) {
      for (const t of tools) {
        await apiFetch(`/tools/${encodeURIComponent(t.name)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: false }),
        });
      }
    } else {
      await apiFetch("/tools/enable", { method: "POST" });
    }
    load();
  };

  const isConfigured = (t: any) =>
    !t.requires_config || (t.config_values && Object.keys(t.config_values).length > 0);

  const renderCard = (t: any, enabledSection: boolean) => (
    <div className={`tool-card ${enabledSection ? "enabled" : ""}`} key={t.name}>
      <div className="tool-card-head">
        <h3 className="tool-name" title={t.name}>
          <ToolIcon name={t.name} />
          <span className="tool-name-text">{t.name}</span>
        </h3>
        <div className="tool-status">
          <span className={`status-dot ${t.enabled ? "on" : ""}`} />
          <span className="status-text">{t.enabled ? "Enabled" : "Disabled"}</span>
        </div>
      </div>
      <p className="tool-desc">{t.description}</p>
      {t.requires_config && (
        <div className="tool-config-hint">
          {isConfigured(t) ? (
            <span className="configured">✓ Configured</span>
          ) : (
            <span className="not-configured">⚠ Requires configuration</span>
          )}
        </div>
      )}
      <div className="tool-card-foot">
        {t.async_capable && (
          <button
            className={`tool-foot-btn ${t.async_execution ? "active" : ""}`}
            onClick={() => toggleAsync(t)}
            disabled={!t.enabled}
            title="Async execution"
          >
            {t.async_execution ? "⚡ Async" : "Async off"}
          </button>
        )}
        {t.requires_config && (
          <button className="tool-foot-btn" onClick={() => setConfigTool(t)}>
            Configure
          </button>
        )}
        <button className="tool-foot-btn" onClick={() => toggle(t)}>
          {t.enabled ? "Disable" : "Enable"}
        </button>
      </div>
    </div>
  );

  return (
    <div className="plugin-panel tools-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Workspace</span>
          <span className="bc-sep">/</span>
          <b>Tools</b>
        </div>
        <div className="page-actions">
          <label className="toggle-all">
            <span>{allEnabled ? "Disable All" : "Enable All"}</span>
            <input type="checkbox" checked={allEnabled} onChange={toggleAll} />
            <span className="toggle-slider" />
          </label>
        </div>
      </div>
      <p className="panel-hint">Built-in tools available to the current agent. Toggle to enable/disable; disabled tools are not loaded into the agent loop.</p>

      {loading ? (
        <div className="muted">Loading…</div>
      ) : (
        <>
          <div className="tool-section">
            <div className="tool-section-title">
              <span className="section-dot green" />
              Enabled
              <span className="section-count">{enabled.length} active</span>
            </div>
            {enabled.length > 0 ? (
              <div className="tools-grid">{enabled.map((t) => renderCard(t, true))}</div>
            ) : (
              <div className="empty-state">No enabled tools.</div>
            )}
          </div>

          {disabled.length > 0 && (
            <div className="tool-section">
              <div className="tool-section-title">
                <span className="section-dot gray" />
                Available
              </div>
              <div className="tools-grid">{disabled.map((t) => renderCard(t, false))}</div>
            </div>
          )}
        </>
      )}

      <ToolConfigModal
        tool={configTool}
        open={!!configTool}
        onClose={() => setConfigTool(null)}
        onSave={saveConfig}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Skills 面板（QwenPaw 风格）
// ---------------------------------------------------------------------------
function SkillIcon({ name, emoji }: { name: string; emoji?: string }) {
  const letter = name.charAt(0).toUpperCase();
  const bg = ICON_PALETTE[hashStringToIndex(name, ICON_PALETTE.length)];
  if (emoji) return <span className="skill-emoji">{emoji}</span>;
  return <span className="tool-icon-fallback" style={{ backgroundColor: bg }}>{letter}</span>;
}

export function SkillsPanel() {
  const [data, setData] = useState<{ skills: any[] }>({ skills: [] });
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState<string>("all");
  const [viewMode, setViewMode] = useState<"card" | "list">("card");

  const load = async () => {
    setLoading(true);
    try {
      const r = await apiFetch("/skills");
      setData(await r.json());
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const allTags = useMemo(() => {
    const set = new Set<string>();
    data.skills.forEach((s) => (s.tags || []).forEach((t: string) => set.add(t)));
    return ["all", ...Array.from(set).sort()];
  }, [data.skills]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return data.skills.filter((s) => {
      const matchQ = !q || s.name.toLowerCase().includes(q) || (s.description || "").toLowerCase().includes(q);
      const matchTag = tag === "all" || (s.tags || []).includes(tag);
      return matchQ && matchTag;
    });
  }, [data.skills, query, tag]);

  const enabled = filtered.filter((s) => s.enabled);
  const disabled = filtered.filter((s) => !s.enabled);

  const toggle = async (s: any) => {
    await apiFetch(`/skills/${encodeURIComponent(s.scope)}/${encodeURIComponent(s.id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !s.enabled }),
    });
    load();
  };

  const batch = async (enabled: boolean) => {
    const ids = data.skills.map((s) => `${s.scope}:${s.id}`);
    if (!ids.length) return;
    await apiFetch("/skills/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, enabled }),
    });
    load();
  };

  const allEnabled = data.skills.length > 0 && data.skills.every((s) => s.enabled);

  const renderCard = (s: any) => (
    <div className={`tool-card skill-card ${s.enabled ? "enabled" : ""}`} key={`${s.scope}:${s.id}`}>
      <div className="tool-card-head">
        <h3 className="tool-name" title={s.name}>
          <SkillIcon name={s.name} emoji={s.emoji} />
          <span className="tool-name-text">{s.name}</span>
        </h3>
        <div className="tool-status">
          <span className={`status-dot ${s.enabled ? "on" : ""}`} />
          <span className="status-text">{s.enabled ? "Enabled" : "Disabled"}</span>
        </div>
      </div>
      <p className="tool-desc">{s.description || "No description"}</p>
      <div className="skill-meta">
        <span className={`scope-badge ${s.scope}`}>{s.scope === "system" ? "Built-in" : "Custom"}</span>
        {s.version && <span className="version-badge">v{s.version}</span>}
        {(s.tags || []).map((t: string) => <span className="tag-badge" key={t}>{t}</span>)}
      </div>
      <div className="tool-card-foot">
        <button className="tool-foot-btn" onClick={() => toggle(s)}>
          {s.enabled ? "Disable" : "Enable"}
        </button>
      </div>
    </div>
  );

  const renderListItem = (s: any) => (
    <div className={`skill-list-item ${s.enabled ? "enabled" : ""}`} key={`${s.scope}:${s.id}`}>
      <div className="skill-list-info">
        <SkillIcon name={s.name} emoji={s.emoji} />
        <div className="skill-list-text">
          <b>{s.name}</b>
          <span>{s.description || "No description"}</span>
        </div>
      </div>
      <div className="skill-list-tags">
        <span className={`scope-badge ${s.scope}`}>{s.scope === "system" ? "Built-in" : "Custom"}</span>
        {(s.tags || []).slice(0, 3).map((t: string) => <span className="tag-badge" key={t}>{t}</span>)}
      </div>
      <div className="tool-status">
        <span className={`status-dot ${s.enabled ? "on" : ""}`} />
        <span>{s.enabled ? "Enabled" : "Disabled"}</span>
      </div>
      <button className="tool-foot-btn" onClick={() => toggle(s)}>
        {s.enabled ? "Disable" : "Enable"}
      </button>
    </div>
  );

  return (
    <div className="plugin-panel skills-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Workspace</span>
          <span className="bc-sep">/</span>
          <b>Skills</b>
        </div>
        <div className="page-actions">
          <label className="toggle-all">
            <span>{allEnabled ? "Disable All" : "Enable All"}</span>
            <input type="checkbox" checked={allEnabled} onChange={() => batch(!allEnabled)} />
            <span className="toggle-slider" />
          </label>
        </div>
      </div>
      <p className="panel-hint">Built-in and custom skills available to the current agent. Toggle to enable/disable.</p>

      <div className="skills-toolbar">
        <input
          className="skills-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search skills..."
        />
        <div className="skills-filter-tabs">
          {allTags.map((t) => (
            <button key={t} className={`filter-tab ${tag === t ? "active" : ""}`} onClick={() => setTag(t)}>
              {t === "all" ? "All" : t}
            </button>
          ))}
        </div>
        <div className="skills-view-toggle">
          <button className={viewMode === "card" ? "active" : ""} onClick={() => setViewMode("card")}>Cards</button>
          <button className={viewMode === "list" ? "active" : ""} onClick={() => setViewMode("list")}>List</button>
        </div>
      </div>

      {loading ? (
        <div className="muted">Loading…</div>
      ) : (
        <>
          <div className="tool-section">
            <div className="tool-section-title">
              <span className="section-dot green" />
              Enabled
              <span className="section-count">{enabled.length} active</span>
            </div>
            {enabled.length > 0 ? (
              viewMode === "card" ? (
                <div className="tools-grid">{enabled.map(renderCard)}</div>
              ) : (
                <div className="skills-list">{enabled.map(renderListItem)}</div>
              )
            ) : (
              <div className="empty-state">No enabled skills.</div>
            )}
          </div>

          {disabled.length > 0 && (
            <div className="tool-section">
              <div className="tool-section-title">
                <span className="section-dot gray" />
                Available
              </div>
              {viewMode === "card" ? (
                <div className="tools-grid">{disabled.map(renderCard)}</div>
              ) : (
                <div className="skills-list">{disabled.map(renderListItem)}</div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// MCP 面板（QwenPaw 风格）
// ---------------------------------------------------------------------------
type MCPTransport = "stdio" | "sse" | "streamable_http";

function McpTypeBadge({ transport }: { transport: string }) {
  const isRemote = transport === "streamable_http" || transport === "sse";
  return <span className={`mcp-type-badge ${isRemote ? "remote" : "local"}`}>{isRemote ? "Remote" : "Local"}</span>;
}

export function McpPanel() {
  const [clients, setClients] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"json" | "form">("json");
  const [jsonText, setJsonText] = useState(`{\n  "mcpServers": {\n    "example": {\n      "command": "npx",\n      "args": ["-y", "@example/mcp-server"],\n      "env": { "API_KEY": "<YOUR_KEY>" }\n    }\n  }\n}`);
  const [form, setForm] = useState({
    key: "",
    name: "",
    description: "",
    transport: "stdio" as MCPTransport,
    url: "",
    command: "",
    args: "",
    env: "",
    cwd: "",
  });

  const load = async () => {
    setLoading(true);
    try {
      const r = await apiFetch("/mcp");
      const d = await r.json();
      setClients(d.clients || []);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const createFromJson = async () => {
    try {
      const parsed = JSON.parse(jsonText);
      await apiFetch("/mcp/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      setModalOpen(false);
      load();
    } catch {
      alert("Invalid JSON format");
    }
  };

  const createFromForm = async () => {
    if (!form.key.trim()) { alert("Key is required"); return; }
    const isHttp = form.transport === "streamable_http" || form.transport === "sse";
    if (isHttp && !form.url.trim()) { alert("URL is required for HTTP/SSE transport"); return; }
    if (form.transport === "stdio" && !form.command.trim()) { alert("Command is required for stdio transport"); return; }

    const env: Record<string, string> = {};
    form.env.split("\n").map((s) => s.trim()).filter(Boolean).forEach((line) => {
      const idx = line.indexOf("=");
      if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    });

    await apiFetch("/mcp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: form.key.trim(),
        name: form.name.trim() || form.key.trim(),
        description: form.description,
        transport: form.transport,
        url: isHttp ? form.url.trim() : "",
        command: form.transport === "stdio" ? form.command.trim() : "",
        args: form.transport === "stdio" ? form.args.split(/[\n, ]+/).map((s) => s.trim()).filter(Boolean) : [],
        env,
        cwd: form.cwd.trim(),
      }),
    });
    setForm({ key: "", name: "", description: "", transport: "stdio", url: "", command: "", args: "", env: "", cwd: "" });
    setModalOpen(false);
    load();
  };

  const toggle = async (key: string, enabled: boolean) => {
    await apiFetch(`/mcp/${encodeURIComponent(key)}?enabled=${enabled}`, { method: "PATCH" });
    load();
  };

  const remove = async (key: string) => {
    if (!confirm(`Delete MCP client "${key}"?`)) return;
    await apiFetch(`/mcp/${encodeURIComponent(key)}`, { method: "DELETE" });
    load();
  };

  const enabled = clients.filter((c) => c.enabled);
  const disabled = clients.filter((c) => !c.enabled);

  const renderCard = (c: any) => (
    <div className={`tool-card mcp-card ${c.enabled ? "enabled" : ""}`} key={c.key}>
      <div className="tool-card-head">
        <h3 className="tool-name" title={c.name}>
          <span className="tool-icon-fallback" style={{ backgroundColor: "#7265e6" }}>M</span>
          <span className="tool-name-text">{c.name}</span>
        </h3>
        <div className="tool-status">
          <span className={`status-dot ${c.enabled ? "on" : ""}`} />
          <span>{c.enabled ? "Enabled" : "Disabled"}</span>
        </div>
      </div>
      <p className="tool-desc">{c.description || "No description"}</p>
      <div className="skill-meta">
        <McpTypeBadge transport={c.transport} />
        <span className="transport-badge">{c.transport}</span>
        {c.command && <span className="cmd-badge">{c.command}</span>}
      </div>
      <div className="tool-card-foot">
        <button className="tool-foot-btn" onClick={() => toggle(c.key, !c.enabled)}>
          {c.enabled ? "Disable" : "Enable"}
        </button>
        <button className="tool-foot-btn" onClick={() => remove(c.key)}>Delete</button>
      </div>
    </div>
  );

  const isHttp = form.transport === "streamable_http" || form.transport === "sse";

  return (
    <div className="plugin-panel mcp-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Workspace</span>
          <span className="bc-sep">/</span>
          <b>MCP</b>
        </div>
        <div className="page-actions">
          <button className="newchat small" onClick={() => setModalOpen(true)}>+ Create Client</button>
        </div>
      </div>
      <p className="panel-hint">Manage Model Context Protocol server configurations in ~/.agent-harness/mcp.json.</p>

      {loading ? (
        <div className="muted">Loading…</div>
      ) : clients.length === 0 ? (
        <div className="empty-state">No MCP clients configured. Click "Create Client" to add one.</div>
      ) : (
        <>
          <div className="tool-section">
            <div className="tool-section-title">
              <span className="section-dot green" />
              Enabled
              <span className="section-count">{enabled.length} active</span>
            </div>
            {enabled.length > 0 ? <div className="tools-grid">{enabled.map(renderCard)}</div> : <div className="empty-state">No enabled clients.</div>}
          </div>
          {disabled.length > 0 && (
            <div className="tool-section">
              <div className="tool-section-title"><span className="section-dot gray" />Available</div>
              <div className="tools-grid">{disabled.map(renderCard)}</div>
            </div>
          )}
        </>
      )}

      {modalOpen && (
        <div className="modal-backdrop" onClick={() => setModalOpen(false)}>
          <div className="modal-box wide" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head"><b>Create MCP Client</b><button className="modal-close" onClick={() => setModalOpen(false)}>×</button></div>
            <div className="modal-tabs">
              <button className={activeTab === "json" ? "active" : ""} onClick={() => setActiveTab("json")}>JSON Import</button>
              <button className={activeTab === "form" ? "active" : ""} onClick={() => setActiveTab("form")}>Form</button>
            </div>
            {activeTab === "json" ? (
              <div className="modal-body">
                <p className="field-help">Supports standard <code>{`{ "mcpServers": { ... } }`}</code> format or direct key-value object.</p>
                <textarea value={jsonText} onChange={(e) => setJsonText(e.target.value)} rows={16} className="json-textarea" />
              </div>
            ) : (
              <div className="modal-body">
                <div className="form-row">
                  <label className="field"><span>Key *</span><input value={form.key} onChange={(e) => setForm({ ...form, key: e.target.value })} placeholder="example-client" /></label>
                  <label className="field"><span>Name</span><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Example Client" /></label>
                </div>
                <label className="field"><span>Transport</span>
                  <select value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value as MCPTransport })}>
                    <option value="stdio">Stdio</option>
                    <option value="sse">SSE</option>
                    <option value="streamable_http">Streamable HTTP</option>
                  </select>
                </label>
                {isHttp && <label className="field"><span>URL *</span><input value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="https://mcp.example.com/mcp" /></label>}
                {form.transport === "stdio" && (
                  <>
                    <label className="field"><span>Command *</span><input value={form.command} onChange={(e) => setForm({ ...form, command: e.target.value })} placeholder="npx" /></label>
                    <label className="field"><span>Args</span><input value={form.args} onChange={(e) => setForm({ ...form, args: e.target.value })} placeholder="-y @example/mcp-server" /></label>
                    <label className="field"><span>Working Directory</span><input value={form.cwd} onChange={(e) => setForm({ ...form, cwd: e.target.value })} placeholder="/path/to/workdir" /></label>
                    <label className="field"><span>Env (KEY=VALUE per line)</span><textarea value={form.env} onChange={(e) => setForm({ ...form, env: e.target.value })} rows={3} /></label>
                  </>
                )}
                <label className="field"><span>Description</span><input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="What does this MCP client do?" /></label>
              </div>
            )}
            <div className="modal-foot">
              <button onClick={() => setModalOpen(false)}>Cancel</button>
              <button className="primary" onClick={activeTab === "json" ? createFromJson : createFromForm}>Create</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ACP 面板（QwenPaw 风格）
// ---------------------------------------------------------------------------
const BUILTIN_ACP_ORDER = ["opencode", "qwen_code", "claude_code", "codex"];
const ACP_ICON: Record<string, string> = {
  opencode: "🌟",
  qwen_code: "🛠",
  claude_code: "⚡",
  codex: "🔌",
};

type ACPFilter = "all" | "builtin" | "custom";

export function AcpPanel() {
  const [agents, setAgents] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<ACPFilter>("all");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [isCreate, setIsCreate] = useState(false);
  const [form, setForm] = useState<any>(({ key: "", name: "", command: "", args: "", env: "", enabled: true, trusted: true, tool_parse_mode: "call_title", stdio_buffer_limit_bytes: 4194304 }));

  const load = async () => {
    setLoading(true);
    try {
      const r = await apiFetch("/acp");
      const d = await r.json();
      setAgents(d.agents || {});
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const orderedKeys = useMemo(() => {
    const keys = Object.keys(agents);
    return [
      ...BUILTIN_ACP_ORDER.filter((k) => keys.includes(k)),
      ...keys.filter((k) => !BUILTIN_ACP_ORDER.includes(k)).sort(),
    ];
  }, [agents]);

  const cards = useMemo(() => {
    return orderedKeys
      .map((key) => ({ key, config: agents[key] }))
      .filter(({ config }) => {
        if (filter === "builtin") return config.builtin;
        if (filter === "custom") return !config.builtin;
        return true;
      })
      .sort((a, b) => Number(b.config.enabled) - Number(a.config.enabled));
  }, [orderedKeys, agents, filter]);

  const openCreate = () => {
    setIsCreate(true);
    setForm({ key: "", name: "", command: "", args: "", env: "", enabled: true, trusted: true, tool_parse_mode: "call_title", stdio_buffer_limit_bytes: 4194304 });
    setDrawerOpen(true);
  };

  const openEdit = (key: string) => {
    const c = agents[key];
    setIsCreate(false);
    setForm({
      key,
      name: c.name || key,
      command: c.command || "",
      args: (c.args || []).join(" "),
      env: Object.entries(c.env || {}).map(([k, v]) => `${k}=${v}`).join("\n"),
      enabled: c.enabled,
      trusted: c.trusted !== false,
      tool_parse_mode: c.tool_parse_mode || "call_title",
      stdio_buffer_limit_bytes: c.stdio_buffer_limit_bytes || 4194304,
    });
    setDrawerOpen(true);
  };

  const save = async () => {
    const key = (form.key || "").trim();
    if (!key) { alert("Key is required"); return; }
    const env: Record<string, string> = {};
    (form.env || "").split("\n").map((s: string) => s.trim()).filter(Boolean).forEach((line: string) => {
      const idx = line.indexOf("=");
      if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    });
    await apiFetch("/acp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key,
        name: form.name || key,
        command: form.command,
        args: (form.args || "").split(/\s+/).filter(Boolean),
        env,
        enabled: form.enabled,
        trusted: form.trusted,
        tool_parse_mode: form.tool_parse_mode,
        stdio_buffer_limit_bytes: Number(form.stdio_buffer_limit_bytes) || 4194304,
      }),
    });
    setDrawerOpen(false);
    load();
  };

  const toggle = async (key: string, enabled: boolean) => {
    await apiFetch(`/acp/${encodeURIComponent(key)}/enable?enabled=${enabled}`, { method: "PATCH" });
    load();
  };

  const remove = async (key: string) => {
    if (!confirm(`Delete ACP agent "${key}"?`)) return;
    await apiFetch(`/acp/${encodeURIComponent(key)}`, { method: "DELETE" });
    load();
  };

  return (
    <div className="plugin-panel acp-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Workspace</span>
          <span className="bc-sep">/</span>
          <b>ACP</b>
        </div>
        <div className="page-actions">
          <button className="newchat small" onClick={openCreate}>+ Create Agent</button>
        </div>
      </div>
      <p className="panel-hint">Agent Configuration Protocol agents. Enable built-in agents or add custom external agent commands.</p>

      <div className="acp-filter-tabs">
        {(["all", "builtin", "custom"] as ACPFilter[]).map((f) => (
          <button key={f} className={`filter-tab ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
            {f === "all" ? "All" : f === "builtin" ? "Built-in" : "Custom"}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="muted">Loading…</div>
      ) : cards.length === 0 ? (
        <div className="empty-state">No ACP agents found.</div>
      ) : (
        <div className="tools-grid acp-grid">
          {cards.map(({ key, config }) => (
            <div className={`tool-card acp-card ${config.enabled ? "enabled" : ""}`} key={key} onClick={() => openEdit(key)}>
              <div className="tool-card-head">
                <h3 className="tool-name" title={config.name || key}>
                  <span className="skill-emoji">{ACP_ICON[key] || "🤖"}</span>
                  <span className="tool-name-text">{config.name || key}</span>
                </h3>
                <div className="tool-status">
                  <span className={`status-dot ${config.enabled ? "on" : ""}`} />
                  <span>{config.enabled ? "Enabled" : "Disabled"}</span>
                </div>
              </div>
              <div className="skill-meta">
                <span className={`scope-badge ${config.builtin ? "system" : "project"}`}>{config.builtin ? "Built-in" : "Custom"}</span>
              </div>
              <p className="tool-desc">{config.command || "No command configured"} {config.args?.join(" ")}</p>
              <div className="tool-card-foot" onClick={(e) => e.stopPropagation()}>
                <button className="tool-foot-btn" onClick={(e) => { e.stopPropagation(); toggle(key, !config.enabled); }}>
                  {config.enabled ? "Disable" : "Enable"}
                </button>
                {!config.builtin && (
                  <button className="tool-foot-btn" onClick={(e) => { e.stopPropagation(); remove(key); }}>Delete</button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {drawerOpen && (
        <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <b>{isCreate ? "Create ACP Agent" : `Edit ${form.name || form.key}`}</b>
              <button className="drawer-close" onClick={() => setDrawerOpen(false)}>×</button>
            </div>
            <div className="drawer-body">
              <label className="field"><span>Key *</span><input value={form.key} disabled={!isCreate} onChange={(e) => setForm({ ...form, key: e.target.value })} placeholder="agent-key" /></label>
              <label className="field"><span>Name</span><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Agent Name" /></label>
              <label className="field"><span>Command *</span><input value={form.command} onChange={(e) => setForm({ ...form, command: e.target.value })} placeholder="opencode" /></label>
              <label className="field"><span>Args</span><input value={form.args} onChange={(e) => setForm({ ...form, args: e.target.value })} placeholder="--some --args" /></label>
              <label className="field"><span>Env (KEY=VALUE per line)</span><textarea value={form.env} onChange={(e) => setForm({ ...form, env: e.target.value })} rows={3} /></label>
              <label className="field checkbox"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /><span>Enabled</span></label>
              <label className="field checkbox"><input type="checkbox" checked={form.trusted} onChange={(e) => setForm({ ...form, trusted: e.target.checked })} /><span>Trusted</span></label>
              <label className="field"><span>Tool Parse Mode</span>
                <select value={form.tool_parse_mode} onChange={(e) => setForm({ ...form, tool_parse_mode: e.target.value })}>
                  <option value="call_title">call_title</option>
                  <option value="content">content</option>
                </select>
              </label>
              <label className="field"><span>Stdio Buffer Limit (bytes)</span><input type="number" value={form.stdio_buffer_limit_bytes} onChange={(e) => setForm({ ...form, stdio_buffer_limit_bytes: e.target.value })} /></label>
            </div>
            <div className="drawer-foot">
              <button onClick={() => setDrawerOpen(false)}>Cancel</button>
              <button className="primary" onClick={save}>Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Agents 管理面板
// ---------------------------------------------------------------------------
export function AgentsPanel() {
  const [db, setDb] = useState<any>({ agents: [] });
  const [form, setForm] = useState({ name: "", emoji: "🤖", system_prompt: "", default_model: "deepseek-chat", default_mode: "chat" });
  const load = () => apiFetch("/agents").then((r) => r.json()).then(setDb).catch(() => {});
  useEffect(() => {
    load();
  }, []);

  const add = async () => {
    await apiFetch("/agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    setForm({ name: "", emoji: "🤖", system_prompt: "", default_model: "deepseek-chat", default_mode: "chat" });
    load();
  };

  const remove = async (id: string) => {
    await apiFetch(`/agents/${encodeURIComponent(id)}`, { method: "DELETE" });
    load();
  };

  return (
    <div className="plugin-panel agents-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Settings</span>
          <span className="bc-sep">/</span>
          <b>Agent Management</b>
        </div>
      </div>
      <p className="panel-hint">创建并管理不同人设与默认配置的 Agent Profile。</p>
      <div className="form-stack">
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="名称" />
        <input value={form.emoji} onChange={(e) => setForm({ ...form, emoji: e.target.value })} placeholder="Emoji" />
        <input value={form.default_model} onChange={(e) => setForm({ ...form, default_model: e.target.value })} placeholder="默认模型" />
        <select value={form.default_mode} onChange={(e) => setForm({ ...form, default_mode: e.target.value })}>
          <option value="chat">chat</option>
          <option value="coding">coding</option>
          <option value="mission">mission</option>
        </select>
        <textarea value={form.system_prompt} onChange={(e) => setForm({ ...form, system_prompt: e.target.value })} placeholder="System Prompt" />
        <button onClick={add}>创建 Agent</button>
      </div>
      <div className="cards">
        {db.agents.map((a: any) => (
          <div className="card" key={a.id}>
            <div className="card-head"><b>{a.emoji} {a.name}</b></div>
            <div className="card-desc">{a.system_prompt}</div>
            <div className="card-meta">{a.default_model} · {a.default_mode}</div>
            {a.id !== "default" && <button className="danger" onClick={() => remove(a.id)}>删除</button>}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sessions 面板
// ---------------------------------------------------------------------------
export function SessionsPanel({ onSelect }: { onSelect: (id: string) => void }) {
  const [data, setData] = useState<any>({ sessions: [] });
  const load = () => apiFetch("/sessions").then((r) => r.json()).then(setData).catch(() => {});
  useEffect(() => {
    load();
  }, []);

  const del = async (id: string) => {
    await apiFetch(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    load();
  };

  const reset = async (id: string) => {
    if (!confirm(`重置会话 ${id}？\n\n将清除该线程的挂起审批、checkpoint（含未决 HITL 中断）与上下文历史，救活“卡死”的会话。此操作不可撤销。`)) return;
    try {
      const r = await apiFetch(`/threads/${encodeURIComponent(id)}/reset`, { method: "POST" });
      const d = await r.json();
      if (d.ok) {
        alert(`已重置 ${id}。`);
        load();
      } else {
        alert(`重置失败：${JSON.stringify(d)}`);
      }
    } catch (e: any) {
      alert(`重置失败：${e?.message || e}`);
    }
  };

  return (
    <div className="plugin-panel sessions-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Control</span>
          <span className="bc-sep">/</span>
          <b>Sessions</b>
        </div>
      </div>
      <p className="panel-hint">后端会话索引（同一 thread_id 的历史跨请求/跨模型可恢复）。会话卡死时可用「重置」救活，无需新建对话。</p>
      <div className="session-table">
        {data.sessions.map((s: any) => (
          <div className="session-row" key={s.id}>
            <div className="session-info">
              <b>{s.title || s.id}</b>
              <span className="muted">{formatDate(s.updated_at)}</span>
            </div>
            <div className="session-actions">
              <button onClick={() => onSelect(s.id)}>进入</button>
              <button className="warn" onClick={() => reset(s.id)}>重置</button>
              <button className="danger" onClick={() => del(s.id)}>删除</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cron Jobs 面板（对齐 QwenPaw：表格列表 + 右侧抽屉创建/编辑）
// ---------------------------------------------------------------------------

type ScheduleType = "hourly" | "daily" | "weekly" | "custom";
type TaskType = "agent" | "command";

interface CronForm {
  id?: string;
  name: string;
  enabled: boolean;
  scheduleType: ScheduleType;
  hour: number;
  minute: number;
  days: boolean[]; // 0=周一 … 6=周日
  customCron: string;
  timezone: string;
  taskType: TaskType;
  content: string;
}

const DAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
// 前端 index 0=周一 对应 cron 1；6=周日 对应 cron 0/7
const DAY_TO_CRON = [1, 2, 3, 4, 5, 6, 0];

const pad = (n: number) => String(n).padStart(2, "0");

const TIMEZONES = [
  { value: "Asia/Shanghai", label: "China Standard Time (+08:00, Asia/Shanghai)" },
  { value: "UTC", label: "UTC (+00:00)" },
  { value: "Asia/Tokyo", label: "Japan Standard Time (+09:00, Asia/Tokyo)" },
  { value: "America/New_York", label: "Eastern Time (America/New_York)" },
  { value: "Europe/London", label: "Greenwich Mean Time (Europe/London)" },
  { value: "Australia/Sydney", label: "Australian Eastern Time (Australia/Sydney)" },
];

const SCHEDULE_TYPE_LABELS: Record<ScheduleType | "scheduled", string> = {
  hourly: "Hourly",
  daily: "Daily",
  weekly: "Weekly",
  custom: "Custom",
  scheduled: "一次性",
};

const SCHEDULE_TYPE_LABELS_CN: Record<ScheduleType, string> = {
  hourly: "每小时",
  daily: "每天",
  weekly: "每周",
  custom: "自定义",
};

const TASK_TYPE_LABELS: Record<TaskType, string> = {
  agent: "agent",
  command: "command",
};

const STATUS_META: Record<string, { label: string; cls: string }> = {
  success: { label: "成功", cls: "ok" },
  failed: { label: "失败", cls: "bad" },
  error: { label: "错误", cls: "bad" },
  timeout: { label: "超时", cls: "warn" },
};

function scheduleText(cron: string): string {
  const s = (cron || "").trim();
  const parts = s.split(/\s+/);
  if (parts.length !== 5 && parts.length !== 6) return s || "—";
  const [minute, hour, dom, month, dow] = parts;
  if (hour === "*" && dom === "*" && month === "*" && dow === "*" && minute === "0") return "每小时整点";
  if (hour === "*" && dom === "*" && month === "*" && dow === "*") return `每小时第 ${minute} 分`;
  if (dom === "*" && month === "*" && dow === "*") return `每天 ${pad(+hour)}:${pad(+minute)}`;
  return s;
}

function parseDays(dow: string): number[] {
  const out: number[] = [];
  const map: Record<string, number> = { "0": 6, "7": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5 };
  for (const raw of dow.split(",")) {
    const r = raw.trim();
    if (!r) return [];
    if (r.includes("-")) {
      const [a, b] = r.split("-");
      if (!/^\d+$/.test(a) || !/^\d+$/.test(b)) return [];
      const ia = +a, ib = +b;
      if (!(String(ia) in map) || !(String(ib) in map)) return [];
      const sa = map[String(ia)], sb = map[String(ib)];
      if (sa > sb) return [];
      for (let d = sa; d <= sb; d++) if (!out.includes(d)) out.push(d);
      continue;
    }
    if (/^\d+$/.test(r)) {
      if (!(r in map)) return [];
      const d = map[r];
      if (!out.includes(d)) out.push(d);
      continue;
    }
    return [];
  }
  return out;
}

function serializeCron(f: CronForm): { schedule: string; scheduleType: ScheduleType } {
  switch (f.scheduleType) {
    case "hourly":
      return { schedule: "0 * * * *", scheduleType: "hourly" };
    case "daily":
      return { schedule: `${f.minute} ${f.hour} * * *`, scheduleType: "daily" };
    case "weekly": {
      const nums = f.days
        .map((on, i) => (on ? DAY_TO_CRON[i] : -1))
        .filter((n) => n >= 0)
        .sort((a, b) => a - b);
      const dow = nums.length ? nums.join(",") : "1";
      return { schedule: `${f.minute} ${f.hour} * * ${dow}`, scheduleType: "weekly" };
    }
    case "custom":
      return { schedule: f.customCron.trim(), scheduleType: "custom" };
  }
}

function defaultForm(): CronForm {
  return {
    name: "",
    enabled: true,
    scheduleType: "daily",
    hour: 9,
    minute: 0,
    days: [true, false, false, false, false, false, false],
    customCron: "0 9 * * *",
    timezone: "Asia/Shanghai",
    taskType: "command",
    content: "",
  };
}

function jobToForm(job: any): CronForm {
  const f = defaultForm();
  f.id = job.id;
  f.name = job.name || "";
  f.enabled = job.enabled !== false;
  f.timezone = job.timezone || "Asia/Shanghai";
  f.taskType = job.task_type || "command";
  f.content = (job.task_type === "agent" ? job.prompt : job.command) || "";

  const scheduleType = job.schedule_type || "custom";
  const cron = (job.schedule || "").trim();
  const parts = cron.split(/\s+/);

  if (["hourly", "daily", "weekly", "custom"].includes(scheduleType)) {
    f.scheduleType = scheduleType;
  }

  if (parts.length === 5) {
    const [m, h, dom, mon, dow] = parts;
    if (h !== "*") f.hour = +h;
    if (m !== "*") f.minute = +m;
    if (f.scheduleType === "weekly" && dow !== "*") {
      const sel = [false, false, false, false, false, false, false];
      parseDays(dow).forEach((d) => (sel[d] = true));
      f.days = sel;
    }
    if (f.scheduleType === "custom") {
      f.customCron = cron;
    }
  }
  return f;
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export function CronPanel() {
  const [db, setDb] = useState<any>({ jobs: [] });
  const [history, setHistory] = useState<any>({ runs: [] });
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [form, setForm] = useState<CronForm>(defaultForm());
  const [running, setRunning] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const load = () => apiFetch("/cron").then((r) => r.json()).then(setDb).catch(() => {});
  const loadHistory = () => apiFetch("/cron/history").then((r) => r.json()).then(setHistory).catch(() => {});
  useEffect(() => {
    load();
    loadHistory();
  }, []);

  const remove = async (id: string) => {
    if (!confirm("确定删除该定时任务？")) return;
    await apiFetch(`/cron/${encodeURIComponent(id)}`, { method: "DELETE" });
    load();
  };

  const toggle = async (id: string, enabled: boolean) => {
    await apiFetch(`/cron/${encodeURIComponent(id)}?enabled=${enabled}`, { method: "PATCH" });
    load();
  };

  const runNow = async (id: string) => {
    setRunning(id);
    try {
      await apiFetch(`/cron/${encodeURIComponent(id)}/run`, { method: "POST" });
      await loadHistory();
    } finally {
      setRunning(null);
    }
  };

  const openAdd = () => {
    setForm(defaultForm());
    setDrawerOpen(true);
  };
  const openEdit = (job: any) => {
    setForm(jobToForm(job));
    setDrawerOpen(true);
  };

  const cronExpr = serializeCron(form).schedule;
  const customInvalid = form.scheduleType === "custom" && !/(\S+\s+){4,5}\S+$/.test(form.customCron.trim());

  const save = async () => {
    if (!form.name.trim()) {
      alert("请填写 Job Name");
      return;
    }
    if (!form.content.trim()) {
      alert("请填写 Message content");
      return;
    }
    if (customInvalid) {
      alert("cron 表达式需为 5 或 6 段，如 0 9 * * 1-5");
      return;
    }
    const { schedule, scheduleType } = serializeCron(form);
    const content = form.content.trim();
    const payload: any = {
      id: form.id,
      name: form.name.trim(),
      schedule,
      enabled: form.enabled,
      task_type: form.taskType,
      timezone: form.timezone,
      schedule_type: scheduleType,
    };
    if (form.taskType === "agent") {
      payload.prompt = content;
      payload.command = "";
    } else {
      payload.command = content;
      payload.prompt = "";
    }
    await apiFetch("/cron", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setDrawerOpen(false);
    load();
  };

  const toggleExpanded = (id: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  return (
    <div className="plugin-panel cron-panel">
      <div className="cron-header">
        <div className="cron-breadcrumb">
          <span className="cron-bc-muted">Control</span>
          <span className="cron-bc-sep">/</span>
          <b>Cron Jobs</b>
        </div>
        <button className="cron-create-btn" onClick={openAdd}>+ Create Job</button>
      </div>
      <p className="panel-hint">基于 APScheduler 后台调度，按设定时间自动运行任务。</p>

      <div className="cron-table-wrap">
        <table className="cron-table">
          <thead>
            <tr>
              <th>Job ID</th>
              <th>Job Name</th>
              <th>Type</th>
              <th>Status</th>
              <th>Schedule Type</th>
              <th>Schedule (Cron)</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
              {db.jobs.length === 0 && (
                <tr>
                  <td colSpan={7}>
                  <div className="cron-empty">No data</div>
                </td>
              </tr>
            )}
            {db.jobs.map((j: any) => (
              <tr key={j.id}>
                <td className="cron-id">{j.id}</td>
                <td className="cron-name">{j.name}</td>
                <td>
                  <span className={"badge " + (j.task_type === "agent" ? "ok" : "off")}>
                    {j.task_type === "agent" ? "Agent" : "Command"}
                  </span>
                </td>
                <td>
                  <span className={"badge " + (j.enabled ? "ok" : "off")}>
                    {j.enabled ? "Enabled" : "Disabled"}
                  </span>
                </td>
                <td>{SCHEDULE_TYPE_LABELS[j.schedule_type] || j.schedule_type || "—"}</td>
                <td>
                  <div className="cron-schedule-cell">
                    <span>
                      {j.schedule_type === "scheduled" && j.run_at
                        ? `一次性 · ${fmtTime(j.run_at)}`
                        : (j.schedule_text || j.schedule || "—")}
                    </span>
                    {j.schedule_type !== "scheduled" && j.enabled && j.next_run_at && (
                      <span className="cron-next">Next: {fmtTime(j.next_run_at)}</span>
                    )}
                  </div>
                </td>
                <td>
                  <div className="cron-row-actions">
                    {j.schedule_type !== "scheduled" && <button onClick={() => openEdit(j)}>编辑</button>}
                    {j.schedule_type !== "scheduled" && (
                      <button onClick={() => toggle(j.id, !j.enabled)}>
                        {j.enabled ? "禁用" : "启用"}
                      </button>
                    )}
                    <button onClick={() => runNow(j.id)} disabled={running === j.id}>
                      {running === j.id ? "执行中…" : "执行"}
                    </button>
                    <button className="danger" onClick={() => remove(j.id)}>删除</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {history.runs.length > 0 && (
        <div className="cron-history">
          <h3>Recent Execution History</h3>
          <div className="cron-history-list">
            {history.runs.slice(0, 12).map((r: any) => {
              const meta = STATUS_META[r.status] || { label: r.status, cls: "off" };
              const open = expanded.has(r.id);
              return (
                <div className={"cron-hist-row " + meta.cls} key={r.id}>
                  <div className="cron-hist-head" onClick={() => toggleExpanded(r.id)}>
                    <span className={"badge " + meta.cls}>{meta.label}</span>
                    <b>{r.job_name}</b>
                    <span className="cron-hist-cmd">{r.command}</span>
                    <span className="cron-hist-time">{fmtTime(r.started_at)}</span>
                    <span className="cron-hist-caret">{open ? "▾" : "▸"}</span>
                  </div>
                  {open && (
                    <div className="cron-hist-detail">
                      {r.error && <div className="card-error">{r.error}</div>}
                      {r.output && <pre className="card-output">{r.output}</pre>}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {drawerOpen && (
        <div className="cron-drawer-backdrop" onClick={() => setDrawerOpen(false)}>
          <div className="cron-drawer" onClick={(e) => e.stopPropagation()}>
            <div className="cron-drawer-head">
              <div className="cron-drawer-title">{form.id ? "Edit Job" : "Create Job"}</div>
              <button className="cron-drawer-close" onClick={() => setDrawerOpen(false)}>×</button>
            </div>

            <div className="cron-drawer-body">
              <label className="cron-field">
                <span>Job Name <span className="cron-required">*</span></span>
                <input
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="e.g., Daily Morning Report"
                />
              </label>

              <label className="cron-field row-check">
                <span>Status</span>
                <span className="cron-toggle">
                  <input
                    type="checkbox"
                    checked={form.enabled}
                    onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                  />
                  <span className="cron-toggle-slider" />
                </span>
              </label>

              <label className="cron-field">
                <span>Schedule (Cron) <span className="cron-required">*</span></span>
                <select
                  value={form.scheduleType}
                  onChange={(e) => setForm({ ...form, scheduleType: e.target.value as ScheduleType })}
                >
                  <option value="hourly">Hourly</option>
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                  <option value="custom">Custom</option>
                </select>
              </label>

              {(form.scheduleType === "daily" || form.scheduleType === "weekly") && (
                <label className="cron-field">
                  <span>Time <span className="cron-required">*</span></span>
                  <input
                    type="time"
                    value={`${pad(form.hour)}:${pad(form.minute)}`}
                    onChange={(e) => {
                      const [h, m] = e.target.value.split(":").map(Number);
                      setForm({ ...form, hour: h || 0, minute: m || 0 });
                    }}
                  />
                </label>
              )}

              {form.scheduleType === "weekly" && (
                <div className="cron-field">
                  <span>Weekday</span>
                  <div className="cron-days">
                    {DAY_NAMES.map((d, i) => (
                      <button
                        key={d}
                        className={"cron-day " + (form.days[i] ? "on" : "")}
                        onClick={() => {
                          const days = [...form.days];
                          days[i] = !days[i];
                          setForm({ ...form, days });
                        }}
                      >
                        {d}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <label className="cron-field">
                <span>Timezone</span>
                <select
                  value={form.timezone}
                  onChange={(e) => setForm({ ...form, timezone: e.target.value })}
                >
                  {TIMEZONES.map((tz) => (
                    <option key={tz.value} value={tz.value}>{tz.label}</option>
                  ))}
                </select>
              </label>

              <label className="cron-field">
                <span>Task Type <span className="cron-required">*</span></span>
                <select
                  value={form.taskType}
                  onChange={(e) => setForm({ ...form, taskType: e.target.value as TaskType })}
                >
                  <option value="agent">agent</option>
                  <option value="command">command</option>
                </select>
              </label>

              <label className="cron-field">
                <span>Message content</span>
                <textarea
                  value={form.content}
                  onChange={(e) => setForm({ ...form, content: e.target.value })}
                  rows={4}
                  placeholder={
                    form.taskType === "agent"
                      ? "For simple text tasks, enter the message body to send..."
                      : "Enter the shell command to execute in the workspace directory..."
                  }
                />
              </label>

              {form.scheduleType === "custom" && (
                <label className="cron-field">
                  <span>Cron Expression</span>
                  <input
                    value={form.customCron}
                    onChange={(e) => setForm({ ...form, customCron: e.target.value })}
                    placeholder="0 9 * * 1-5"
                    className={customInvalid ? "cron-input-bad" : ""}
                  />
                  <span className="cron-help">Format: minute hour day month weekday (5 fields). <a href="https://crontab.guru/" target="_blank" rel="noreferrer">crontab.guru ↗</a></span>
                </label>
              )}

              <div className="cron-preview">
                📌 Will run <b>{scheduleText(cronExpr)}</b> in <b>{form.timezone}</b>：<code>{form.content || "（未填写）"}</code>
              </div>
            </div>

            <div className="cron-drawer-footer">
              <button className="ghost" onClick={() => setDrawerOpen(false)}>Cancel</button>
              <button className="primary" onClick={save}>Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Heartbeat 面板
// ---------------------------------------------------------------------------
export function HeartbeatPanel() {
  const [hb, setHb] = useState<any>(null);
  useEffect(() => {
    const load = () => apiFetch("/heartbeat").then((r) => r.json()).then(setHb).catch(() => {});
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="plugin-panel heartbeat-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Control</span>
          <span className="bc-sep">/</span>
          <b>Heartbeat</b>
        </div>
      </div>
      <p className="panel-hint">后端健康状态与资源占用。</p>
      {hb && (
        <div className="stats-grid">
          <div className="stat"><span>状态</span><b>{hb.status}</b></div>
          <div className="stat"><span>时间</span><b>{hb.time}</b></div>
          <div className="stat"><span>运行</span><b>{hb.uptime_seconds}s</b></div>
          <div className="stat"><span>RSS</span><b>{hb.memory.rss_mb} MB</b></div>
          <div className="stat"><span>VMS</span><b>{hb.memory.vms_mb} MB</b></div>
          <div className="stat"><span>Workspace</span><b>{hb.workspace}</b></div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Agent Statistics 面板
// ---------------------------------------------------------------------------
export function StatsPanel({ metrics }: { metrics: any }) {
  return (
    <div className="plugin-panel stats-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Settings</span>
          <span className="bc-sep">/</span>
          <b>Agent Statistics</b>
        </div>
      </div>
      <p className="panel-hint">实时可观测指标（与侧边栏 mini 指标同源）。</p>
      {metrics ? (
        <div className="stats-grid">
          <div className="stat"><span>首字节</span><b>{metrics.first_byte_ms || 0} ms</b></div>
          <div className="stat"><span>首思考</span><b>{metrics.first_reasoning_ms || 0} ms</b></div>
          <div className="stat"><span>首答案</span><b>{metrics.first_answer_ms || 0} ms</b></div>
          <div className="stat"><span>生成耗时</span><b>{metrics.generation_ms || 0} ms</b></div>
          <div className="stat"><span>轮次</span><b>{metrics.turns}</b></div>
          <div className="stat"><span>工具调用</span><b>{metrics.tool_calls}</b></div>
          <div className="stat"><span>拒绝</span><b>{metrics.tool_denials}</b></div>
          <div className="stat"><span>折叠</span><b>{metrics.folds || 0}</b></div>
          <div className="stat"><span>召回</span><b>{metrics.recalls || 0}</b></div>
          <div className="stat"><span>剪枝</span><b>{metrics.tool_pruned || 0}</b></div>
          <div className="stat"><span>媒体剥离</span><b>{metrics.media_stripped || 0}</b></div>
          <div className="stat"><span>错误</span><b>{metrics.errors || 0}</b></div>
        </div>
      ) : (
        <div className="muted">加载中…</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Configuration 面板（原设置弹窗升级为常驻面板）
// ---------------------------------------------------------------------------
export function ConfigPanel({
  form,
  setForm,
  runtimeConfig,
  setRuntime,
  setRunning,
  setScalar,
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
  onSave,
}: any) {
  const [tab, setTab] = useState("react");
  const tabs = [
    { id: "llm", label: "LLM" },
    { id: "react", label: "ReAct Agent" },
    { id: "retry", label: "LLM Auto Retry" },
    { id: "ratelimit", label: "LLM Rate Limiter" },
    { id: "context", label: "Context Management" },
    { id: "memory", label: "Long-term Memory" },
    { id: "security", label: "Tool Execution Security" },
  ];

  const rc = runtimeConfig?.running || {};
  const loop = rc.loop || {};
  const it = loop.iteration || {};
  const dl = loop.doom_loop || {};
  const rb = loop.rubric || {};
  const lctx = rc.light_context_config || {};
  const reme = rc.reme_light_memory_config || {};
  const rl = runtimeConfig?.rate_limiter || {};
  const plan = runtimeConfig?.plan || {};

  return (
    <div className="plugin-panel config-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Workspace</span>
          <span className="bc-sep">/</span>
          <b>Configuration</b>
        </div>
      </div>
      <p className="panel-hint">对齐 QwenPaw Workspace/Configuration：分 Tab 管理代理、重试、限流、上下文、记忆与安全策略。保存后即时生效。</p>
      {msg && <div className="panel-msg">{msg}</div>}

      <div className="config-tabs">
        {tabs.map((t) => (
          <button
            key={t.id}
            className={tab === t.id ? "active" : ""}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="config-tab-content">
        {/* ---------------- LLM ---------------- */}
        {tab === "llm" && (
          <>
            <label className="field">
              <span>模型厂商</span>
              <select value={form.provider} onChange={(e) => onProviderChange(e.target.value)}>
                <option value="">— 自定义 / 手动填 Base URL —</option>
                {providers.map((p: any) => (
                  <option key={p.id} value={p.id}>
                    {p.label} {p.reasoning ? "· 支持思考" : ""}
                  </option>
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
                {discoveredModels.map((m: string) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label className="field checkbox">
              <input type="checkbox" checked={form.reasoning} onChange={(e) => onToggleReasoning(e.target.checked)} />
              <span>思考模式（reasoning）</span>
            </label>
            <label className="field checkbox">
              <input type="checkbox" checked={form.enableAuth} onChange={(e) => setForm("enableAuth", e.target.checked)} />
              <span>启用访问鉴权</span>
            </label>
            {form.enableAuth && (
              <label className="field">
                <span>服务令牌</span>
                <input type="password" value={form.serviceKey} onChange={(e) => setForm("serviceKey", e.target.value)} placeholder="前端将保存到 localStorage" />
              </label>
            )}
          </>
        )}

        {/* ---------------- ReAct Agent ---------------- */}
        {tab === "react" && (
          <>
            <label className="field">
              <span>Agent Language</span>
              <select value={runtimeConfig?.language || "zh"} onChange={(e) => setScalar("language", e.target.value)}>
                <option value="zh">中文</option>
                <option value="en">English</option>
              </select>
            </label>
            <label className="field">
              <span>Max Iterations (loop.iteration)</span>
              <input type="number" min={1} max={1000} value={it.max_iterations ?? ""} placeholder="留空=用 max_iters(100)" onChange={(e) => setRunning(["loop", "iteration", "max_iterations"], e.target.value === "" ? null : parseInt(e.target.value || "0", 10))} />
            </label>
            <label className="field">
              <span>Max Iters (兜底上限)</span>
              <input type="number" min={1} max={1000} value={rc.max_iters ?? 100} onChange={(e) => setRunning(["max_iters"], parseInt(e.target.value || "0", 10))} />
            </label>
            <label className="field">
              <span>Shell Command Timeout (s)</span>
              <input type="number" min={1} max={3600} value={rc.shell_command_timeout ?? 60} onChange={(e) => setRunning(["shell_command_timeout"], parseInt(e.target.value || "0", 10))} />
            </label>
            <label className="field">
              <span>Max Context Length (tokens)</span>
              <input type="number" min={1024} step={1024} value={rc.max_input_length ?? 131072} onChange={(e) => setRunning(["max_input_length"], parseInt(e.target.value || "0", 10))} />
            </label>
            <label className="field">
              <span>Context Manager Backend</span>
              <select value={rc.context_manager_backend || "light"} onChange={(e) => setRunning(["context_manager_backend"], e.target.value)}>
                <option value="light">light</option>
                <option value="scroll">scroll</option>
              </select>
            </label>
            <label className="field">
              <span>Memory Manager Backend</span>
              <select value={rc.memory_manager_backend || "remelight"} onChange={(e) => setRunning(["memory_manager_backend"], e.target.value)}>
                <option value="remelight">remelight</option>
                <option value="none">none</option>
              </select>
            </label>
            <label className="field checkbox">
              <input type="checkbox" checked={!!plan.enabled} onChange={(e) => setRuntime("plan", "enabled", e.target.checked)} />
              <span>Plan Mode</span>
            </label>
            <label className="field checkbox">
              <input type="checkbox" checked={!!rb.enabled} onChange={(e) => setRunning(["loop", "rubric", "enabled"], e.target.checked)} />
              <span>续跑门 Rubric（防止纯文本早停）</span>
            </label>
            <label className="field checkbox">
              <input type="checkbox" checked={!!dl.enabled} onChange={(e) => setRunning(["loop", "doom_loop", "enabled"], e.target.checked)} />
              <span>Doom Loop 防护（鬼打墙检测）</span>
            </label>
          </>
        )}

        {/* ---------------- LLM Auto Retry ---------------- */}
        {tab === "retry" && (
          <>
            <label className="field checkbox">
              <input type="checkbox" checked={!!rc.llm_retry_enabled} onChange={(e) => setRunning(["llm_retry_enabled"], e.target.checked)} />
              <span>启用 LLM 自动重试</span>
            </label>
            <label className="field">
              <span>Max Retries</span>
              <input type="number" min={0} max={10} value={rc.llm_max_retries ?? 3} onChange={(e) => setRunning(["llm_max_retries"], parseInt(e.target.value || "0", 10))} />
            </label>
            <label className="field">
              <span>Backoff Base (seconds)</span>
              <input type="number" min={0} step={0.1} value={rc.llm_backoff_base ?? 1} onChange={(e) => setRunning(["llm_backoff_base"], parseFloat(e.target.value || "0"))} />
            </label>
          </>
        )}

        {/* ---------------- LLM Rate Limiter ---------------- */}
        {tab === "ratelimit" && (
          <>
            <label className="field checkbox">
              <input type="checkbox" checked={!!rl.enabled} onChange={(e) => setRuntime("rate_limiter", "enabled", e.target.checked)} />
              <span>启用请求限流</span>
            </label>
            <label className="field">
              <span>Requests Per Minute</span>
              <input type="number" min={1} max={10000} value={rl.requests_per_minute ?? 60} onChange={(e) => setRuntime("rate_limiter", "requests_per_minute", parseInt(e.target.value || "0", 10))} />
            </label>
          </>
        )}

        {/* ---------------- Context Management ---------------- */}
        {tab === "context" && (
          <>
            <label className="field">
              <span>Context Manager Backend</span>
              <select value={rc.context_manager_backend || "light"} onChange={(e) => setRunning(["context_manager_backend"], e.target.value)}>
                <option value="light">light</option>
                <option value="scroll">scroll</option>
              </select>
            </label>
            <label className="field checkbox">
              <input type="checkbox" checked={!!lctx.context_compact_config?.enabled} onChange={(e) => setRunning(["light_context_config", "context_compact_config", "enabled"], e.target.checked)} />
              <span>自动压缩上下文 (context_compact)</span>
            </label>
            <label className="field">
              <span>Compact Threshold Ratio</span>
              <input type="number" min={0.1} max={0.9} step={0.05} value={lctx.context_compact_config?.compact_threshold_ratio ?? 0.8} onChange={(e) => setRunning(["light_context_config", "context_compact_config", "compact_threshold_ratio"], parseFloat(e.target.value || "0"))} />
            </label>
            <label className="field checkbox">
              <input type="checkbox" checked={!!lctx.tool_result_pruning_config?.enabled} onChange={(e) => setRunning(["light_context_config", "tool_result_pruning_config", "enabled"], e.target.checked)} />
              <span>工具结果裁剪 (tool_result_pruning)</span>
            </label>
          </>
        )}

        {/* ---------------- Long-term Memory ---------------- */}
        {tab === "memory" && (
          <>
            <label className="field">
              <span>Memory Manager Backend</span>
              <select value={rc.memory_manager_backend || "remelight"} onChange={(e) => setRunning(["memory_manager_backend"], e.target.value)}>
                <option value="remelight">remelight</option>
                <option value="none">none</option>
              </select>
            </label>
            <label className="field">
              <span>Dream Cron (记忆优化定时)</span>
              <input value={reme.dream_cron || "0 23 * * *"} placeholder="0 23 * * *" onChange={(e) => setRunning(["reme_light_memory_config", "dream_cron"], e.target.value)} />
            </label>
            <label className="field">
              <span>Auto Memory Interval (每 N 轮)</span>
              <input type="number" min={0} max={50} value={reme.auto_memory_interval ?? 5} onChange={(e) => setRunning(["reme_light_memory_config", "auto_memory_interval"], parseInt(e.target.value || "0", 10))} />
            </label>
            <label className="field">
              <span>Daily Memory Dir</span>
              <input value={rc.daily_memory_dir || "memory"} onChange={(e) => setRunning(["daily_memory_dir"], e.target.value)} />
            </label>
          </>
        )}

        {/* ---------------- Tool Execution Security ---------------- */}
        {tab === "security" && (
          <>
            <label className="field">
              <span>Approval Level (审批级别)</span>
              <select value={runtimeConfig?.approval_level || "AUTO"} onChange={(e) => setScalar("approval_level", e.target.value)}>
                <option value="OFF">OFF — 全部自动放行（等同信任模式）</option>
                <option value="SMART">SMART — 只读/常见命令自动放行</option>
                <option value="AUTO">AUTO — 仅受保护工具需审批</option>
                <option value="STRICT">STRICT — 全部工具人工审批</option>
              </select>
            </label>
            <label className="field">
              <span>Approval Timeout (seconds)</span>
              <input type="number" min={10} max={3600} value={rc.approval_timeout_seconds ?? 300} onChange={(e) => setRunning(["approval_timeout_seconds"], parseInt(e.target.value || "0", 10))} />
            </label>
            <p className="panel-hint">
              QwenPaw 真实机制：以 <code>approval_level</code> 单一字段表达工具执行安全策略
              （STRICT/SMART/AUTO/OFF），由后端 ApprovalGate 统一解释，不再使用分散的 trust_mode / auto_approve 开关。
            </p>
          </>
        )}
      </div>

      <button className="save-config" onClick={onSave}>保存配置</button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Channels 占位面板
// ---------------------------------------------------------------------------
export function ChannelsPanel() {
  return (
    <div className="plugin-panel channels-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Control</span>
          <span className="bc-sep">/</span>
          <b>Channels</b>
        </div>
      </div>
      <p className="panel-hint">多 Agent 通信通道（预留）。可后续接入 WebSocket / 多 Agent 消息总线。</p>
      <div className="muted">暂无已配置 Channel。</div>
    </div>
  );
}
