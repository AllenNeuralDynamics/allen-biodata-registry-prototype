/**
 * Admin — Organization + Space management for org_admin / data_administrator.
 *
 * Surface:
 *  - Create new Organization (POST /orgs).
 *  - Add Space under an Org (POST /orgs/{id}/spaces).
 *  - Assign roles to users (PUT /orgs/{id}/users/{uid}/role).
 *
 * Hidden from non-admin users by App.tsx route gating; this page also
 * surfaces a `getRoles()` helper so individual sections can show/hide
 * controls based on the caller's actual role set rather than just the
 * fact that they got past the auth gate.
 *
 * Validates: R22.2, R22.4, R22.5 — role-aware admin UI.
 */
import { useEffect, useState } from "react";
import { api } from "../api";
import { decodeJwt, getToken } from "../auth";

interface Org {
  id: string;
  name: string;
  display_name?: string;
  notification_topic_arn?: string | null;
}

function getRoles(): string[] {
  const token = getToken();
  if (!token) return [];
  const claims = decodeJwt(token) as Record<string, any> | null;
  // Roles propagate via the Authorizer Lambda's `roles` claim. When the
  // hosted UI sets a custom attribute we read it here; in the PoC the
  // role set is resolved at API time so we treat any signed-in user as
  // having admin access in the UI and let the backend authorization
  // 403 if the caller actually lacks the role.
  const r = claims?.["custom:roles"] || claims?.["roles"] || "";
  return typeof r === "string" ? r.split(",").filter(Boolean) : [];
}

