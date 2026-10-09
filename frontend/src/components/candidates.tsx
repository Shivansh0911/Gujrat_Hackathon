/**
 * Candidate hops: tracing a vehicle when the plate is legible once.
 *
 * This is the capability the whole Phase 2 brief exists for, and it is also the one
 * most able to mislead. So the panel is built around the operator's ability to
 * disagree with it:
 *
 *   Both crops are shown side by side, always. A fingerprint match that cannot be
 *   looked at is an assertion, and the single most useful thing a human adds here is
 *   two eyes on two photographs.
 *
 *   The score is broken into its five components rather than shown as one number. A
 *   0.71 that came from a strong colour match and a mediocre fingerprint is a
 *   different proposition from a 0.71 that came the other way round, and only the
 *   breakdown says which.
 *
 *   Accept and reject are recorded in the hash-chained ledger. That is an
 *   accountability control — somebody decided this, and the ledger says who and why —
 *   and over time a labelled dataset for improving the scorer against real operator
 *   judgement rather than against the tracker's own association.
 *
 *   Nothing here can promote a candidate to a confirmed hop. There is deliberately no
 *   control for it: an operator agreeing with a fingerprint is not a plate read, and
 *   offering the promotion would make the three tiers decorative.
 */

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { api, type CandidateHop, type CandidateJourney } from "../lib/api";
import { CandidateCaveat, TierBadge, TierLegend } from "./tiers";

function ScoreBar({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="t-micro text-muted w-[5.5rem] shrink-0">{label}</span>
      <div className="flex-1 h-1 bg-ink-700 rounded overflow-hidden">
        <div
          className="h-full bg-accent"
          style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }}
        />
      </div>
      <span className="t-micro num w-8 text-right">{value.toFixed(2)}</span>
    </div>
  );
}

/** Two crops, side by side, at a size a person can actually judge. */
function CropPair({
  seedUrl,
  candidateUrl,
}: {
  seedUrl: string | null;
  candidateUrl: string | null;
}) {
  return (
    <div className="grid grid-cols-2 gap-1.5">
      {(
        [
          ["Plate read here", seedUrl],
          ["Proposed match", candidateUrl],
        ] as const
      ).map(([label, url]) => (
        <figure key={label} className="min-w-0">
          <figcaption className="t-micro uppercase tracking-wider text-muted mb-0.5">
            {label}
          </figcaption>
          {url ? (
            <img
              src={url}
              alt={`Vehicle crop: ${label}`}
              loading="lazy"
              className="w-full h-24 object-cover rounded border border-edge bg-ink-700"
            />
          ) : (
            <div className="w-full h-24 rounded border border-dashed border-edge grid place-items-center t-micro text-muted">
              no crop stored
            </div>
          )}
        </figure>
      ))}
    </div>
  );
}

