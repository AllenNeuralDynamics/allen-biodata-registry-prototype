import MetaMateAvatar from "../components/MetaMateAvatar";
import MetaMateChat from "../components/MetaMateChat";

/**
 * MetaMatePage — the full-page MetaMate explorer tab. A roomy view for
 * reading and exploring datasets through the read-only assistant. Shares the
 * same conversation memory as the floating bubble (sessionStorage), so the
 * thread continues seamlessly between the two.
 */
export default function MetaMatePage({ user }: { user: string | null }) {
  return (
    <div className="mm-page">
      <div className="mm-page-head">
        <MetaMateAvatar size={56} glow />
        <div>
          <h2 style={{ margin: 0 }}>MetaMate</h2>
          <p className="meta" style={{ margin: "2px 0 0" }}>
            Your read-only metadata assistant — search and explore datasets, resolve
            species/strains in NCBI Taxonomy, and review proposed enrichments.
            MetaMate only ever shows data you're allowed to see, and never changes anything.
          </p>
        </div>
      </div>
      <div className="mm-page-card">
        <MetaMateChat user={user} fullPage />
      </div>
    </div>
  );
}
