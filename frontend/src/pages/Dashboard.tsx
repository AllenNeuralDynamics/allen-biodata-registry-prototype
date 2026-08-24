import { useEffect, useState } from "react";
import { api } from "../api";
import { getToken, decodeJwt } from "../auth";

export default function Dashboard() {
  const [counts, setCounts] = useState<any[]>([]);
  const [validation, setValidation] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  const claims = decodeJwt(getToken() || "");
  const who = claims?.email || claims?.sub || "your account";

  useEffect(() => {
    Promise.all([api.metricsAssetCounts(), api.metricsValidationDistribution()])
      .then(([c, v]) => {
        setCounts(c.by_lifecycle_state || []);
        setValidation(v.by_validation_status || []);
      })
      .catch((e) => setError(e.message));
  }, []);

  return (
    <div>
      <h2>Registry overview</h2>
      <div className="info-banner" style={{
        background: "#eef0ff", border: "1px solid #6366F1", borderRadius: 6,
        padding: "8px 12px", margin: "8px 0 16px", fontSize: 13, color: "#3730a3",
      }}>
        🔒 Showing only the data <strong>{who}</strong> is allowed to see — these
        counts are scoped by database row-level security. Sign in as a different
        persona and the numbers change.
      </div>
      {error && <div className="warning-banner">Error: {error}</div>}

      <h3 style={{ marginTop: 24 }}>Lifecycle distribution</h3>
      <div className="metric-grid">
        {counts.map((c: any) => (
          <div className="metric" key={c.state}>
            <div className="num">{c.count}</div>
            <div className="label">{c.state}</div>
          </div>
        ))}
      </div>

      <h3 style={{ marginTop: 32 }}>Validation status</h3>
      <div className="metric-grid">
        {validation.map((c: any) => (
          <div className="metric" key={c.status}>
            <div className="num">{c.count}</div>
            <div className="label">{c.status}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
