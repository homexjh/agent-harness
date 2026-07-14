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
    backend: "remelight",
    reme_light_memory_config: {
      summarize_when_compact: true,
      auto_memory_interval: 10,
      dream_cron: "",
      rebuild_memory_index_on_start: false,
      enable_search_raw_log: false,
      auto_memory_search_config: { enabled: true, max_results: 5 },
      embedding_model_config: {
        backend: "openai",
        base_url: "",
        api_key: "",
        model_name: "",
        dimensions: 1024,
        enable_cache: true,
        max_cache_size: 3000,
        max_input_length: 8192,
        max_batch_size: 10,
      },
    },
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

function TextField({
  label,
  value,
  onChange,
  placeholder,
  password,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  password?: boolean;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <input
        type={password ? "password" : "text"}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

export function MemoryPanel() {
  const [cfg, setCfg] = useState<any>(defaultConfig());
  const [stats, setStats] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const lc = cfg.reme_light_memory_config;
  const emb = lc.embedding_model_config;
  const ams = lc.auto_memory_search_config;

  const update = useCallback((path: string, value: any) => {
    setCfg((prev: any) => {
      const next = JSON.parse(JSON.stringify(prev));
      const keys = path.split(".");
      let o = next;
      for (let i = 0; i < keys.length - 1; i++) o = o[keys[i]];
      o[keys[keys.length - 1]] = value;
      return next;
    });
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const c = await (await apiFetch("/memory/config")).json();
      if (c && Object.keys(c).length) setCfg(c);
    } catch (e) {
      /* 用默认配置 */
    }
    try {
      const s = await (await apiFetch("/memory/stats")).json();
      setStats(s);
    } catch (e) {
      setStats(null);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      await apiFetch("/memory/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: cfg }),
      });
      setMsg("配置已保存并重新调度 dream cron");
      load();
    } catch (e: any) {
      setMsg("保存失败：" + (e?.message || e));
    }
    setSaving(false);
  };

  const run = async (action: string, fn: () => Promise<any>, ok: string) => {
    setBusy(action);
    setMsg(null);
    try {
      const r = await fn();
      setMsg(ok + (r && r.dreamed_at ? `（${r.dreamed_at}）` : ""));
      load();
    } catch (e: any) {
      setMsg("操作失败：" + (e?.message || e));
    }
    setBusy(null);
  };

  const doSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const r = await (
        await apiFetch("/memory/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: searchQuery, max_results: ams.max_results }),
        })
      ).json();
      setSearchResults(r.results || []);
    } catch (e: any) {
      setSearchResults([]);
      setMsg("检索失败：" + (e?.message || e));
    }
    setSearching(false);
  };

  const disabled = cfg.backend === "none";

  return (
    <div className="memory-panel">
      <div className="page-header">
        <div className="page-breadcrumb">
          <span>Workspace</span>
          <span className="bc-sep">/</span>
          <b>Memory</b>
        </div>
        <div className="page-actions">
          {msg && <span className="panel-msg">{msg}</span>}
          <button className="tool-foot-btn active" onClick={save} disabled={saving}>
            {saving ? "保存中…" : "保存配置"}
          </button>
        </div>
      </div>

      {loading && <div className="panel-hint">加载中…</div>}

      {!loading && disabled && (
        <div className="card" style={{ borderColor: "var(--accent)" }}>
          <div className="card-head">Memory 已禁用</div>
          <div className="card-desc">
            backend = none。把下方 Backend 切回 <b>remelight</b> 即可启用长期语义记忆。
          </div>
        </div>
      )}

      {/* 主配置 */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">Memory Manager</div>
        <div className="card-desc">
          长期语义记忆（对齐 QwenPaw ReMeLight）：向量 + BM25 混合检索、自动记忆、定时 dream。
        </div>
        <label className="field">
          <span>Backend</span>
          <select value={cfg.backend} onChange={(e) => update("backend", e.target.value)}>
            <option value="remelight">remelight</option>
            <option value="none">none (disabled)</option>
          </select>
        </label>

        <Toggle
          label="Summarize when compact"
          help="上下文压缩时把摘要写入记忆"
          checked={lc.summarize_when_compact}
          onChange={(v) => update("reme_light_memory_config.summarize_when_compact", v)}
        />
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 200 }}>
            <NumberField
              label="Auto memory interval（轮）"
              value={lc.auto_memory_interval}
              onChange={(v) => update("reme_light_memory_config.auto_memory_interval", v)}
            />
          </div>
          <div style={{ flex: 1, minWidth: 200 }}>
            <TextField
              label="Dream cron"
              value={lc.dream_cron}
              placeholder="0 3 * * * （留空不调度）"
              onChange={(v) => update("reme_light_memory_config.dream_cron", v)}
            />
          </div>
        </div>
        <Toggle
          label="Rebuild memory index on start"
          checked={lc.rebuild_memory_index_on_start}
          onChange={(v) => update("reme_light_memory_config.rebuild_memory_index_on_start", v)}
        />
        <Toggle
          label="Enable search raw log"
          help="把原始对话日志纳入检索范围"
          checked={lc.enable_search_raw_log}
          onChange={(v) => update("reme_light_memory_config.enable_search_raw_log", v)}
        />
      </div>

      {/* Auto Memory Search */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">Auto Memory Search</div>
        <div className="card-desc">
          回复前自动用最新用户消息做混合检索，命中则注入系统提示（对齐 QwenPaw auto_memory_search）。
        </div>
        <Toggle
          label="Enabled"
          checked={ams.enabled}
          onChange={(v) =>
            update("reme_light_memory_config.auto_memory_search_config.enabled", v)
          }
        />
        <NumberField
          label="Max results"
          value={ams.max_results}
          onChange={(v) =>
            update("reme_light_memory_config.auto_memory_search_config.max_results", v)
          }
        />
      </div>

      {/* Embedding Model */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">Embedding Model</div>
        <div className="card-desc">
          向量化后端（OpenAI 兼容）。留空 / 无 key 时自动降级为纯 BM25 检索。
        </div>
        <label className="field">
          <span>Backend</span>
          <select
            value={emb.backend}
            onChange={(e) =>
              update("reme_light_memory_config.embedding_model_config.backend", e.target.value)
            }
          >
            <option value="openai">OpenAI</option>
            <option value="dashscope">DashScope</option>
            <option value="dashscope_multimodal">DashScope Multimodal</option>
            <option value="gemini">Gemini</option>
            <option value="ollama">Ollama</option>
          </select>
        </label>
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 200 }}>
            <TextField
              label="Base URL"
              value={emb.base_url}
              placeholder="https://api.openai.com/v1"
              onChange={(v) =>
                update("reme_light_memory_config.embedding_model_config.base_url", v)
              }
            />
          </div>
          <div style={{ flex: 1, minWidth: 200 }}>
            <TextField
              label="Model name"
              value={emb.model_name}
              placeholder="text-embedding-3-small"
              onChange={(v) =>
                update("reme_light_memory_config.embedding_model_config.model_name", v)
              }
            />
          </div>
        </div>
        <TextField
          label="API Key"
          password
          value={emb.api_key}
          placeholder="sk-…（ollama 可留空）"
          onChange={(v) => update("reme_light_memory_config.embedding_model_config.api_key", v)}
        />
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 150 }}>
            <NumberField
              label="Dimensions"
              value={emb.dimensions}
              onChange={(v) =>
                update("reme_light_memory_config.embedding_model_config.dimensions", v)
              }
            />
          </div>
          <div style={{ flex: 1, minWidth: 150 }}>
            <NumberField
              label="Max cache size"
              value={emb.max_cache_size}
              onChange={(v) =>
                update("reme_light_memory_config.embedding_model_config.max_cache_size", v)
              }
            />
          </div>
          <div style={{ flex: 1, minWidth: 150 }}>
            <NumberField
              label="Max input length"
              value={emb.max_input_length}
              onChange={(v) =>
                update("reme_light_memory_config.embedding_model_config.max_input_length", v)
              }
            />
          </div>
          <div style={{ flex: 1, minWidth: 150 }}>
            <NumberField
              label="Max batch size"
              value={emb.max_batch_size}
              onChange={(v) =>
                update("reme_light_memory_config.embedding_model_config.max_batch_size", v)
              }
            />
          </div>
        </div>
        <Toggle
          label="Enable cache"
          checked={emb.enable_cache}
          onChange={(v) =>
            update("reme_light_memory_config.embedding_model_config.enable_cache", v)
          }
        />
      </div>

      {/* Stats + actions */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">Vault Stats</div>
        {stats && (
          <div className="stats-grid">
            <div className="stat">
              <span>Enabled</span>
              <b style={{ color: stats.enabled ? "var(--accent)" : "#999" }}>
                {stats.enabled ? "yes" : "no"}
              </b>
            </div>
            <div className="stat">
              <span>Notes</span>
              <b>{stats.notes ?? 0}</b>
            </div>
            <div className="stat">
              <span>Index chunks</span>
              <b>{stats.chunks ?? 0}</b>
            </div>
            <div className="stat">
              <span>Embedding</span>
              <b style={{ color: stats.embedding_enabled ? "var(--accent)" : "#999" }}>
                {stats.embedding_enabled ? "on" : "off"}
              </b>
            </div>
            <div className="stat">
              <span>Last dream</span>
              <b style={{ fontSize: 11 }}>{stats.last_dream ? stats.last_dream.slice(0, 19) : "—"}</b>
            </div>
          </div>
        )}
        <div className="card-actions" style={{ marginTop: 12, display: "flex", gap: 10 }}>
          <button
            className="tool-foot-btn"
            disabled={busy === "reindex" || disabled}
            onClick={() => run("reindex", () => apiFetch("/memory/reindex", { method: "POST" }).then((r) => r.json()), "索引已重建：")}
          >
            {busy === "reindex" ? "重建中…" : "重建索引 (Reindex)"}
          </button>
          <button
            className="tool-foot-btn"
            disabled={busy === "dream" || disabled}
            onClick={() => run("dream", () => apiFetch("/memory/dream", { method: "POST" }).then((r) => r.json()), "Dream 完成：")}
          >
            {busy === "dream" ? "Dreaming…" : "触发 Dream"}
          </button>
        </div>
      </div>

      {/* Test search */}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">Test Memory Search</div>
        <div className="card-desc">直接对记忆 vault 做混合检索（向量 + BM25 RRF）。</div>
        <div className="form-row">
          <input
            value={searchQuery}
            placeholder="输入检索关键词…"
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doSearch()}
          />
          <button className="tool-foot-btn active" onClick={doSearch} disabled={searching || disabled}>
            {searching ? "检索中…" : "搜索"}
          </button>
        </div>
        {searchResults.length > 0 && (
          <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
            {searchResults.map((r, i) => (
              <div key={i} className="card" style={{ padding: "10px 12px", margin: 0 }}>
                <div className="card-meta">
                  [{i + 1}] {r.path} · score={r.score}
                </div>
                <div style={{ fontSize: 13, marginTop: 4 }}>{r.text}</div>
              </div>
            ))}
          </div>
        )}
        {searchResults.length === 0 && !searching && searchQuery && (
          <div className="panel-hint">无结果（可先写入每日笔记或重建索引）。</div>
        )}
      </div>
    </div>
  );
}
