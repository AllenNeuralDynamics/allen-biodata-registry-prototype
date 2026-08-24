/**
 * Allen Institute logo lockup used across the registry UI.
 *
 * The official PNG (2497×385) has a transparent background with light/white
 * art, so it reads cleanly on the dark navy header (matching the `.hdr`
 * treatment in the v2 PoC scope document). On light backgrounds it needs a
 * dark plate, which the "plate" variant provides.
 */
type Props = {
  /** Rendered logo height in px. */
  height?: number;
  /** "dark" = on the navy header (no plate; white art on navy);
   *  "plate" = on a light page (logo sits on a navy rounded plate). */
  variant?: "dark" | "plate";
};

export default function AllenLogo({ height = 44, variant = "dark" }: Props) {
  const img = (
    <img
      src="/allen-institute-logo.png"
      alt="Allen Institute"
      style={{ display: "block", height, width: "auto" }}
    />
  );

  if (variant === "plate") {
    return (
      <div
        aria-label="Allen Institute"
        style={{
          background: "#0F1419",
          padding: "10px 16px",
          borderRadius: 8,
          display: "inline-flex",
          alignItems: "center",
        }}
      >
        {img}
      </div>
    );
  }

  return (
    <div aria-label="Allen Institute" style={{ display: "inline-flex", alignItems: "center" }}>
      {img}
    </div>
  );
}
