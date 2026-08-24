import { useState, useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";

interface Hit {
  id: string;
  score: number;
  source: {
    id: string;
    name?: string;
    data_type?: string;
    lifecycle_state?: string;
    validation_status?: string;
    storage_uri?: string;
    space_id?: string;
    is_sensitive?: boolean;
  };
}

export default function Search() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Hit[]>([]);
  const [total, setTotal] = useState(0);
  const [facets, setFacets] = useState<Record<string, { value: string; count: number }[]>>({});
  const [suggestions, setSuggestions] = useState<{ id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nlMode, setNlMode] = useState(false);
  const [nlResult, setNlResult] = useState<any | null>(null);
  const debounceTimer = useRef<number | null>(null);

  // Verified demo queries — one click runs them so there's nothing to type.
  const EXAMPLES = [
    "how many assets are in each lifecycle state",
    "show me all published mouse data",
    "what data types do we have and how many of each",
  ];
  const KEYWORD_EXAMPLES = ["multiplane-ophys", "ecephys", "behavior", "SmartSPIM", "single-plane-ophys"];

  // Debounced suggestions while typing.
  useEffect(() => {
    if (!query || nlMode) {
      setSuggestions([]);
      return;
    }
    if (debounceTimer.current) window.clearTimeout(debounceTimer.current);
    debounceTimer.current = window.setTimeout(() => {
      api.suggest(query)
        .then((r) => setSuggestions(r.suggestions || []))
        .catch(() => setSuggestions([]));
    }, 200);
    return () => {
      if (debounceTimer.current) window.clearTimeout(debounceTimer.current);
    };
  }, [query, nlMode]);

  async function runSearch(e?: React.FormEvent, qOverride?: string) {
    if (e) e.preventDefault();
    const q = (qOverride ?? query).trim();
    if (!q) return;
    if (qOverride !== undefined) setQuery(qOverride);
    setLoading(true);
    setError(null);
    setNlResult(null);
    try {
      if (nlMode) {
        // Natural-language search.
        const resp = await fetch(
          `${import.meta.env.VITE_API_BASE || "https://pho8lsqt7d.execute-api.us-west-2.amazonaws.com/dev"}/search/nl`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Authorization": localStorage.getItem("biodata_registry_id_token") || "",
            },
            body: JSON.stringify({ question: q }),
          }
        );
        const body = await resp.json();
        if (!resp.ok) throw new Error(body.message || `HTTP ${resp.status}`);
        setNlResult(body);
        setResults([]);
        setTotal(0);
        setFacets({});
      } else {
        const r = await api.search(q, 30);
        setResults(r.hits || []);
        setTotal(r.total || 0);
        setFacets(r.facets || {});
      }
    } catch (e: any) {
      setError(e.message || "search failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <h2>{nlMode ? "Natural-language search" : "Search assets"}</h2>

      <div style={{ marginBottom: 12 }}>
        <label style={{ fontSize: 14, color: "#666" }}>
          <input
            type="checkbox"
            checked={nlMode}
            onChange={(e) => setNlMode(e.target.checked)}
            style={{ marginRight: 6 }}
          />
          Use natural language (Bedrock NL→SQL)
        </label>
      </div>

      <form className="search-bar" onSubmit={runSearch}>
        <input
          autoFocus
          type="text"
          placeholder={nlMode ? "Ask in plain English — e.g. 'how many assets are in each lifecycle state'" : "Search by name, modality, species…"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button type="submit" className="primary" disabled={!query || loading}>
          {loading ? "Searching…" : "Search"}
        </button>
      </form>

      {nlMode && (
        <div style={{ margin: "4px 0 14px" }}>
          <span className="meta" style={{ marginRight: 8 }}>Try:</span>
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              type="button"
              onClick={() => runSearch(undefined, ex)}
              disabled={loading}
              style={{
                margin: "0 6px 6px 0", padding: "4px 10px", fontSize: 12,
                borderRadius: 14, border: "1px solid #6366F1", background: "#eef0ff",
                color: "#3730a3", cursor: "pointer",
              }}
            >
              {ex}
            </button>
          ))}
        </div>
      )}

      {!nlMode && (
        <div style={{ margin: "4px 0 14px" }}>
          <span className="meta" style={{ marginRight: 8 }}>Try:</span>
          {KEYWORD_EXAMPLES.map((ex) => (
            <button
              key={ex}
              type="button"
              onClick={() => runSearch(undefined, ex)}
              disabled={loading}
              style={{
                margin: "0 6px 6px 0", padding: "4px 10px", fontSize: 12,
                borderRadius: 14, border: "1px solid #6366F1", background: "#eef0ff",
                color: "#3730a3", cursor: "pointer",
              }}
            >
              {ex}
            </button>
          ))}
        </div>
      )}

      {!nlMode && suggestions.length > 0 && (
        <div className="card" style={{ padding: 8 }}>
          {suggestions.map((s) => (
            <div key={s.id} style={{ padding: "4px 0" }}>
              <Link to={`/asset/${s.id}`}>{s.name}</Link>
            </div>
          ))}
        </div>
      )}

      {error && <div className="warning-banner">Error: {error}</div>}

      {nlResult && (
        <div className="card">
          <h3>NL→SQL result</h3>
          <p className="meta">
            {nlResult.cache_hit ? "Cache hit" : "Generated by Bedrock"} ·
            {" "}
            {nlResult.row_count} row{nlResult.row_count === 1 ? "" : "s"}
          </p>
          <pre>{nlResult.sql}</pre>
          {nlResult.rows && nlResult.rows.length > 0 ? (
            <table style={{ width: "100%", marginTop: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr>{nlResult.columns.map((c: string) => (
                  <th key={c} style={{ textAlign: "left", padding: 6, borderBottom: "1px solid #ddd" }}>{c}</th>
                ))}</tr>
              </thead>
              <tbody>
                {nlResult.rows.map((row: any[], i: number) => (
                  <tr key={i}>
                    {row.map((v, j) => (
                      <td key={j} style={{ padding: 6, borderBottom: "1px solid #f1f1f1" }}>
                        {v === null ? <span style={{ color: "#aaa" }}>—</span> : String(v)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="meta">No rows returned.</p>
          )}
        </div>
      )}

      {!nlMode && results.length > 0 && (
        <div>
          <p className="meta" style={{ margin: "8px 0" }}>
            {total} total · showing top {results.length}
          </p>
          {Object.keys(facets).length > 0 && (
            <div className="card" style={{ marginBottom: 12 }}>
              <h3 style={{ marginTop: 0, fontSize: 15 }}>Facets <span className="meta" style={{ fontWeight: 400 }}>· counts across all {total} matches, computed in one query</span></h3>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
                {[
                  ["data_type", "Modality / platform"],
                  ["species", "Species"],
                  ["lifecycle_state", "Lifecycle"],
                  ["validation_status", "Validation"],
                  ["organization", "Organization"],
                ].map(([key, label]) =>
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
            </div>
          )}
          {results.map((h) => (
            <div className="card" key={h.id}>
              <h3>
                <Link to={`/asset/${h.id}`}>{h.source.name || h.id}</Link>
              </h3>
              <div className="meta">
                {h.source.data_type && <span>{h.source.data_type} · </span>}
                {h.source.lifecycle_state && <span>{h.source.lifecycle_state} · </span>}
                {h.source.validation_status && <span>{h.source.validation_status}</span>}
                {h.source.is_sensitive && <span style={{ color: "#E7157B", marginLeft: 6 }}>· sensitive</span>}
              </div>
              {h.source.storage_uri && (
                <div className="meta" style={{ marginTop: 4 }}>{h.source.storage_uri}</div>
              )}
            </div>
          ))}
        </div>
      )}

      {!nlMode && !loading && query && results.length === 0 && !error && (
        <p className="meta">No results.</p>
      )}
    </div>
  );
}
