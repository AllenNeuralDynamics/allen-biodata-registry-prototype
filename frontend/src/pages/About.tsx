/**
 * About — accessible without login. Explains what the registry does
 * and how to engage. Doubles as a landing target for sign-out and
 * "Learn more" CTA.
 */
export default function About() {
  return (
    <div>
      <h2>About the Allen BioData Registry</h2>

      <div className="card">
        <h3>Mission</h3>
        <p>
          The Allen Institute BioData Registry provides a single, governed
          home for AIND research metadata across modalities (behavior, ephys,
          ophys, fmri, icephys, ecephys, histology, and ccf-registration).
          Every metadata change is tracked through an immutable revision
          history; every sensitive-data path is enforced at three independent
          layers; every dataset can be reused with a stable identifier.
        </p>
      </div>

      <div className="card">
        <h3>Architecture at a glance</h3>
        <ul>
          <li>
            <strong>Aurora PostgreSQL</strong> — system of record. RLS policies
            enforce visibility based on
            <code>app.current_user_id</code>, <code>app.current_org_ids</code>,
            <code>app.current_space_ids</code>, and <code>app.current_roles</code>
            session GUCs.
          </li>
          <li>
            <strong>DocumentDB</strong> — denormalized read store for the
            <code>aind-data-access-api</code> client library. CDC populates
            this asynchronously through SQS.
          </li>
          <li>
            <strong>OpenSearch Serverless</strong> — full-text and vector
            search. Synonym expansion, BM25 multi-match, and pgvector
            embeddings populated by the embedding-backfill Lambda.
          </li>
          <li>
            <strong>Bedrock Knowledge Base</strong> — backs the
            <code>POST /search/nl</code> path. Claude generates SQL
            constrained by the registry's DDL + JSONB conventions +
            ontology mappings.
          </li>
          <li>
            <strong>AgentCore Runtime</strong> — runs the metadata curation
            agent under a strictly read-only IAM boundary. Every
            agent-proposed change passes through human approval before the
            REST write path.
          </li>
        </ul>
      </div>

      <div className="card">
        <h3>How to use the registry</h3>
        <ol>
          <li>
            Sign in via the Allen Institute Cognito hosted UI.
          </li>
          <li>
            Browse the <strong>Search</strong> page (full-text + NL→SQL).
          </li>
          <li>
            Use the <strong>Register asset</strong> page for new metadata
            entries; client-side validation gives instant feedback.
          </li>
          <li>
            Open the <strong>Agent</strong> chat for ontology-aware metadata
            curation suggestions.
          </li>
          <li>
            <strong>Admin</strong>, <strong>Collections</strong>, and{" "}
            <strong>Sharing</strong> tabs are role-gated; they appear only
            when you hold the required role.
          </li>
        </ol>
      </div>

      <div className="card">
        <h3>External integration</h3>
        <p>
          Programmatic access is available through the typed Python client
          (generated from the same OpenAPI spec the API Gateway is built
          from). External AI agents can also connect via the registry's MCP
          server, which exposes read-only tools subject to the same RLS and
          SQL guardrails as the web UI.
        </p>
      </div>
    </div>
  );
}
