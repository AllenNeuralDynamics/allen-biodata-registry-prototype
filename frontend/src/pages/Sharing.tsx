/**
 * Sharing — create + view sharing-grants between Orgs and Spaces.
 *
 * Backend: POST /orgs/{id}/sharing-grants.
 *
 * The Authorizer Lambda picks up active sharing grants and merges them
 * into the caller's RLS context on the next sign-in (`access:{user_id}`
 * cache busts immediately on grant creation).
 *
 * Validates: R9.5, R9.6 — sharing-grant creation UI for org_admins.
 */
import { useState } from "react";
import { config } from "../config";
import { getToken, decodeJwt } from "../auth";

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

export default function Sharing() {
  const claims = decodeJwt(getToken() || "");
  const _viewer = claims?.email || claims?.sub || "user";

  const [granterOrg, setGranterOrg] = useState("");
  const [granteeOrg, setGranteeOrg] = useState("");
  const [granteeSpace, setGranteeSpace] = useState("");
  const [role, setRole] = useState("viewer");
  const [expiresAt, setExpiresAt] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  async function submitGrant(e: React.FormEvent) {
    e.preventDefault();
    setMsg(null);
    if (!granterOrg) { setMsg("Granter org ID is required."); return; }
    if (!granteeOrg && !granteeSpace) {
      setMsg("Either grantee org or grantee space must be specified.");
      return;
    }
    try {
      const r = await authedRequest(`/orgs/${granterOrg}/sharing-grants`, {
        method: "POST",
        body: JSON.stringify({
          grantee_org_id: granteeOrg || undefined,
          grantee_space_id: granteeSpace || undefined,
          role,
          expires_at: expiresAt || undefined,
        }),
      });
      setMsg(`Created sharing grant ${r.id}.`);
      setGranteeOrg("");
      setGranteeSpace("");
      setExpiresAt("");
    } catch (err: any) {
      setMsg(`Error: ${err.message}`);
    }
  }

  return (
    <div>
      <h2>Sharing grants</h2>

      <div className="card">
        <p className="meta">
          Sharing grants extend RLS visibility from one Org or Space to
          another. After a grant is created, the recipient's session
          variables (<code>app.current_space_ids</code> and
          <code>app.current_org_ids</code>) include the granted scope on
          their next request — the Access_Filter_Cache is busted immediately
          by Governance_Lambda on every grant mutation.
        </p>
      </div>

      <div className="card">
        <h3>Create sharing grant</h3>
        <form onSubmit={submitGrant}>
          <div style={{ marginBottom: 8 }}>
            <label>Granter org ID *</label>
            <input
              type="text"
              value={granterOrg}
              onChange={(e) => setGranterOrg(e.target.value)}
              required
              placeholder="UUID of the org granting access"
            />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Grantee org ID (or leave empty if granting to a space)</label>
            <input
              type="text"
              value={granteeOrg}
              onChange={(e) => setGranteeOrg(e.target.value)}
              placeholder="UUID of the receiving org"
            />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Grantee space ID (or leave empty if granting to an org)</label>
            <input
              type="text"
              value={granteeSpace}
              onChange={(e) => setGranteeSpace(e.target.value)}
              placeholder="UUID of the receiving space"
            />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Role to grant</label>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="viewer">viewer</option>
              <option value="contributor">contributor</option>
              <option value="space_admin">space_admin</option>
            </select>
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Expires at (optional, ISO date)</label>
            <input
              type="date"
              value={expiresAt}
              onChange={(e) => setExpiresAt(e.target.value)}
            />
          </div>
          <button className="primary" type="submit" disabled={!granterOrg || (!granteeOrg && !granteeSpace)}>
            Create grant
          </button>
        </form>
        {msg && <p className="meta" style={{ marginTop: 8 }}>{msg}</p>}
      </div>
    </div>
  );
}
