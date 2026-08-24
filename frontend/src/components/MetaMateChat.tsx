import { useState, useRef, useEffect } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import MetaMateAvatar from "./MetaMateAvatar";

/**
 * MetaMateChat — the shared, read-only metadata-assistant chat core used by
 * the floating bubble (MetaMateWidget) and the full-page tab (MetaMatePage),
 * in BOTH authenticated and public (landing page) modes.
 *
 *  - Authenticated: calls /agent/chat; full toolset under the caller's RLS.
 *  - Public (publicMode): calls /public/agent/chat; a locked-down,
 *    published-data-only toolset — it can never surface private data.
 *
 * Conversation memory persists for the session (sessionStorage) and survives
 * navigation + refresh. The agent never writes — proposals render read-only.
 */

export interface Msg {
  role: "user" | "assistant";
  content: string;
  tools?: string[];
}

const AUTH_KEY = "metamate_session_v1";
const PUBLIC_KEY = "metamate_public_v1";

const AUTH_GREETING =
  "Hi, I'm MetaMate — your metadata assistant. I can search the registry, " +
  "read and explore datasets you have access to, look species and strains " +
  "up in NCBI Taxonomy, and flag missing required fields, then propose " +
  "enrichments. I never change your data — every suggestion is yours to " +
  "apply. What can I help you find?";

const PUBLIC_GREETING =
  "Hi, I'm MetaMate 👋 — the Allen BioData Registry's open-data guide. I can " +
  "help you discover PUBLISHED datasets, explain the metadata schema, and look " +
  "species/strains up in NCBI Taxonomy. I only ever show published, public data " +
  "— nothing private. What would you like to explore?";

const TOOL_LABELS: Record<string, string> = {
  search_assets: "🔍 searched the registry",
  public_search: "🔍 searched published data",
  get_asset: "📄 read an asset",
  get_revisions: "🕓 read version history",
  required_fields: "📋 checked required fields",
  lookup_ontology: "🧬 NCBI Taxonomy lookup",
};

function greetingMsg(publicMode: boolean): Msg {
  return { role: "assistant", content: publicMode ? PUBLIC_GREETING : AUTH_GREETING };
}

function loadMessages(key: string, publicMode: boolean): Msg[] {
  try {
    const raw = sessionStorage.getItem(key);
    if (raw) return JSON.parse(raw);
  } catch {
    /* ignore */
  }
  return [greetingMsg(publicMode)];
}

function pageContext(pathname: string, publicMode: boolean): { note: string; starters: string[] } {
  if (publicMode) {
    return {
      note: "The visitor is on the public open-data portal and may only access PUBLISHED datasets.",
      starters: [
        "What kinds of published datasets are available?",
        "Find published multiplane-ophys data",
        "What's the NCBI taxonomy ID for the house mouse?",
        "What fields describe a subject?",
      ],
    };
  }
  const assetMatch = pathname.match(/^\/asset\/([^/]+)/);
  if (assetMatch) {
    const id = assetMatch[1];
    return {
      note: `The user is currently viewing the asset detail page for asset id ${id}.`,
      starters: [
        "What required fields is this asset's subject missing?",
        "Resolve this subject's species/strain in NCBI Taxonomy",
        "Summarize this asset's metadata",
      ],
    };
  }
  const map: Record<string, string> = {
    "/": "the Dashboard",
    "/search": "the Search page",
    "/create": "the Register Asset page",
    "/collections": "the Collections page",
    "/admin": "the Admin page",
    "/sharing": "the Sharing page",
    "/metamate": "the MetaMate explorer page",
  };
  const page = map[pathname] || "a page in the registry";
  return {
    note: `The user is currently on ${page}.`,
    starters: [
      "Find multiplane-ophys datasets and summarize what you see",
      "What's the NCBI taxonomy ID for the house mouse?",
      "What fields are required for a subject?",
      "Show me SmartSPIM datasets",
    ],
  };
}

function splitProposals(text: string): { prose: string; proposals: string[] } {
  const proposals: string[] = [];
  const lines = text.split("\n");
  const kept: string[] = [];
  for (const line of lines) {
    const idx = line.indexOf("PROPOSE_CHANGE:");
    if (idx >= 0) {
      proposals.push(line.slice(idx + "PROPOSE_CHANGE:".length).trim());
      const before = line.slice(0, idx).trim();
      if (before) kept.push(before);
    } else {
      kept.push(line);
    }
  }
  return { prose: kept.join("\n").trim(), proposals };
}

