import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../api";

interface Revision {
  revision_number: number;
  change_source: string;
  changed_at?: string;
  timestamp?: string;
  changed_by?: string;
  user_id?: string;
}

type Action = "register" | "publish" | "archive" | "unpublish";

// Client-side mirror of the backend state machine so we only offer valid
// transitions (avoids confusing INVALID_STATE_TRANSITION errors on stage).
const NEXT: Record<string, { action: Action; label: string; primary?: boolean }[]> = {
  draft: [{ action: "register", label: "Register" }],
  registered: [{ action: "publish", label: "Publish", primary: true }],
  published: [
    { action: "unpublish", label: "Unpublish (recall)" },
    { action: "archive", label: "Archive" },
  ],
  archived: [{ action: "register", label: "Re-register" }],
};

export default function AssetDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [asset, setAsset] = useState<any | null>(null);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [desc, setDesc] = useState("");
  const [changeSource, setChangeSource] = useState<"manual" | "agent">("manual");
  const [validateResult, setValidateResult] = useState<any | null>(null);

  async function loadAsset() {
    if (!id) return;
    const a = await api.getAsset(id);
    if (!a) { setError("Asset not found."); return; }
    setAsset(a);
    setDesc(a.description || "");
  }

  async function loadRevisions() {
    if (!id) return;
    try {
      const rev = await api.revisions("data_asset", id);
      setRevisions(rev.revisions || []);
    } catch {
      /* RLS may hide; non-fatal */
    }
  }

  useEffect(() => {
    (async () => {
      try { await loadAsset(); await loadRevisions(); }
      catch (e: any) { setError(e?.body?.message || e.message || "load failed"); }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  async function lifecycle(action: Action) {
    if (!id) return;
    setWorking(true); setError(null); setNotice(null);
    try {
      const r = await api.lifecycleAction(id, action);
      setAsset((a: any) => ({ ...a, lifecycle_state: r.to_state }));
      setNotice(`Lifecycle: ${r.from_state} → ${r.to_state}`);
      await loadRevisions();
    } catch (e: any) {
      setError(e?.body?.message || e.message || "lifecycle action failed");
    } finally { setWorking(false); }
  }

  async function saveEdit() {
    if (!id) return;
    setWorking(true); setError(null); setNotice(null);
    try {
      const r = await api.updateAsset(id, { description: desc }, changeSource);
      setNotice(
        `Saved — version ${r.version ?? "bumped"}. A new revision was recorded (change_source “${changeSource}”).`
      );
      await loadAsset();
      await loadRevisions();
    } catch (e: any) {
      setError(e?.body?.message || e.message || "save failed");
    } finally { setWorking(false); }
  }

  async function validate() {
    if (!asset) return;
    setWorking(true); setValidateResult(null);
    try {
      const r = await api.validate("data_asset", {
        name: asset.name, storage_uri: asset.storage_uri, data_type: asset.data_type,
      });
      setValidateResult(r);
    } catch (e: any) {
      setError(e.message || "validate failed");
    } finally { setWorking(false); }
  }

  if (error && !asset) return (
    <div>
      <button onClick={() => navigate(-1)}>← Back</button>
      <div className="warning-banner" style={{ marginTop: 12 }}>{error}</div>
    </div>
  );
  if (!asset) return <div>Loading…</div>;

  const nextActions = NEXT[asset.lifecycle_state] || [];

  return (
    <div>
      <button onClick={() => navigate(-1)}>← Back</button>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>{asset.name || asset.id}</h3>
        <div className="meta">
          {asset.data_type} · <strong>{asset.lifecycle_state}</strong> · {asset.validation_status}
          {typeof asset.version !== "undefined" && <> · <strong>v{asset.version}</strong></>}
          {asset.is_sensitive && <span style={{ color: "#E7157B" }}> · sensitive</span>}
        </div>
        {asset.storage_uri && (
          <p className="meta" style={{ marginTop: 6 }}>
            <strong>Storage URI:</strong> <code>{asset.storage_uri}</code>
          </p>
        )}
        <p className="meta"><strong>ID:</strong> <code>{asset.id}</code></p>
      </div>

      {notice && <div className="card" style={{ background: "#eafaf1", borderColor: "#abebc6" }}>{notice}</div>}
      {error && <div className="warning-banner">{error}</div>}

      <div className="card">
        <h3>Lifecycle</h3>
        <p className="meta" style={{ marginBottom: 8 }}>
          Current state: <strong>{asset.lifecycle_state}</strong>. Available transitions:
        </p>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {nextActions.length === 0 && <span className="meta">No transitions from this state.</span>}
          {nextActions.map((a) => (
            <button
              key={a.action}
              onClick={() => lifecycle(a.action)}
              disabled={working}
              className={a.primary ? "primary" : ""}
            >
              {a.label}
            </button>
          ))}
          <button onClick={validate} disabled={working}>Validate (dry-run)</button>
        </div>
        {asset.lifecycle_state === "published" && (
          <p className="meta" style={{ marginTop: 8 }}>
            <strong>Unpublish (recall)</strong> pulls this asset back to <code>registered</code> so you can
            correct it and re-publish — without archiving. It disappears from the public view until re-published.
          </p>
        )}
        {validateResult && <pre style={{ marginTop: 12 }}>{JSON.stringify(validateResult, null, 2)}</pre>}
      </div>

      <div className="card">
        <h3>Edit metadata <span className="meta">(creates a new version)</span></h3>
        <p className="meta" style={{ marginBottom: 6 }}>Description</p>
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          rows={3}
          style={{ width: "100%", padding: 8, fontFamily: "inherit", borderRadius: 6, border: "1px solid #d8dbf0" }}
          placeholder="Edit the description, then Save to record a new revision…"
        />
        <div style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <label className="meta" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            Change source:
            <select
              value={changeSource}
              onChange={(e) => setChangeSource(e.target.value as "manual" | "agent")}
              style={{ padding: "4px 8px", borderRadius: 6, border: "1px solid #d8dbf0", fontFamily: "inherit" }}
            >
              <option value="manual">Manual</option>
              <option value="agent">Agent-assisted (applying a MetaMate suggestion)</option>
            </select>
          </label>
          <button className="primary" onClick={saveEdit} disabled={working || desc === (asset.description || "")}>
            Save change
          </button>
        </div>
        <p className="meta" style={{ marginTop: 6, fontSize: 12 }}>
          Pick <strong>Agent-assisted</strong> when applying a MetaMate proposal — the revision is then recorded as <code>change_source: "agent"</code> in the history below.
        </p>
      </div>

      <div className="card">
        <h3>Revision history <span className="meta">(immutable audit trail)</span></h3>
        {revisions.length === 0 && <p className="meta">No revisions visible.</p>}
        {revisions.map((r) => (
          <div key={r.revision_number} style={{ borderBottom: "1px solid #eee", padding: "6px 0" }}>
            <strong>r{r.revision_number}</strong> ·{" "}
            <span style={{ color: "#6366F1", fontWeight: 600 }}>{r.change_source}</span> ·{" "}
            <span className="meta">
              {new Date(r.changed_at || r.timestamp || "").toLocaleString()}
            </span>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>Raw JSON</h3>
        <pre>{JSON.stringify(asset, null, 2)}</pre>
      </div>
    </div>
  );
}
