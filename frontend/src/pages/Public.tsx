/**
 * Public landing — anyone can browse, search, and open published assets
 * without signing in. Backed by the unauthenticated endpoints:
 *   - GET /public/stats         → aggregate counts (no row-level data)
 *   - GET /public/assets?q=     → browse/search published+valid+non-sensitive
 *   - GET /public/assets/{id}   → single published asset detail
 *
 * Validates: R21.1, R21.2, R21.3, R21.4 — public dashboards + browse.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { config } from "../config";
import { login } from "../auth";
import { api } from "../api";

interface PublicStats {
  total?: number;
  published?: number;
  validated?: number;
  unavailable?: boolean;
  error?: string;
}

interface AssetHit {
  id: string;
  source: {
    id: string;
    name?: string;
    data_type?: string;
    species?: string | null;
    storage_uri?: string;
    organization?: string;
    lifecycle_state?: string;
    validation_status?: string;
  };
}

async function fetchPublicStats(): Promise<PublicStats> {
  const url = `${config.apiBase}/public/stats?t=${Date.now()}`;
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) return { unavailable: true, error: `HTTP ${r.status}` };
    return (await r.json()) as PublicStats;
  } catch (e: any) {
    return { unavailable: true, error: `${e?.name || "Error"}: ${e?.message || e}` };
  }
}

export default function PublicLanding() {
  const [query, setQuery] = useState("");
  const [stats, setStats] = useState<PublicStats | null>(null);
  const [assets, setAssets] = useState<AssetHit[]>([]);
  const [total, setTotal] = useState(0);
  const [facets, setFacets] = useState<Record<string, { value: string; count: number }[]>>({});
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    fetchPublicStats().then(setStats);
    runBrowse("");
  }, []);

  async function runBrowse(q: string) {
    setLoading(true);
    setDetail(null);
    try {
      const r = await api.publicAssets(q, 24);
      setAssets(r.hits || []);
      setTotal(r.total || 0);
      setFacets(r.facets || {});
    } catch {
      setAssets([]);
      setTotal(0);
      setFacets({});
    } finally {
      setLoading(false);
    }
  }

  async function openDetail(id: string) {
    setDetailLoading(true);
    setDetail(null);
    try {
      setDetail(await api.publicAsset(id));
    } catch (e: any) {
      setDetail({ error: e?.message || "could not load asset" });
    } finally {
      setDetailLoading(false);
    }
  }

  const fmt = (n: number | null | undefined) =>
    stats === null ? "…" : (n === null || n === undefined ? "—" : n.toLocaleString());

  const EXAMPLES = ["multiplane-ophys", "ecephys", "behavior", "SmartSPIM"];

  return (
    <div>
      <section className="hero">
        <h1>Allen Institute BioData Registry</h1>
        <p>
          A unified, governed metadata registry for AIND data assets. Discover
          published research data, track validation status, and explore
          provenance across modalities — without leaving your browser.
        </p>
        <div className="hero-cta">
          <button className="primary" onClick={login}>Sign in</button>
          <Link to="/about">Learn more</Link>
        </div>
      </section>

      <section className="public-search-card card">
        <form className="search-bar" onSubmit={(e) => { e.preventDefault(); runBrowse(query); }}>
          <input
            type="text"
            placeholder="Search published research data… (e.g. multiplane-ophys, ecephys, behavior)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="primary" type="submit">Search</button>
        </form>
        <div style={{ marginTop: 8 }}>
          <span className="meta" style={{ marginRight: 8 }}>Try:</span>
          {EXAMPLES.map((ex) => (
            <button key={ex} type="button" onClick={() => { setQuery(ex); runBrowse(ex); }}
              style={{ margin: "0 6px 6px 0", padding: "3px 10px", fontSize: 12, borderRadius: 14,
                border: "1px solid #6366F1", background: "#eef0ff", color: "#3730a3", cursor: "pointer" }}>
              {ex}
            </button>
          ))}
          {query && (
            <button type="button" onClick={() => { setQuery(""); runBrowse(""); }}
              style={{ margin: "0 6px 6px 0", padding: "3px 10px", fontSize: 12, borderRadius: 14,
                border: "1px solid #ccc", background: "#fff", color: "#555", cursor: "pointer" }}>
              clear
            </button>
          )}
        </div>
        <p className="meta" style={{ marginTop: 4 }}>
          Published assets are visible to anyone — no login. Sign in for full access including drafts and your spaces.
        </p>
      </section>

      <section className="public-stats">
        <div className="stat-card card">
          <h3>Total assets</h3>
          <div className="stat-value">{fmt(stats?.total)}</div>
          <div className="meta">metadata records across the registry</div>
        </div>
        <div className="stat-card card">
          <h3>Published</h3>
          <div className="stat-value">{fmt(stats?.published)}</div>
          <div className="meta">openly browsable without login</div>
        </div>
        <div className="stat-card card">
          <h3>Validated</h3>
          <div className="stat-value">{fmt(stats?.validated)}</div>
          <div className="meta">passed aind-data-schema validation</div>
        </div>
      </section>

      {/* Facet summary across the current published result set */}
      {Object.keys(facets).length > 0 && (
        <section className="card">
          <h3 style={{ marginTop: 0, fontSize: 15 }}>
            Browsing {total.toLocaleString()} published datasets
            <span className="meta" style={{ fontWeight: 400 }}> · faceted counts computed in one query</span>
          </h3>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
            {[["data_type", "Modality / platform"], ["species", "Species"], ["organization", "Organization"]].map(
              ([key, label]) =>
                facets[key] && facets[key].length > 0 ? (
                  <div key={key}>
                    <div className="meta" style={{ fontWeight: 600, color: "#3730a3", marginBottom: 4 }}>{label}</div>
                    {facets[key].slice(0, 6).map((b) => (
                      <div key={String(b.value)} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "2px 0", borderBottom: "1px solid #f1f1f1" }}>
                        <span>{b.value === null ? "—" : String(b.value)}</span>
                        <span style={{ fontWeight: 600 }}>{b.count}</span>
                      </div>
                    ))}
                  </div>
                ) : null
            )}
          </div>
        </section>
      )}

      {/* Asset detail panel (opens when a dataset is clicked) */}
      {(detail || detailLoading) && (
        <section className="card" style={{ borderLeft: "4px solid #6366F1" }}>
          <button type="button" onClick={() => setDetail(null)} style={{ float: "right", border: "none", background: "none", cursor: "pointer", fontSize: 18 }}>×</button>
          {detailLoading ? (
            <p className="meta">Loading dataset…</p>
          ) : detail?.error ? (
            <p className="meta" style={{ color: "#d13212" }}>{detail.error}</p>
          ) : (
            <div>
              <h3 style={{ marginTop: 0 }}>{detail.name}</h3>
              <div className="meta">
                {detail.data_type} · {detail.lifecycle_state} · {detail.validation_status}
                {detail.species ? ` · ${detail.species}` : ""}
              </div>
              {detail.storage_uri && (
                <p className="meta" style={{ marginTop: 8 }}><strong>Storage URI:</strong> <code>{detail.storage_uri}</code></p>
              )}
              {detail.description && <p style={{ marginTop: 8, fontSize: 13 }}>{detail.description}</p>}
              <p className="meta" style={{ marginTop: 8 }}><strong>ID:</strong> <code>{detail.id}</code></p>
            </div>
          )}
        </section>
      )}

      {/* Browsable list of published datasets */}
      <section>
        <h3 style={{ margin: "8px 0" }}>Published datasets {!loading && total > 0 ? `(${total.toLocaleString()})` : ""}</h3>
        {loading ? (
          <p className="meta">Loading published datasets…</p>
        ) : assets.length === 0 ? (
          <p className="meta">No published datasets match that search.</p>
        ) : (
          assets.map((h) => (
            <div className="card" key={h.id} style={{ cursor: "pointer" }} onClick={() => openDetail(h.id)}>
              <h3 style={{ fontSize: 15, margin: 0 }}>{h.source.name || h.id}</h3>
              <div className="meta" style={{ marginTop: 4 }}>
                {h.source.data_type && <span>{h.source.data_type} · </span>}
                {h.source.species && <span>{h.source.species} · </span>}
                <span>{h.source.validation_status}</span>
              </div>
              {h.source.storage_uri && (
                <div className="meta" style={{ marginTop: 4, fontSize: 12 }}>{h.source.storage_uri}</div>
              )}
            </div>
          ))
        )}
      </section>

      {stats?.unavailable && (
        <section className="card">
          <p className="meta">Live counts are momentarily unavailable (public <code>/public/stats</code> endpoint).</p>
        </section>
      )}
    </div>
  );
}
