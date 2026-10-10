/**
 * Find a vehicle when nobody ever read its plate.
 *
 * Every other search on this platform starts by typing a registration. On this estate
 * most vehicles never produce one — the plate occupies too few pixels for any
 * recogniser to resolve — so an investigator who saw a vehicle on one camera and wants
 * it found on the others has had nothing to type.
 *
 * Here they point at it instead. Pick any vehicle out of the footage and the estate is
 * searched by what it looks like: a 76-dimension CIELAB-and-edge fingerprint, its
 * class, its colour, and whether the hop is physically possible.
 *
 * The screen is built around one rule, and it is a limit rather than a feature:
 *
 *   **Everything found here is a Candidate, always.** Not when the score is low —
 *   always. The journey screen may return Probable because a confirmed plate read
 *   anchors the chain and the rest inherits its credibility. Nothing anchors this, so
 *   nothing can inherit anything. Two vehicles that look alike, with no registration
 *   tying either to anything, are two vehicles that look alike.
 *
 * So both crops sit side by side at a size a person can actually judge, the score is
 * broken into the five components that produced it, and the caveat is on the screen
 * rather than behind a tooltip.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import {
  api,
  mediaUrl,
  type SightingBrief,
  type VehicleMatch,
  type VehicleSearchResult,
} from "../lib/api";
import { TierBadge, TierLegend } from "../components/tiers";
import { ErrorBox } from "../components/ui";
import { WhatAmILookingAt } from "../components/explain";

const CLASSES = ["", "car", "bus", "truck", "motorcycle"] as const;

function isoLocal(d: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

function colourLabel(lab: Record<string, number> | null | undefined) {
  if (!lab) return null;
  const { L, a, b } = lab as { L?: number; a?: number; b?: number };
  if (L == null) return null;
  // Named roughly, with the measurement beside it. A name alone over-claims on a
  // 76-float descriptor; the numbers alone tell an operator nothing.
  const chroma = Math.hypot(a ?? 0, b ?? 0);
  const name =
    chroma < 8 ? (L > 65 ? "light / white" : L < 35 ? "dark" : "grey") : (b ?? 0) > 8 ? "warm" : "cool";
  return `${name} · L ${L.toFixed(0)}`;
}

function SightingCard({
  sighting,
  selected,
  onPick,
}: {
  sighting: SightingBrief;
  selected: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onPick}
      aria-pressed={selected}
      className={`text-left panel p-2 flex flex-col gap-1.5 transition-colors ${
        selected ? "ring-2 ring-accent" : "hover:bg-ink-700"
      }`}
    >
      {sighting.crop_url ? (
        <img
          src={mediaUrl(sighting.crop_url)}
          alt={`${sighting.vehicle_class} seen at ${sighting.camera_name}`}
          loading="lazy"
          className="w-full h-20 object-cover rounded border border-edge bg-ink-900"
        />
      ) : (
        <div className="w-full h-20 rounded border border-dashed border-edge grid place-items-center t-micro text-muted">
          no crop
        </div>
      )}
      <div className="min-w-0">
        <div className="t-micro truncate font-medium">{sighting.camera_name}</div>
        <div className="t-micro text-muted num">
          {new Date(sighting.observed_at_utc).toLocaleTimeString("en-IN", {
            hour: "2-digit",
            minute: "2-digit",
          })}{" "}
          · {sighting.vehicle_class}
        </div>
        {/* Shown because its absence is the point of the screen. */}
        <div className="t-micro mono mt-0.5">
          {sighting.plate_read ? (
            <span className="text-fg3">{sighting.plate_read}</span>
          ) : (
            <span className="text-muted">no plate read</span>
          )}
        </div>
      </div>
    </button>
  );
}

function ScoreBar({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="t-micro text-muted w-[5.5rem] shrink-0">{label}</span>
      <div className="flex-1 h-1 bg-ink-700 rounded overflow-hidden">
        <div className="h-full bg-accent" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
      </div>
      <span className="t-micro num w-8 text-right">{value.toFixed(2)}</span>
    </div>
  );
}

