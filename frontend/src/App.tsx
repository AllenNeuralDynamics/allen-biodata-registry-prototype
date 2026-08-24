import { useEffect, useState } from "react";
import { Routes, Route, Link, useLocation, NavLink } from "react-router-dom";
import { captureTokenFromHash, decodeJwt, getToken, login, logout } from "./auth";
import AllenLogo from "./components/AllenLogo";
import Search from "./pages/Search";
import AssetDetail from "./pages/AssetDetail";
import Dashboard from "./pages/Dashboard";
import CreateAsset from "./pages/CreateAsset";
import PublicLanding from "./pages/Public";
import About from "./pages/About";
import Admin from "./pages/Admin";
import Collections from "./pages/Collections";
import Sharing from "./pages/Sharing";
import MetaMatePage from "./pages/MetaMatePage";
import MetaMateWidget from "./components/MetaMateWidget";

/**
 * Header — mirrors the v2 PoC scope document's `.hdr`: dark navy bar with a
 * periwinkle bottom border, the Allen logo on the left, a title block in the
 * middle, and badges describing the stack.
 */
function Header({ user, onLogout }: { user: string | null; onLogout: () => void }) {
  return (
    <div className="hdr">
      <Link to="/" className="hdr-logo" aria-label="Allen Institute home">
        <AllenLogo height={64} variant="dark" />
      </Link>
      <div className="hdr-text">
        <h1>Allen BioData Registry</h1>
        <p>Unified metadata registry with AI-powered search, enrichment, and multi-tenant governance</p>
        <div className="hdr-badges">
          <span className="badge badge-poc">Proof of Concept</span>
          <span className="badge badge-au">Aurora PostgreSQL</span>
          <span className="badge badge-os">OpenSearch</span>
          <span className="badge badge-or">Bedrock + AgentCore</span>
        </div>
      </div>
      <div className="hdr-user">
        {user ? (
          <>
            <span className="hdr-user-email">{user}</span>
            <button onClick={onLogout}>Sign out</button>
          </>
        ) : (
          <button className="primary" onClick={login}>Sign in</button>
        )}
      </div>
    </div>
  );
}

/**
 * Tab nav — mirrors the scope doc's `.nav-container` / `.nav-tab`: a sticky
 * white bar of tabs that underline in periwinkle on hover and pink-ish on
 * active.
 */
function TabNav({ user }: { user: string | null }) {
  const tabs = user
    ? [
        { to: "/", label: "Dashboard", end: true },
        { to: "/search", label: "Search" },
        { to: "/metamate", label: "🤖 MetaMate" },
        { to: "/create", label: "Register asset" },
        { to: "/admin", label: "Admin" },
        { to: "/collections", label: "Collections" },
        { to: "/sharing", label: "Sharing" },
        { to: "/about", label: "About" },
      ]
    : [
        { to: "/", label: "Home", end: true },
        { to: "/about", label: "About" },
      ];

  return (
    <div className="nav-container">
      <div className="nav-tabs">
        {tabs.map((t) => (
          <NavLink
            key={t.to}
            to={t.to}
            end={t.end}
            className={({ isActive }) => "nav-tab" + (isActive ? " active" : "")}
          >
            {t.label}
          </NavLink>
        ))}
      </div>
    </div>
  );
}

function Footer() {
  return (
    <div className="footer">
      <strong>Allen Institute BioData Registry</strong> — PoC handoff build
      <br />
      Powered by AWS Aurora PostgreSQL · OpenSearch · DocumentDB · Bedrock + AgentCore
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<string | null>(null);
  const location = useLocation();

  useEffect(() => {
    captureTokenFromHash();
    const token = getToken();
    if (token) {
      const claims = decodeJwt(token);
      setUser(claims?.email || claims?.sub || "user");
    } else {
      setUser(null);
    }
  }, [location.pathname]);

  return (
    <div className="app">
      <Header user={user} onLogout={logout} />
      <TabNav user={user} />
      <div className="main">
        <Routes>
          {user ? (
            <>
              <Route path="/" element={<Dashboard />} />
              <Route path="/search" element={<Search />} />
              <Route path="/metamate" element={<MetaMatePage user={user} />} />
              <Route path="/asset/:id" element={<AssetDetail />} />
              <Route path="/create" element={<CreateAsset />} />
              <Route path="/admin" element={<Admin />} />
              <Route path="/collections" element={<Collections />} />
              <Route path="/sharing" element={<Sharing />} />
              <Route path="/about" element={<About />} />
            </>
          ) : (
            <>
              <Route path="/" element={<PublicLanding />} />
              <Route path="/about" element={<About />} />
              <Route path="/search" element={<PublicLanding />} />
            </>
          )}
        </Routes>
      </div>
      {user ? <MetaMateWidget user={user} /> : <MetaMateWidget user={null} publicMode />}
      <Footer />
    </div>
  );
}
