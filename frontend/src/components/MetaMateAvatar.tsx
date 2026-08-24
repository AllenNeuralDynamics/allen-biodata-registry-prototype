/**
 * MetaMate — the Allen BioData Registry metadata assistant mascot.
 *
 * A friendly robot rendered as inline SVG in the Allen palette
 * (navy #0F1419 visor, periwinkle #6366F1 body, pink #E7157B accents)
 * so it stays crisp at any size and never depends on an external image.
 * Holds a magnifier (it "looks things up" — e.g. NCBI Taxonomy) and shows
 * a neuron/snowflake mark on its chest (neuroscience + the registry).
 */
export default function MetaMateAvatar({
  size = 40,
  glow = false,
}: {
  size?: number;
  glow?: boolean;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      role="img"
      aria-label="MetaMate assistant"
      style={glow ? { filter: "drop-shadow(0 0 6px rgba(99,102,241,.55))" } : undefined}
    >
      <defs>
        <linearGradient id="mmBody" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#eef0ff" />
          <stop offset="100%" stopColor="#c7caff" />
        </linearGradient>
        <linearGradient id="mmVisor" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#1b2440" />
          <stop offset="100%" stopColor="#0F1419" />
        </linearGradient>
      </defs>

      {/* antenna */}
      <line x1="50" y1="16" x2="50" y2="6" stroke="#6366F1" strokeWidth="3" strokeLinecap="round" />
      <circle cx="50" cy="6" r="4.5" fill="#E7157B" />

      {/* head */}
      <rect x="22" y="16" width="56" height="46" rx="18" fill="url(#mmBody)" stroke="#6366F1" strokeWidth="2.5" />

      {/* ear / headphones */}
      <rect x="14" y="30" width="9" height="18" rx="4.5" fill="#6366F1" />
      <rect x="77" y="30" width="9" height="18" rx="4.5" fill="#6366F1" />

      {/* visor */}
      <rect x="29" y="24" width="42" height="30" rx="14" fill="url(#mmVisor)" />
      {/* eyes */}
      <circle cx="42" cy="38" r="4.2" fill="#7dd3fc" />
      <circle cx="58" cy="38" r="4.2" fill="#7dd3fc" />
      {/* smile */}
      <path d="M41 45 Q50 52 59 45" stroke="#6366F1" strokeWidth="2.6" fill="none" strokeLinecap="round" />

      {/* body */}
      <rect x="30" y="64" width="40" height="26" rx="11" fill="url(#mmBody)" stroke="#6366F1" strokeWidth="2.5" />
      {/* chest neuron/snowflake mark */}
      <g stroke="#6366F1" strokeWidth="2" strokeLinecap="round">
        <line x1="50" y1="71" x2="50" y2="83" />
        <line x1="44.5" y1="74" x2="55.5" y2="80" />
        <line x1="55.5" y1="74" x2="44.5" y2="80" />
      </g>
      <circle cx="50" cy="77" r="2.4" fill="#E7157B" />

      {/* magnifier in hand */}
      <circle cx="80" cy="70" r="8" fill="none" stroke="#E7157B" strokeWidth="3" />
      <line x1="86" y1="76" x2="92" y2="82" stroke="#E7157B" strokeWidth="3.4" strokeLinecap="round" />
    </svg>
  );
}