export default function Admin() {
  const roles = getRoles();
  const isOrgAdmin = roles.length === 0 || roles.includes("org_admin") || roles.includes("data_administrator");

  // --- Create Org form state ---
  const [orgName, setOrgName] = useState("");
  const [orgDisplay, setOrgDisplay] = useState("");
  const [createMsg, setCreateMsg] = useState<string | null>(null);

  // --- Recent orgs (best-effort — falls back to empty when /orgs GET is unimplemented) ---
  const [orgs, setOrgs] = useState<Org[]>([]);

  // --- Add Space form state ---
  const [spaceOrg, setSpaceOrg] = useState("");
  const [spaceName, setSpaceName] = useState("");
  const [spaceMsg, setSpaceMsg] = useState<string | null>(null);

  // --- Role assignment form state ---
  const [roleOrg, setRoleOrg] = useState("");
  const [roleUid, setRoleUid] = useState("");
  const [roleValue, setRoleValue] = useState("viewer");
  const [roleMsg, setRoleMsg] = useState<string | null>(null);

  useEffect(() => {
    // Best-effort enumerate. Most PoC deployments don't expose GET /orgs,
    // so we degrade silently to an empty list.
    fetch(`${import.meta.env.VITE_API_BASE || "https://pho8lsqt7d.execute-api.us-west-2.amazonaws.com/dev"}/orgs`, {
      headers: { Authorization: getToken() || "" },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => setOrgs(body?.orgs || body || []))
      .catch(() => setOrgs([]));
  }, []);

  if (!isOrgAdmin) {
    return (
      <div>
        <h2>Admin</h2>
        <div className="card">
          <p className="meta">
            This page is restricted to org_admin / data_administrator
            principals. Your token does not include either role.
          </p>
        </div>
      </div>
    );
  }

  async function createOrg(e: React.FormEvent) {
    e.preventDefault();
    setCreateMsg(null);
    try {
      const r = await api.createOrg(orgName, orgDisplay || undefined);
      setCreateMsg(`Created org ${r.id}. ${r.notification_topic_arn ? "Notification topic ready." : ""}`);
      setOrgName("");
      setOrgDisplay("");
      setOrgs((prev) => [r, ...prev]);
    } catch (err: any) {
      setCreateMsg(`Error: ${err.message}`);
    }
  }

  async function createSpace(e: React.FormEvent) {
    e.preventDefault();
    setSpaceMsg(null);
    try {
      const resp = await fetch(
        `${import.meta.env.VITE_API_BASE || "https://pho8lsqt7d.execute-api.us-west-2.amazonaws.com/dev"}/orgs/${spaceOrg}/spaces`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: getToken() || "",
          },
          body: JSON.stringify({ name: spaceName }),
        }
      );
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.message || `HTTP ${resp.status}`);
      setSpaceMsg(`Created space ${body.id} under org ${spaceOrg}.`);
      setSpaceName("");
    } catch (err: any) {
      setSpaceMsg(`Error: ${err.message}`);
    }
  }

  async function assignRole(e: React.FormEvent) {
    e.preventDefault();
    setRoleMsg(null);
    try {
      const resp = await fetch(
        `${import.meta.env.VITE_API_BASE || "https://pho8lsqt7d.execute-api.us-west-2.amazonaws.com/dev"}/orgs/${roleOrg}/users/${roleUid}/role`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            Authorization: getToken() || "",
          },
          body: JSON.stringify({ role: roleValue }),
        }
      );
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.message || `HTTP ${resp.status}`);
      setRoleMsg(`Granted ${roleValue} to ${roleUid} on org ${roleOrg}.`);
    } catch (err: any) {
      setRoleMsg(`Error: ${err.message}`);
    }
  }

  return (
    <div>
      <h2>Admin</h2>

      <div className="card">
        <h3>Create Organization</h3>
        <form onSubmit={createOrg}>
          <div style={{ marginBottom: 8 }}>
            <label>Name (machine-readable, no spaces)</label>
            <input
              type="text"
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              required
              pattern="[a-z0-9-]+"
              placeholder="acme-research"
            />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Display name</label>
            <input
              type="text"
              value={orgDisplay}
              onChange={(e) => setOrgDisplay(e.target.value)}
              placeholder="ACME Research Lab"
            />
          </div>
          <button className="primary" type="submit" disabled={!orgName}>
            Create
          </button>
        </form>
        {createMsg && <p className="meta" style={{ marginTop: 8 }}>{createMsg}</p>}
      </div>

      <div className="card">
        <h3>Existing Organizations</h3>
        {orgs.length === 0 ? (
          <p className="meta">No organizations enumerated. (GET /orgs may not be exposed; PoC deployments often only support per-id GET.)</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: 6, borderBottom: "1px solid #ddd" }}>Org ID</th>
                <th style={{ textAlign: "left", padding: 6, borderBottom: "1px solid #ddd" }}>Name</th>
                <th style={{ textAlign: "left", padding: 6, borderBottom: "1px solid #ddd" }}>SNS Topic</th>
              </tr>
            </thead>
            <tbody>
              {orgs.map((o) => (
                <tr key={o.id}>
                  <td style={{ padding: 6, borderBottom: "1px solid #f1f1f1" }}>
                    <code>{o.id}</code>
                  </td>
                  <td style={{ padding: 6, borderBottom: "1px solid #f1f1f1" }}>
                    {o.display_name || o.name}
                  </td>
                  <td style={{ padding: 6, borderBottom: "1px solid #f1f1f1" }}>
                    {o.notification_topic_arn ? "✓" : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3>Add Space to Organization</h3>
        <form onSubmit={createSpace}>
          <div style={{ marginBottom: 8 }}>
            <label>Org ID</label>
            <input type="text" value={spaceOrg} onChange={(e) => setSpaceOrg(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Space name</label>
            <input type="text" value={spaceName} onChange={(e) => setSpaceName(e.target.value)} required />
          </div>
          <button className="primary" type="submit" disabled={!spaceOrg || !spaceName}>
            Create space
          </button>
        </form>
        {spaceMsg && <p className="meta" style={{ marginTop: 8 }}>{spaceMsg}</p>}
      </div>

      <div className="card">
        <h3>Assign role to user</h3>
        <form onSubmit={assignRole}>
          <div style={{ marginBottom: 8 }}>
            <label>Org ID</label>
            <input type="text" value={roleOrg} onChange={(e) => setRoleOrg(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>User ID (Cognito sub or app_user.id)</label>
            <input type="text" value={roleUid} onChange={(e) => setRoleUid(e.target.value)} required />
          </div>
          <div style={{ marginBottom: 8 }}>
            <label>Role</label>
            <select value={roleValue} onChange={(e) => setRoleValue(e.target.value)}>
              <option value="viewer">viewer</option>
              <option value="contributor">contributor</option>
              <option value="space_admin">space_admin</option>
              <option value="org_admin">org_admin</option>
              <option value="data_administrator">data_administrator</option>
            </select>
          </div>
          <button className="primary" type="submit" disabled={!roleOrg || !roleUid}>
            Grant role
          </button>
        </form>
        {roleMsg && <p className="meta" style={{ marginTop: 8 }}>{roleMsg}</p>}
      </div>
    </div>
  );
}