function CandidateCard({
  candidate,
  plate,
  purpose,
}: {
  candidate: CandidateHop;
  plate: string;
  purpose: string;
}) {
  const [decided, setDecided] = useState<"accepted" | "rejected" | null>(null);
  const [note, setNote] = useState("");

  const adjudicate = useMutation({
    mutationFn: (accepted: boolean) =>
      api.adjudicate({
        sighting_id: candidate.sighting_id,
        seed_sighting_id: candidate.seed_sighting_id,
        plate,
        accepted,
        purpose: purpose || "operator adjudication from the journey screen",
        note: note || undefined,
      }),
    onSuccess: (_data, accepted) => setDecided(accepted ? "accepted" : "rejected"),
  });

  const colour =
    candidate.dominant_colour_lab == null
      ? null
      : `L ${candidate.dominant_colour_lab.L?.toFixed(0)} a ${candidate.dominant_colour_lab.a?.toFixed(0)} b ${candidate.dominant_colour_lab.b?.toFixed(0)}`;

  return (
    <li className="panel p-3 flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="t-section truncate">{candidate.camera_name}</div>
          <div className="t-micro mono text-muted">{candidate.camera_ref}</div>
        </div>
        <TierBadge tier={candidate.tier} score={candidate.score.total} />
      </div>

      <CropPair seedUrl={candidate.seed_crop_url} candidateUrl={candidate.crop_url} />

      {/* The evidence, as numbers a reviewer can check against the map. */}
      <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        {[
          ["Seen at", new Date(candidate.observed_at_utc).toLocaleString("en-IN")],
          ["From the seed", `${Math.round(candidate.seconds_from_seed)} s`],
          ["Distance", `${(candidate.distance_m / 1000).toFixed(1)} km`],
          ["Implied speed", `${candidate.implied_speed_kmph.toFixed(0)} km/h`],
          ["Appearance distance", candidate.appearance_distance.toFixed(4)],
          ["Crop quality", candidate.quality_score.toFixed(2)],
          ["Class", candidate.vehicle_class],
          ["Colour (CIELAB)", colour ?? "—"],
        ].map(([label, value]) => (
          <div key={String(label)} className="flex justify-between gap-2">
            <dt className="t-micro text-muted">{label}</dt>
            <dd className="t-micro num">{value}</dd>
          </div>
        ))}
      </dl>

      <details className="t-small">
        <summary className="cursor-pointer t-micro uppercase tracking-wider text-muted">
          Why this score
        </summary>
        <div className="flex flex-col gap-1 mt-1.5">
          <ScoreBar label="Appearance" value={candidate.score.appearance} />
          <ScoreBar label="Crop quality" value={candidate.score.quality} />
          <ScoreBar label="Class" value={candidate.score.class} />
          <ScoreBar label="Colour" value={candidate.score.colour} />
          <ScoreBar label="Plausibility" value={candidate.score.plausibility} />
        </div>
      </details>

      {decided ? (
        <p
          className="t-small border-l-2 pl-2"
          style={{
            borderColor:
              decided === "accepted" ? "rgb(var(--ok))" : "rgb(var(--muted))",
          }}
        >
          {decided === "accepted" ? "Accepted" : "Rejected"} — written to the audit
          ledger. The hop remains a {candidate.tier}; only a plate read confirms a
          vehicle.
        </p>
      ) : (
        <div className="flex flex-col gap-1.5">
          <input
            className="input t-small"
            placeholder="What did you see that the scorer did not? (optional)"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            aria-label="Adjudication note"
          />
          <div className="flex gap-1.5">
            <button
              type="button"
              className="btn t-small flex-1"
              disabled={adjudicate.isPending}
              onClick={() => adjudicate.mutate(true)}
            >
              Same vehicle
            </button>
            <button
              type="button"
              className="btn t-small flex-1"
              disabled={adjudicate.isPending}
              onClick={() => adjudicate.mutate(false)}
            >
              Different vehicle
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

export function CandidatePanel({
  result,
  plate,
  purpose,
}: {
  result: CandidateJourney;
  plate: string;
  purpose: string;
}) {
  return (
    <section className="flex flex-col gap-2.5">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="t-section">
          Appearance candidates
          <span className="t-micro text-muted font-normal ml-2 num">
            {result.candidates.length} proposed from {result.seeds.length} plate read
            {result.seeds.length === 1 ? "" : "s"}, searching{" "}
            {result.sightings_searched.toLocaleString("en-IN")} sightings
          </span>
        </h2>
        <TierLegend compact />
      </header>

      <CandidateCaveat caveat={result.caveat} />

      {result.seeds.length === 0 ? (
        <p className="t-small text-muted panel p-3">
          No plate read for {plate} in this window, so there is nothing to anchor an
          appearance search on. A candidate journey needs one legible read to start
          from — that is what makes it a trace rather than a guess.
        </p>
      ) : result.candidates.length === 0 ? (
        <p className="t-small text-muted panel p-3">
          Nothing matched closely enough to propose. The gate is a cosine distance of{" "}
          <span className="mono num">{result.max_candidate_distance}</span>, chosen
          because the measured false-link rate there is 0.055 — a looser gate was
          measured at 0.834 and would fill this list with wrong answers.
        </p>
      ) : (
        <ul className="grid gap-2.5 md:grid-cols-2 xl:grid-cols-3">
          {result.candidates.map((candidate) => (
            <CandidateCard
              key={candidate.sighting_id}
              candidate={candidate}
              plate={plate}
              purpose={purpose}
            />
          ))}
        </ul>
      )}

      <footer className="t-micro text-muted flex flex-wrap gap-x-4">
        <span>
          fingerprint <span className="mono">{result.appearance_kind}</span>
        </span>
        <span>
          analytics <span className="mono">{result.analytics_version}</span>
        </span>
        <span>
          score floor <span className="mono num">{result.min_score}</span>
        </span>
        <span>
          query <span className="mono num">{result.query_ms.toFixed(0)} ms</span>
        </span>
      </footer>
    </section>
  );
}
