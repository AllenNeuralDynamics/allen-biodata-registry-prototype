/**
 * Collections — create + nest collections, attach assets, mint DOIs.
 *
 * Backend: POST /collections, POST /collections/{id}/assets,
 *          POST /collections/{id}/children, PUT /collections/{id}/doi.
 *
 * RLS: the Collections_Lambda enforces visibility — a user can only add
 * assets they can see. The cycle detection in
 * detect_collection_cycle() returns INVALID_HIERARCHY on bad parent/child
 * pairs; this page surfaces that 400 verbatim.
 *
 * Validates: R12.1, R12.2, R12.3, R12.4, R24.1, R24.2, R24.3.
 */
import { useState } from "react";
import { config } from "../config";
import { getToken } from "../auth";
import { api } from "../api";

interface Collection {
  id: string;
  name: string;
  description?: string;
  doi?: string;
}

async function authedRequest(path: string, options: RequestInit = {}) {
  const r = await fetch(`${config.apiBase}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: getToken() || "",
      ...(options.headers as Record<string, string> | undefined ?? {}),
    },
  });
  const text = await r.text();
  let body: any = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!r.ok) throw new Error(body?.message || `HTTP ${r.status}`);
  return body;
}

export default function CollectionsPage() {
  const [collections, setCollections] = useState<Collection[]>([]);

  // Create form state.
  const [name, setName] = useState("");
  const [spaceId, setSpaceId] = useState("");
  const [description, setDescription] = useState("");
  const [createMsg, setCreateMsg] = useState<string | null>(null);

  // Add-asset form state.
  const [parentColId, setParentColId] = useState("");
  const [assetId, setAssetId] = useState("");
  const [addAssetMsg, setAddAssetMsg] = useState<string | null>(null);

  // Nest form state.
  const [parentForChild, setParentForChild] = useState("");
  const [childCol, setChildCol] = useState("");
  const [nestMsg, setNestMsg] = useState<string | null>(null);

  // DOI form state.
  const [doiCol, setDoiCol] = useState("");
  const [doiVal, setDoiVal] = useState("");
  const [doiMsg, setDoiMsg] = useState<string | null>(null);

  async function createCollection(e: React.FormEvent) {
    e.preventDefault();
    setCreateMsg(null);
    try {
      const c = await api.createCollection(name, spaceId, description || undefined);
      setCreateMsg(`Created collection ${c.id}.`);
      setCollections((prev) => [c, ...prev]);
      setName("");
      setDescription("");
    } catch (err: any) {
      setCreateMsg(`Error: ${err.message}`);
    }
  }

  async function addAsset(e: React.FormEvent) {
    e.preventDefault();
    setAddAssetMsg(null);
    try {
      await authedRequest(`/collections/${parentColId}/assets`, {
        method: "POST",
        body: JSON.stringify({ asset_id: assetId }),
      });
      setAddAssetMsg(`Added asset ${assetId} to collection ${parentColId}.`);
      setAssetId("");
    } catch (err: any) {
      setAddAssetMsg(`Error: ${err.message}`);
    }
  }

  async function nestCollection(e: React.FormEvent) {
    e.preventDefault();
    setNestMsg(null);
    try {
      await authedRequest(`/collections/${parentForChild}/children`, {
        method: "POST",
        body: JSON.stringify({ child_id: childCol }),
      });
      setNestMsg(`Nested ${childCol} under ${parentForChild}.`);
      setChildCol("");
    } catch (err: any) {
      // INVALID_HIERARCHY surfaces the cycle path here.
      setNestMsg(`Error: ${err.message}`);
    }
  }

  async function attachDoi(e: React.FormEvent) {
    e.preventDefault();
    setDoiMsg(null);
    try {
      await authedRequest(`/collections/${doiCol}/doi`, {
        method: "PUT",
        body: JSON.stringify({ doi: doiVal }),
      });
      setDoiMsg(`Attached DOI ${doiVal} to collection ${doiCol}.`);
    } catch (err: any) {
      setDoiMsg(`Error: ${err.message}`);
    }
  }

  return (
    <div>
      <h2>Collections</h2>

      <div className="card">
        <h3>Create collection</h3>
        <form onSubmit={createCollection}>
          <div style={{ marginBottom: 8 }}>
            <label>Name</label>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Space ID (UUID)</label>
            <input type="text" value={spaceId} onChange={(e) => setSpaceId(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Description (optional)</label>
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} />
          </div>
          <button className="primary" type="submit" disabled={!name || !spaceId}>
            Create
          </button>
        </form>
        {createMsg && <p className="meta" style={{ marginTop: 8 }}>{createMsg}</p>}
      </div>

      {collections.length > 0 && (
        <div className="card">
          <h3>Created in this session</h3>
          <ul>
            {collections.map((c) => (
              <li key={c.id}><code>{c.id}</code> — {c.name}{c.doi && ` · DOI ${c.doi}`}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="card">
        <h3>Add asset to collection</h3>
        <form onSubmit={addAsset}>
          <div style={{ marginBottom: 8 }}>
            <label>Collection ID</label>
            <input type="text" value={parentColId} onChange={(e) => setParentColId(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Asset ID</label>
            <input type="text" value={assetId} onChange={(e) => setAssetId(e.target.value)} required />
          </div>
          <button className="primary" type="submit" disabled={!parentColId || !assetId}>
            Attach
          </button>
        </form>
        {addAssetMsg && <p className="meta" style={{ marginTop: 8 }}>{addAssetMsg}</p>}
      </div>

      <div className="card">
        <h3>Nest collection (parent → child)</h3>
        <p className="meta">Cycle detection runs server-side; an INVALID_HIERARCHY response includes the offending cycle path.</p>
        <form onSubmit={nestCollection}>
          <div style={{ marginBottom: 8 }}>
            <label>Parent collection ID</label>
            <input type="text" value={parentForChild} onChange={(e) => setParentForChild(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Child collection ID</label>
            <input type="text" value={childCol} onChange={(e) => setChildCol(e.target.value)} required />
          </div>
          <button className="primary" type="submit" disabled={!parentForChild || !childCol}>
            Nest
          </button>
        </form>
        {nestMsg && <p className="meta" style={{ marginTop: 8 }}>{nestMsg}</p>}
      </div>

      <div className="card">
        <h3>Attach DOI</h3>
        <form onSubmit={attachDoi}>
          <div style={{ marginBottom: 8 }}>
            <label>Collection ID</label>
            <input type="text" value={doiCol} onChange={(e) => setDoiCol(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>DOI (e.g. 10.5281/zenodo.123456)</label>
            <input type="text" value={doiVal} onChange={(e) => setDoiVal(e.target.value)} required />
          </div>
          <button className="primary" type="submit" disabled={!doiCol || !doiVal}>
            Attach
          </button>
        </form>
        {doiMsg && <p className="meta" style={{ marginTop: 8 }}>{doiMsg}</p>}
      </div>
    </div>
  );
}