function prettyProposal(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

export default function MetaMateChat({
  user,
  fullPage = false,
  publicMode = false,
}: {
  user: string | null;
  fullPage?: boolean;
  publicMode?: boolean;
}) {
  const storeKey = publicMode ? PUBLIC_KEY : AUTH_KEY;
  const [messages, setMessages] = useState<Msg[]>(() => loadMessages(storeKey, publicMode));
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);
  const location = useLocation();
  const ctx = pageContext(location.pathname, publicMode);

  useEffect(() => {
    try {
      sessionStorage.setItem(storeKey, JSON.stringify(messages));
    } catch {
      /* ignore */
    }
  }, [messages, storeKey]);

  // Clear authenticated memory on sign-out. (Public memory is independent.)
  useEffect(() => {
    if (!publicMode && !user) {
      sessionStorage.removeItem(AUTH_KEY);
      setMessages([greetingMsg(false)]);
    }
  }, [user, publicMode]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  async function send(textOverride?: string) {
    const text = (textOverride ?? input).trim();
    if (!text || busy) return;
    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: text }]);
    setInput("");

    try {
      const prior = messages
        .slice(-10)
        .map((m) => ({ role: m.role, content: m.content }));
      while (prior.length && prior[0].role === "assistant") prior.shift();
      const sent = `[Context: ${ctx.note}] ${text}`;
      const call = () =>
        publicMode ? api.publicAgentChat(sent, prior) : api.agentChat(sent, prior);
      let resp: any;
      try {
        resp = await call();
      } catch {
        // One automatic retry — the first call may have cold-started the
        // backend; the second is warm and fast.
        resp = await call();
      }
      const reply = resp.reply || resp.message || "(no response)";
      setMessages((m) => [
        ...m,
        { role: "assistant", content: reply, tools: resp.tools_used || [] },
      ]);
    } catch {
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content:
            "I couldn't reach my backend just now — give me a moment and try again.",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  function clearChat() {
    sessionStorage.removeItem(storeKey);
    setMessages([greetingMsg(publicMode)]);
  }

  return (
    <div className={"mm-chat" + (fullPage ? " mm-chat-full" : "")}>
      <div className="mm-messages">
        {messages.map((m, i) => (
          <div key={i} className={`mm-msg mm-${m.role}`}>
            {m.role === "assistant" && <MetaMateAvatar size={fullPage ? 30 : 24} />}
            <div className="mm-bubble">
              {(() => {
                if (m.role === "user") return <span>{m.content}</span>;
                const { prose, proposals } = splitProposals(m.content);
                return (
                  <>
                    {prose && <div className="mm-prose">{prose}</div>}
                    {proposals.map((p, j) => (
                      <div key={j} className="mm-proposal">
                        <div className="mm-proposal-tag">✨ Proposed enrichment · review &amp; apply manually</div>
                        <pre>{prettyProposal(p)}</pre>
                      </div>
                    ))}
                    {m.tools && m.tools.length > 0 && (
                      <div className="mm-tools">
                        {[...new Set(m.tools)].map((t) => (
                          <span key={t} className="mm-tool-chip">{TOOL_LABELS[t] || t}</span>
                        ))}
                      </div>
                    )}
                  </>
                );
              })()}
            </div>
          </div>
        ))}
        {busy && (
          <div className="mm-msg mm-assistant">
            <MetaMateAvatar size={fullPage ? 30 : 24} />
            <div className="mm-bubble mm-typing"><span></span><span></span><span></span></div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {messages.length <= 1 && !busy && (
        <div className="mm-starters">
          {ctx.starters.map((s) => (
            <button key={s} onClick={() => send(s)}>{s}</button>
          ))}
        </div>
      )}

      <form
        className="mm-input"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={publicMode ? "Ask about published datasets…" : "Ask MetaMate to find or explore datasets…"}
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()} aria-label="Send">➤</button>
      </form>
      <div className="mm-footnote">
        {publicMode
          ? "MetaMate shows published data only — and never changes anything."
          : "MetaMate proposes — it never changes your data."}
        <button className="mm-clear-link" onClick={clearChat}>Clear conversation</button>
      </div>
    </div>
  );
}
