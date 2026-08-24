import { useState } from "react";
import { useLocation } from "react-router-dom";
import MetaMateAvatar from "./MetaMateAvatar";
import MetaMateChat from "./MetaMateChat";

/**
 * MetaMateWidget — the floating launcher + chat panel available on every
 * authenticated page. Wraps the shared MetaMateChat core. Hidden on the
 * dedicated /metamate page (which renders the full-page version) so the
 * conversation isn't shown twice.
 */
export default function MetaMateWidget({
  user,
  publicMode = false,
}: {
  user: string | null;
  publicMode?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const location = useLocation();

  if (!user && !publicMode) return null; // authed widget needs a user
  if (location.pathname === "/metamate") return null; // full-page tab handles it

  return (
    <>
      {!open && (
        <button
          className="mm-launcher"
          onClick={() => setOpen(true)}
          aria-label="Open MetaMate assistant"
        >
          <span className="mm-launcher-pulse" />
          <MetaMateAvatar size={46} />
          <span className="mm-launcher-label">Ask&nbsp;MetaMate</span>
        </button>
      )}

      {open && (
        <div className="mm-panel" role="dialog" aria-label="MetaMate assistant">
          <div className="mm-header">
            <MetaMateAvatar size={40} glow />
            <div className="mm-header-text">
              <strong>MetaMate</strong>
              <span>
                {publicMode
                  ? "Allen open-data guide · published data only"
                  : "Allen Institute metadata assistant · read-only"}
              </span>
            </div>
            <button className="mm-icon-btn" title="Minimize" onClick={() => setOpen(false)}>—</button>
          </div>
          <MetaMateChat user={user} publicMode={publicMode} />
        </div>
      )}
    </>
  );
}
