import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";

const MODALITIES = [
  "behavior", "ephys", "ophys", "fmri",
  "icephys", "ecephys", "histology", "ccf-registration",
];

export default function CreateAsset() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [storageUri, setStorageUri] = useState("");
  const [dataType, setDataType] = useState("behavior");
  const [description, setDescription] = useState("");
  const [validateOnly, setValidateOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [validateResult, setValidateResult] = useState<any | null>(null);

  async function preview() {
    setBusy(true);
    setError(null);
    setValidateResult(null);
    try {
      const r = await api.validate("data_asset", {
        name, storage_uri: storageUri, data_type: dataType, description,
      });
      setValidateResult(r);
    } catch (e: any) {
      setError(e?.body?.message || e.message || "validation failed");
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (validateOnly) return preview();
    setBusy(true);
    setError(null);
    try {
      const payload = {
        name,
        storage_uri: storageUri,
        data_type: dataType,
        description,
      };
      const created = await api.createAsset(payload);
      navigate(`/asset/${created.id}`);
    } catch (e: any) {
      setError(e?.body?.message || e.message || "creation failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h2>Register a new asset</h2>

      <form className="card" onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label>
          <div className="meta">Name <span style={{ color: "#E7157B" }}>*</span></div>
          <input
            required
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="behavior-2026-05-01-mouse-001"
          />
        </label>

        <label>
          <div className="meta">Storage URI <span style={{ color: "#E7157B" }}>*</span></div>
          <input
            required
            type="text"
            value={storageUri}
            onChange={(e) => setStorageUri(e.target.value)}
            placeholder="s3://aind-ephys/2026/05/01/run-001.json"
          />
        </label>

        <label>
          <div className="meta">Data type <span style={{ color: "#E7157B" }}>*</span></div>
          <select
            required
            value={dataType}
            onChange={(e) => setDataType(e.target.value)}
          >
            {MODALITIES.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </label>

        <label>
          <div className="meta">Description</div>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            placeholder="Optional free-text description for the agent's RAG context."
          />
        </label>

        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <label className="meta" style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <input
              type="checkbox"
              checked={validateOnly}
              onChange={(e) => setValidateOnly(e.target.checked)}
            />
            Validate only (don't persist)
          </label>
        </div>

        {error && <div className="warning-banner">Error: {error}</div>}

        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" onClick={preview} disabled={busy || !name || !storageUri}>
            Preview validation
          </button>
          <button type="submit" className="primary" disabled={busy || !name || !storageUri}>
            {validateOnly ? "Validate" : "Register asset"}
          </button>
        </div>
      </form>

      {validateResult && (
        <div className="card">
          <h3>Validation result</h3>
          <p className="meta">
            {validateResult.valid ? "✓ Valid" : "✗ Errors"} · entity_type=
            {validateResult.entity_type} · dry_run=
            {String(validateResult.dry_run)}
          </p>
          {validateResult.errors && validateResult.errors.length > 0 && (
            <ul>
              {validateResult.errors.map((e: any, i: number) => (
                <li key={i}><strong>{e.field}</strong>: {e.error}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
