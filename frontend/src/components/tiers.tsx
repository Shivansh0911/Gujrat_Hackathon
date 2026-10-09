/**
 * The three evidence tiers, and the legend that must never be hidden.
 *
 * This file exists so there is exactly one place that decides how a claim about
 * vehicle identity looks. The single most important UI rule in the system is that a
 * Candidate is never rendered in the Confirmed style, and a rule enforced by
 * convention across eleven pages is a rule that will be broken on the twelfth.
 *
 * Each tier differs in more than hue:
 *
 *   Confirmed  solid border, filled, semibold, a filled dot      — a plate was read
 *   Probable   solid border, filled, regular, a half dot         — fuzzy plate, or
 *                                                                  appearance plus
 *                                                                  corroboration
 *   Candidate  dashed border, no fill, lighter, a hollow dot     — appearance alone
 *
 * So the distinction survives a greyscale screenshot, a projector with the contrast
 * wrong, and a reader who cannot separate the hues. Colour is never the only carrier.
 */

export type Tier = "confirmed" | "probable" | "candidate";

const LABEL: Record<Tier, string> = {
  confirmed: "Confirmed",
  probable: "Probable",
  candidate: "Candidate",
};

const BASIS: Record<Tier, string> = {
  confirmed: "A grammar-valid number plate was read above the confidence floor.",
  probable:
    "A fuzzy plate match explained by known character confusions, or a strong appearance match with the vehicle class and colour agreeing.",
  candidate:
    "Appearance match alone. On this project's own footage the measured false-link rate at the shipped threshold is 0.055 and precision is 0.356 — roughly two of three proposals are wrong. Check both crops before acting.",
};

/** The dot. Filled, half, hollow — readable without colour. */
function TierDot({ tier }: { tier: Tier }) {
  if (tier === "confirmed") {
    return <span aria-hidden="true" className="inline-block w-2 h-2 rounded-full bg-current" />;
  }
  if (tier === "probable") {
    return (
      <span
        aria-hidden="true"
        className="inline-block w-2 h-2 rounded-full border border-current"
        style={{ background: "linear-gradient(to right, currentColor 50%, transparent 50%)" }}
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className="inline-block w-2 h-2 rounded-full border border-current border-dashed"
    />
  );
}

export function TierBadge({
  tier,
  score,
  className = "",
}: {
  tier: Tier;
  /** Composite confidence, when the claim carries one. */
  score?: number | null;
  className?: string;
}) {
  return (
    <span
      className={`tier tier-${tier} ${className}`}
      // The basis is on the element rather than only in the legend, so the meaning is
      // one hover away at the point of use.
      title={BASIS[tier]}
    >
      <TierDot tier={tier} />
      <span>{LABEL[tier]}</span>
      {typeof score === "number" ? (
        <span className="mono opacity-80">{score.toFixed(2)}</span>
      ) : null}
    </span>
  );
}

/**
 * The permanent legend.
 *
 * Rendered inline wherever tiers appear, never behind a tooltip or an info icon. An
 * operator who has to discover what a dashed border means has already misread one.
 */
export function TierLegend({ compact = false }: { compact?: boolean }) {
  const tiers: Tier[] = ["confirmed", "probable", "candidate"];
  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-1.5"
      role="note"
      aria-label="Evidence tier legend"
    >
      <span className="t-micro uppercase tracking-wider text-muted">Evidence</span>
      {tiers.map((tier) => (
        <span key={tier} className="flex items-center gap-1.5">
          <TierBadge tier={tier} />
          {compact ? null : (
            <span className="t-micro text-fg3 max-w-[18rem]">
              {tier === "confirmed"
                ? "plate read"
                : tier === "probable"
                  ? "fuzzy plate, or appearance with corroboration"
                  : "appearance alone — not an identification"}
            </span>
          )}
        </span>
      ))}
    </div>
  );
}

/**
 * The caveat that travels with a candidate result.
 *
 * Deliberately a block rather than a footnote. The API returns this text in the
 * response body for the same reason: a caveat that lives only in the interface is gone
 * the moment somebody screenshots the result, and a screenshot is how this will
 * actually reach a meeting.
 */
export function CandidateCaveat({ caveat }: { caveat?: string }) {
  return (
    <div className="panel p-2.5 border-l-2" style={{ borderLeftColor: "rgb(var(--tier-candidate))" }}>
      <div className="flex items-start gap-2">
        <TierBadge tier="candidate" />
        <p className="t-small text-fg3">
          {caveat ??
            "Candidates are appearance matches, not identifications. Each is shown with both crops so it can be checked by eye. Only a plate read confirms a vehicle."}
        </p>
      </div>
    </div>
  );
}

/** ANPR readiness grade, with the same colour-plus-shape discipline. */
export type Grade = "anpr_capable" | "marginal_reaim" | "not_viable" | "insufficient_sample";

const GRADE_CLASS: Record<Grade, string> = {
  anpr_capable: "grade-capable",
  marginal_reaim: "grade-marginal",
  not_viable: "grade-unviable",
  insufficient_sample: "grade-unknown",
};

const GRADE_LABEL: Record<Grade, string> = {
  anpr_capable: "ANPR capable",
  marginal_reaim: "Marginal — re-aim",
  not_viable: "Not viable for ANPR",
  insufficient_sample: "Not yet graded",
};

export function GradeBadge({ grade, title }: { grade: Grade; title?: string }) {
  const cls = GRADE_CLASS[grade] ?? "grade-unknown";
  return (
    <span className={`grade ${cls}`} title={title}>
      {GRADE_LABEL[grade] ?? grade}
    </span>
  );
}

export const GRADE_COLOUR: Record<Grade, string> = {
  anpr_capable: "var(--grade-capable)",
  marginal_reaim: "var(--grade-marginal)",
  not_viable: "var(--grade-unviable)",
  insufficient_sample: "var(--grade-unknown)",
};