function MatchCard({ match, anchor }: { match: VehicleMatch; anchor: SightingBrief }) {
  return (
    <li className="panel p-3 flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="t-section truncate">{match.camera_name}</div>
          <div className="t-micro mono text-muted">{match.camera_ref}</div>
        </div>
        <TierBadge tier="candidate" score={match.score.total} />
      </div>

      {/* Both crops, always. A fingerprint match that cannot be looked at is an
          assertion, and two eyes on two photographs is the most useful thing a human
          adds to this. */}
      <div className="grid grid-cols-2 gap-1.5">
        {(
          [
            ["You picked", mediaUrl(anchor.crop_url) ?? null],
            ["Found here", mediaUrl(match.crop_url) ?? null],
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

      <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        {[
          ["Seen at", new Date(match.observed_at_utc).toLocaleString("en-IN")],
          ["From the anchor", `${Math.round(match.seconds_from_seed / 60)} min`],
          ["Distance", `${(match.distance_m / 1000).toFixed(1)} km`],
          ["Implied speed", `${match.implied_speed_kmph.toFixed(0)} km/h`],
          ["Appearance distance", match.appearance_distance.toFixed(4)],
          ["Class", match.vehicle_class],
          ["Colour", colourLabel(match.dominant_colour_lab) ?? "—"],
          ["Crop quality", match.quality_score.toFixed(2)],
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
          <ScoreBar label="Appearance" value={match.score.appearance} />
          <ScoreBar label="Crop quality" value={match.score.quality} />
          <ScoreBar label="Class" value={match.score.class} />
          <ScoreBar label="Colour" value={match.score.colour} />
          <ScoreBar label="Plausibility" value={match.score.plausibility} />
        </div>
      </details>
    </li>
  );
}

export default function VehicleSearchPage() {
  const [from, setFrom] = useState(() => isoLocal(new Date(Date.now() - 2 * 864e5)));
  const [to, setTo] = useState(() => isoLocal(new Date(Date.now() + 36e5)));
  const [vehicleClass, setVehicleClass] = useState("");
  const [purpose, setPurpose] = useState("");
  const [picked, setPicked] = useState<SightingBrief | null>(null);
  const [result, setResult] = useState<VehicleSearchResult | null>(null);

  const window_ = useMemo(
    () => ({ from: new Date(from).toISOString(), to: new Date(to).toISOString() }),
    [from, to],
  );

  const browse = useQuery({
    queryKey: ["sightings", window_.from, window_.to, vehicleClass],
    queryFn: () =>
      api.browseSightings({
        from: window_.from,
        to: window_.to,
        vehicle_class: vehicleClass || undefined,
        limit: 48,
      }),
  });

  const search = useMutation({
    mutationFn: () => {
      if (!picked) throw new Error("pick a vehicle first");
      return api.findSimilar(
        picked.sighting_id,
        purpose.trim() || "appearance search from the vehicle search screen",
        window_.from,
        window_.to,
      );
    },
    onSuccess: setResult,
  });

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <header className="px-4 py-2.5 border-b border-edge">
        <h1 className="t-title">Find a vehicle</h1>
        <p className="t-micro text-muted">
          Search the estate by what a vehicle looks like, with no registration at all
        </p>
      </header>

      <WhatAmILookingAt
        what="Every vehicle the detector recorded, whether or not its plate was ever readable — pick one and the estate is searched for the same vehicle elsewhere."
        data="Vehicle sightings with a 76-dimension appearance fingerprint, searched with a pgvector index and bounded by the same physical-plausibility rule the journey screen uses."
        limits="Everything found here is a Candidate and none of it can become anything else: no registration anchors the search. The measured false-link rate at this threshold is 0.055 and precision is 0.356 — roughly two proposals in three are wrong, which is why both crops are always shown."
      />

      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-3">
        <section className="panel p-3">
          <div className="flex flex-wrap gap-2 items-end">
            <label className="flex flex-col">
              <span className="t-micro uppercase tracking-wider text-muted">From</span>
              <input
                type="datetime-local"
                className="input"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
              />
            </label>
            <label className="flex flex-col">
              <span className="t-micro uppercase tracking-wider text-muted">To</span>
              <input
                type="datetime-local"
                className="input"
                value={to}
                onChange={(e) => setTo(e.target.value)}
              />
            </label>
            <label className="flex flex-col">
              <span className="t-micro uppercase tracking-wider text-muted">Vehicle type</span>
              <select
                className="input"
                value={vehicleClass}
                onChange={(e) => setVehicleClass(e.target.value)}
              >
                {CLASSES.map((c) => (
                  <option key={c || "any"} value={c}>
                    {c || "any"}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col flex-1 min-w-[16rem]">
              <span className="t-micro uppercase tracking-wider text-muted">
                Purpose — recorded in the audit ledger before the search runs
              </span>
              <input
                className="input"
                value={purpose}
                onChange={(e) => setPurpose(e.target.value)}
                placeholder="FIR 123/2026 — vehicle seen leaving the scene, no plate visible"
              />
            </label>
            <button
              type="button"
              className="btn btn-primary h-[34px]"
              disabled={!picked || search.isPending}
              onClick={() => search.mutate()}
            >
              {search.isPending ? "Searching…" : "Find this vehicle"}
            </button>
          </div>
          {browse.data ? (
            <p className="t-micro text-muted mt-2">{browse.data.note}</p>
          ) : null}
          {browse.error ? <ErrorBox error={browse.error} /> : null}
          {search.error ? <ErrorBox error={search.error} /> : null}
        </section>

        <section>
          <h2 className="t-section mb-2">
            Pick a vehicle
            {picked ? (
              <span className="t-micro text-muted font-normal ml-2">
                selected: {picked.vehicle_class} at {picked.camera_name}
              </span>
            ) : (
              <span className="t-micro text-muted font-normal ml-2">
                any of these — a readable plate is not required
              </span>
            )}
          </h2>
          {browse.isLoading ? (
            <div className="grid gap-2 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
              {Array.from({ length: 12 }, (_, i) => (
                <div key={i} className="skeleton h-32" />
              ))}
            </div>
          ) : browse.data?.sightings.length ? (
            <div className="grid gap-2 grid-cols-2 sm:grid-cols-3 lg:grid-cols-6">
              {browse.data.sightings.map((s) => (
                <SightingCard
                  key={s.sighting_id}
                  sighting={s}
                  selected={picked?.sighting_id === s.sighting_id}
                  onPick={() => {
                    setPicked(s);
                    setResult(null);
                  }}
                />
              ))}
            </div>
          ) : (
            <p className="t-small text-muted panel p-3">
              No vehicle sightings in this window. Sightings are written by the vehicle
              detection pipeline; a window before it ran will be empty.
            </p>
          )}
        </section>

        {result ? (
          <section className="flex flex-col gap-2.5">
            <header className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="t-section">
                Found elsewhere
                <span className="t-micro text-muted font-normal ml-2 num">
                  {result.matches.length} candidate
                  {result.matches.length === 1 ? "" : "s"} · {result.query_ms.toFixed(0)} ms
                </span>
              </h2>
              <TierLegend compact />
            </header>

            <p className="t-small text-fg2 panel p-3 border-l-2" style={{ borderColor: "rgb(var(--tier-candidate))" }}>
              {result.caveat}
            </p>

            {result.matches.length === 0 ? (
              <p className="t-small text-muted panel p-3">
                Nothing matched closely enough to propose. The gate is a cosine distance
                of <span className="mono num">{result.max_distance}</span>, chosen
                because the measured false-link rate there is 0.055 — a looser gate was
                measured at 0.834 and would fill this list with wrong answers.
              </p>
            ) : (
              <ul className="grid gap-2.5 md:grid-cols-2 xl:grid-cols-3">
                {result.matches.map((m) => (
                  <MatchCard key={m.sighting_id} match={m} anchor={result.anchor} />
                ))}
              </ul>
            )}

            <footer className="t-micro text-muted flex flex-wrap gap-x-4">
              <span>
                fingerprint <span className="mono">{result.appearance_kind}</span>
              </span>
              <span>
                gate <span className="mono num">{result.max_distance}</span>
              </span>
              <span>
                score floor <span className="mono num">{result.min_score}</span>
              </span>
              <span>
                anchor{" "}
                <span className="mono">
                  {result.anchor.plate_read ?? "no plate read"}
                </span>
              </span>
            </footer>
          </section>
        ) : null}
      </div>
    </div>
  );
}
