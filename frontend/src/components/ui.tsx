/** Small shared primitives. Deliberately plain: this is a control room, not a brochure. */

export function StatusDot({ status }: { status: string }) {
  const colour =
    status === "ACTIVE" ? "bg-ok"
    : status === "DEGRADED" ? "bg-warn"
    : status === "UNREACHABLE" ? "bg-bad"
    : "bg-muted";
  return <span className={`inline-block w-2 h-2 rounded-full ${colour}`} />;
}

export function Badge({
  tone = "muted",
  children,
  title,
}: {
  tone?: "ok" | "warn" | "bad" | "muted" | "accent";
  children: React.ReactNode;
  title?: string;
}) {
  const tones = {
    ok: "bg-ok/15 text-ok border-ok/40",
    warn: "bg-warn/15 text-warn border-warn/40",
    bad: "bg-bad/15 text-bad border-bad/40",
    accent: "bg-accent/15 text-accent border-accent/40",
    muted: "bg-ink-700 text-muted border-edge",
  } as const;
  return (
    <span className={`badge border ${tones[tone]}`} title={title}>
      {children}
    </span>
  );
}

/**
 * Provenance badge for a plate read.
 *
 * A corrected plate is never shown as a clean read: the badge names how many
 * characters were rewritten and the confidence, and the tooltip lists each
 * substitution. An operator acting on a corrected plate must be able to see that.
 */
export function ProvenanceBadge({
  evidenceType,
  corrections,
  confidence,
}: {
  evidenceType: string;
  corrections: Array<Record<string, unknown>>;
  confidence: number;
}) {
  const n = corrections?.length ?? 0;
  const detail = (corrections ?? [])
    .map((c) => `pos ${c.position}: ${c.raw} → ${c.corrected} (conf ${c.confidence})`)
    .join("\n");

  if (evidenceType === "anpr_exact" && n === 0)
    return <Badge tone="ok">ANPR confirmed · {confidence.toFixed(2)}</Badge>;
  if (n > 0)
    return (
      <Badge tone="warn" title={detail}>
        ANPR partial — {n} char{n > 1 ? "s" : ""} corrected — {confidence.toFixed(2)}
      </Badge>
    );
  return <Badge tone="accent">ANPR fuzzy · {confidence.toFixed(2)}</Badge>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-muted text-sm py-6 justify-center">
      <span className="w-3 h-3 border-2 border-muted/40 border-t-accent rounded-full animate-spin" />
      {label ?? "Loading…"}
    </div>
  );
}

export function Empty({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="text-center py-10 px-4">
      <div className="text-slate-300 font-medium">{title}</div>
      {detail && <div className="text-muted text-sm mt-1 max-w-md mx-auto">{detail}</div>}
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="panel border-bad/50 bg-bad/10 p-3 text-sm text-bad">
      {msg}
    </div>
  );
}
