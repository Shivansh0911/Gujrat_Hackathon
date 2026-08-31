import { FormEvent, useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useMutation } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api, type JourneyResult } from "../lib/api";
import { circlePolygon } from "../lib/map";
import { useMapLibre } from "../lib/useMap";
import { Badge, Empty, ErrorBox, ProvenanceBadge, SourceBadge } from "../components/ui";

function isoLocal(d: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Common reasons for tracing a vehicle, as one click each.
 *
 * Purpose stays mandatory: it is written to the audit ledger *before* the query runs,
 * so that a search returning nothing is recorded exactly like one returning a route.
 * The case that control exists for is someone searching a plate they should not be,
 * and that case is invisible if the field can be skipped.
 *
 * What was worth fixing is the friction, not the requirement. Typing a sentence before
 * every search is what makes an officer resent the control; picking a reason and adding
 * a case number is not. Each preset ends mid-sentence on purpose, so the natural next
 * action is to type the reference that makes it specific.
 */
const PURPOSE_PRESETS = [
  "FIR reference — vehicle trace",
  "Stolen vehicle enquiry — case ",
  "Traffic offence follow-up — challan ",
  "Missing person enquiry — case ",
  "Court-directed enquiry — order ",
];

export default function JourneyPage() {
  const [params] = useSearchParams();
  const { ref: mapContainerRef, mapRef } = useMapLibre();
  const markersRef = useRef<maplibregl.Marker[]>([]);

  const [plate, setPlate] = useState(params.get("plate") ?? "");
  const [from, setFrom] = useState(isoLocal(new Date(Date.now() - 7 * 864e5)));
  const [to, setTo] = useState(isoLocal(new Date(Date.now() + 36e5)));
  const [purpose, setPurpose] = useState("");
  const [activeHop, setActiveHop] = useState<number | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exported, setExported] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: () =>
      api.journey(
        plate.trim().toUpperCase(),
        new Date(from).toISOString(),
        new Date(to).toISOString(),
        purpose.trim(),
      ),
  });
  const result = run.data;

  // ---- map ----------------------------------------------------------------

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !result) return;

    const draw = () => {
      const hops = result.hops;
      const line = {
        type: "FeatureCollection" as const,
        features: hops.length > 1
          ? [{
              type: "Feature" as const,
              properties: {},
              geometry: {
                type: "LineString" as const,
                coordinates: hops.map((h) => [h.lon, h.lat]),
              },
            }]
          : [],
      };

      // Dashed spurs to cameras on the corridor that saw nothing. This is the
      // honest half of the output: a gap is a finding, not an omission.
      const gapLines = {
        type: "FeatureCollection" as const,
        features: result.coverage_gaps.flatMap((g) => {
          const anchor = hops.find((h) => h.seq === g.after_seq);
          if (!anchor) return [];
          return [{
            type: "Feature" as const,
            properties: { label: g.reason },
            geometry: {
              type: "LineString" as const,
              coordinates: [[anchor.lon, anchor.lat], [g.lon, g.lat]],
            },
          }];
        }),
      };

      const circles = {
        type: "FeatureCollection" as const,
        features: hops
          .filter((h) => (h.confidence_radius_m ?? 0) > 500)
          .map((h) => ({
            type: "Feature" as const,
            properties: {},
            geometry: circlePolygon(h.lon, h.lat, h.confidence_radius_m!),
          })),
      };

      const points = {
        type: "FeatureCollection" as const,
        features: hops.map((h) => ({
          type: "Feature" as const,
          properties: { seq: h.seq, exact: h.evidence_type === "anpr_exact" ? 1 : 0 },
          geometry: { type: "Point" as const, coordinates: [h.lon, h.lat] },
        })),
      };

      const gapPoints = {
        type: "FeatureCollection" as const,
        features: result.coverage_gaps.map((g) => ({
          type: "Feature" as const,
          properties: { ref: g.camera_ref },
          geometry: { type: "Point" as const, coordinates: [g.lon, g.lat] },
        })),
      };

      const setOrAdd = (id: string, data: GeoJSON.FeatureCollection, add: () => void) => {
        const src = map.getSource(id) as maplibregl.GeoJSONSource | undefined;
        if (src) src.setData(data as never);
        else add();
      };

      setOrAdd("j-circles", circles as never, () => {
        map.addSource("j-circles", { type: "geojson", data: circles as never });
        map.addLayer({
          id: "j-circles-fill", type: "fill", source: "j-circles",
          paint: { "fill-color": "#4da3ff", "fill-opacity": 0.08 },
        });
        map.addLayer({
          id: "j-circles-line", type: "line", source: "j-circles",
          paint: { "line-color": "#4da3ff", "line-opacity": 0.4, "line-width": 1, "line-dasharray": [2, 2] },
        });
      });

      setOrAdd("j-gaps", gapLines as never, () => {
        map.addSource("j-gaps", { type: "geojson", data: gapLines as never });
        map.addLayer({
          id: "j-gaps-line", type: "line", source: "j-gaps",
          paint: { "line-color": "#f0a13a", "line-width": 2, "line-dasharray": [1, 2], "line-opacity": 0.8 },
        });
      });

      setOrAdd("j-line", line as never, () => {
        map.addSource("j-line", { type: "geojson", data: line as never });
        map.addLayer({
          id: "j-line-layer", type: "line", source: "j-line",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#4da3ff", "line-width": 3, "line-opacity": 0.9 },
        });
      });

      setOrAdd("j-gap-points", gapPoints as never, () => {
        map.addSource("j-gap-points", { type: "geojson", data: gapPoints as never });
        map.addLayer({
          id: "j-gap-points-layer", type: "circle", source: "j-gap-points",
          paint: {
            "circle-color": "#0b0f14", "circle-radius": 5,
            "circle-stroke-width": 2, "circle-stroke-color": "#f0a13a",
          },
        });
      });

      setOrAdd("j-points", points as never, () => {
        map.addSource("j-points", { type: "geojson", data: points as never });
        map.addLayer({
          id: "j-points-layer", type: "circle", source: "j-points",
          paint: {
            "circle-color": ["case", ["==", ["get", "exact"], 1], "#31c48d", "#f0a13a"],
            "circle-radius": 9, "circle-stroke-width": 2, "circle-stroke-color": "#0b0f14",
          },
        });
        // Hop numbers are HTML markers, not a symbol layer: a `text-field` layer
        // requires the style to declare a `glyphs` font endpoint, and our basemap is
        // raster tiles precisely so the console needs no font service on an isolated
        // government network.
      });

      // Replace any markers from a previous search before adding this one's.
      markersRef.current.forEach((m) => m.remove());
      markersRef.current = hops.map((h) => {
        const el = document.createElement("div");
        el.textContent = String(h.seq);
        el.className =
          "grid place-items-center w-[22px] h-[22px] rounded-full text-[11px] " +
          "font-semibold text-ink-900 cursor-pointer";
        el.style.background = h.evidence_type === "anpr_exact" ? "#31c48d" : "#f0a13a";
        el.style.border = "2px solid #0b0f14";
        el.title = `${h.camera_name} — ${new Date(h.observed_at_utc).toLocaleString()}`;
        return new maplibregl.Marker({ element: el }).setLngLat([h.lon, h.lat]).addTo(map);
      });

      if (hops.length) {
        const bounds = new maplibregl.LngLatBounds();
        hops.forEach((h) => bounds.extend([h.lon, h.lat]));
        result.coverage_gaps.forEach((g) => bounds.extend([g.lon, g.lat]));
        map.fitBounds(bounds, { padding: 90, maxZoom: 12, duration: 700 });
      }
    };

    if (map.isStyleLoaded()) draw();
    else map.once("load", draw);
  }, [result]);

  async function exportEvidence() {
    if (!result) return;
    setExporting(true);
    setExported(null);
    try {
      const { blob, auditSeq } = await api.exportJourney(
        result.plate,
        new Date(from).toISOString(),
        new Date(to).toISOString(),
        purpose.trim(),
      );
      // The download is triggered from a blob URL rather than a link to the endpoint,
      // so the browser never issues an unauthenticated GET for a signed document.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `setu-evidence-${result.plate}-${auditSeq ?? "export"}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setExported(auditSeq);
    } finally {
      setExporting(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    setActiveHop(null);
    run.mutate();
  }

  return (
    <div className="h-full flex flex-col">
      {/* ---- query bar ---- */}
      <form onSubmit={submit} className="p-3 border-b border-edge bg-ink-800 flex gap-3 items-end flex-wrap">
        <div className="w-44">
          <label className="label">Registration number</label>
          <input
            className="input mono uppercase"
            value={plate}
            onChange={(e) => setPlate(e.target.value)}
            placeholder="GJ01AB1234"
            required
          />
        </div>
        <div className="w-52">
          <label className="label">From</label>
          <input type="datetime-local" className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="w-52">
          <label className="label">To</label>
          <input type="datetime-local" className="input" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
        <div className="flex-1 min-w-[16rem]">
          <label className="label">
            Purpose <span className="text-warn normal-case">— recorded in the audit ledger before the search runs</span>
          </label>
          <input
            className="input"
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
            placeholder="FIR 123/2026 — vehicle trace requested by Investigating Officer"
            minLength={8}
            required
          />
          <div className="flex flex-wrap gap-1 mt-1.5">
            {PURPOSE_PRESETS.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setPurpose(p)}
                className="text-[10px] px-2 py-0.5 rounded border border-edge bg-ink-700
                           hover:bg-ink-600 text-muted hover:text-slate-200 transition-colors"
              >
                {p.trim()}
              </button>
            ))}
          </div>
        </div>
        <button className="btn btn-primary h-[34px]" disabled={run.isPending}>
          {run.isPending ? "Searching…" : "Trace vehicle"}
        </button>
      </form>

      {/* Column on phones and tablets, row from `lg` up. As a row at 375px the 26rem results
          panel consumed the whole viewport and the map's flex-1 resolved to zero --
          the same defect the GIS page had, and the reason two floating cards appeared
          to be sitting on nothing. */}
      <div className="flex-1 flex flex-col lg:flex-row min-h-0">
        <div className="relative min-w-0 h-[38vh] shrink-0 lg:h-auto lg:flex-1">
          <div ref={mapContainerRef} className="absolute inset-0" />

          {/* Both cards are desktop-only. Overlaid on a 38vh map they covered most of
              it and each other; the same facts are rendered inline at the top of the
              results panel below, where they can wrap. */}
          {result && (
            <div className="hidden lg:block absolute top-3 left-3 panel px-3 py-2 text-xs bg-ink-800/95 space-y-1">
              <div className="flex items-center gap-2">
                <span className="mono text-slate-100 text-sm">{result.plate}</span>
                <Badge tone={result.confidence > 0.7 ? "ok" : "warn"}>
                  confidence {result.confidence.toFixed(2)}
                </Badge>
              </div>
              <div className="text-muted">
                {result.hops.length} hop{result.hops.length !== 1 ? "s" : ""} ·{" "}
                {(result.total_distance_m / 1000).toFixed(1)} km ·{" "}
                {result.duration_s > 0 ? `${(result.duration_s / 60).toFixed(1)} min` : "single sighting"}
              </div>
              <div className="text-muted">query {result.query_ms.toFixed(0)} ms</div>
            </div>
          )}

          {result && (result.coverage_gaps.length > 0 || result.rejected.length > 0) && (
            <div className="hidden lg:block absolute bottom-3 left-3 panel px-3 py-2 text-[11px] bg-ink-800/95 space-y-1 max-w-sm">
              {result.coverage_gaps.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="w-4 border-t-2 border-dashed border-warn mt-1.5 shrink-0" />
                  <span className="text-muted">
                    Dashed: {result.coverage_gaps.length} camera(s) on this route produced
                    no detection — a coverage gap, not an absence of the vehicle.
                  </span>
                </div>
              )}
              {result.rejected.length > 0 && (
                <div className="text-muted">
                  {result.rejected.length} candidate sighting(s) rejected as physically
                  implausible — listed in the timeline.
                </div>
              )}
            </div>
          )}
        </div>

        {/* ---- results ---- */}
        <div
          className="w-full lg:w-[26rem] shrink-0 border-t lg:border-t-0 lg:border-l
                     border-edge bg-ink-800 overflow-y-auto flex-1 lg:flex-none"
        >
          {/* Mobile stand-in for the two map overlays hidden above. */}
          {result && (
            <div className="lg:hidden p-3 border-b border-edge text-[11px] space-y-1.5">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="mono text-slate-100 text-sm">{result.plate}</span>
                <Badge tone={result.confidence > 0.7 ? "ok" : "warn"}>
                  confidence {result.confidence.toFixed(2)}
                </Badge>
              </div>
              <div className="text-muted">
                {result.hops.length} hop{result.hops.length !== 1 ? "s" : ""} ·{" "}
                {(result.total_distance_m / 1000).toFixed(1)} km ·{" "}
                {result.duration_s > 0
                  ? `${(result.duration_s / 60).toFixed(1)} min`
                  : "single sighting"}{" "}
                · query {result.query_ms.toFixed(0)} ms
              </div>
              {result.coverage_gaps.length > 0 && (
                <div className="text-muted">
                  Dashed on the map: {result.coverage_gaps.length} camera(s) on this
                  route produced no detection — a coverage gap, not an absence of the
                  vehicle.
                </div>
              )}
              {result.rejected.length > 0 && (
                <div className="text-muted">
                  {result.rejected.length} candidate sighting(s) rejected as physically
                  implausible — listed in the timeline.
                </div>
              )}
            </div>
          )}

          {run.isError && <div className="p-3"><ErrorBox error={run.error} /></div>}

          {!result && !run.isPending && (
            <Empty
              title="Trace a vehicle"
              detail="Enter a registration number, a time window and the purpose of the search. The purpose is written to the tamper-evident audit ledger before any result is produced."
            />
          )}

          {result && (
            <Results
              result={result}
              activeHop={activeHop}
              onHover={setActiveHop}
              map={mapRef}
              onExport={exportEvidence}
              exporting={exporting}
              exportedSeq={exported}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function Results({
  result,
  activeHop,
  onHover,
  map,
  onExport,
  exporting,
  exportedSeq,
}: {
  result: JourneyResult;
  activeHop: number | null;
  onHover: (seq: number | null) => void;
  map: React.MutableRefObject<maplibregl.Map | null>;
  onExport: () => void;
  exporting: boolean;
  exportedSeq: string | null;
}) {
  if (!result.hops.length) {
    // These are genuinely different findings and an investigator must be able to
    // tell them apart: one is a dead end, the other is a wrong time window.
    return result.plate_ever_seen ? (
      <Empty
        title="No sightings in this window"
        detail={`${result.plate} has been recorded by this network, but not between the times you selected. Widen the window to locate the sightings that exist.`}
      />
    ) : (
      <Empty
        title="Plate never seen"
        detail={`${result.plate} has never been recorded by any camera in this network. This is not a coverage gap — no sighting of this registration exists in the system at all.`}
      />
    );
  }

  return (
    <div className="divide-y divide-edge">
      <div className="p-3 text-xs text-muted space-y-1">
        <div>
          Purpose recorded: <span className="text-slate-300">{result.purpose}</span>
        </div>
        <div>
          Requested by <span className="text-slate-300">{result.requested_by}</span>
        </div>
        {result.cameras_excluded_no_coordinate > 0 && (
          <div className="text-warn">
            {result.cameras_excluded_no_coordinate} camera(s) excluded from this search:
            no coordinate on record.
          </div>
        )}
      </div>

      <div className="p-3 border-b border-edge space-y-2">
        <button
          className="btn btn-primary w-full"
          onClick={onExport}
          disabled={exporting}
        >
          {exporting ? "Generating signed export…" : "Export signed evidence (PDF)"}
        </button>
        {exportedSeq && (
          <div className="text-[11px] text-ok">
            Exported and recorded at audit entry {exportedSeq}. The PDF carries an
            Ed25519 signature over a manifest of every hop, verifiable without SETU.
          </div>
        )}
        <p className="text-[10px] text-muted leading-snug">
          The export re-runs this reconstruction server-side and is separately audited.
          Producing a distributable evidence document is a more consequential act than
          viewing a route on screen.
        </p>
      </div>

      {result.hops.map((hop) => (
        <div
          key={hop.seq}
          className={`p-3 space-y-2 cursor-pointer transition-colors ${
            activeHop === hop.seq ? "bg-ink-700" : "hover:bg-ink-700/50"
          }`}
          onMouseEnter={() => onHover(hop.seq)}
          onMouseLeave={() => onHover(null)}
          onClick={() => map.current?.flyTo({ center: [hop.lon, hop.lat], zoom: 12 })}
        >
          <div className="flex items-start gap-3">
            <div className="w-7 h-7 rounded-full bg-accent/20 border border-accent/50 grid place-items-center text-xs font-medium shrink-0">
              {hop.seq}
            </div>
            <div className="min-w-0 flex-1">
              <div className="font-medium text-sm truncate">{hop.camera_name}</div>
              <div className="text-[11px] text-muted truncate">{hop.location_text}</div>
              <div className="text-[11px] mono text-slate-300 mt-0.5">
                {new Date(hop.observed_at_utc).toLocaleString()}
              </div>
            </div>
          </div>

          <div className="flex flex-wrap gap-1.5">
            <SourceBadge sourceType={hop.camera_source_type} />
            <ProvenanceBadge
              evidenceType={hop.evidence_type}
              corrections={hop.corrections as Array<Record<string, unknown>>}
              confidence={hop.confidence}
            />
            {(hop.confidence_radius_m ?? 0) > 500 && (
              <Badge tone="warn" title="Camera positioned to a district centroid, not surveyed">
                position ±{(hop.confidence_radius_m! / 1000).toFixed(1)} km
              </Badge>
            )}
            {hop.within_tolerance_only && (
              <Badge tone="warn" title="Only plausible once coordinate uncertainty is allowed for">
                within tolerance only
              </Badge>
            )}
          </div>

          {hop.crop_url && (
            <img
              src={hop.crop_url}
              alt={`Evidence crop for ${hop.plate_read}`}
              className="rounded border border-edge bg-black max-h-24 object-contain"
            />
          )}

          <div className="grid grid-cols-3 gap-2 text-[11px]">
            <Metric label="Read" value={<span className="mono">{hop.plate_read}</span>} />
            <Metric label="PTS" value={<span className="mono">{(hop.pts_ms / 1000).toFixed(1)}s</span>} />
            <Metric
              label="Implied speed"
              value={
                hop.implied_speed_kmph != null
                  ? <span className="mono">{hop.implied_speed_kmph.toFixed(0)} km/h</span>
                  : <span className="text-muted">—</span>
              }
            />
          </div>

          {hop.distance_from_prev_m != null && (
            <div className="text-[11px] text-muted">
              {(hop.distance_from_prev_m / 1000).toFixed(2)} km from hop {hop.seq - 1} in{" "}
              {hop.seconds_from_prev!.toFixed(0)} s
            </div>
          )}
        </div>
      ))}

      {result.coverage_gaps.length > 0 && (
        <div className="p-3 space-y-2">
          <div className="text-xs font-medium text-warn">Coverage gaps</div>
          {result.coverage_gaps.map((g, i) => (
            <div key={i} className="text-[11px] bg-ink-900 rounded px-2 py-1.5">
              <span className="mono text-slate-300">{g.camera_ref}</span>{" "}
              <span className="text-muted">— {g.reason}</span>
            </div>
          ))}
          <p className="text-[10px] text-muted leading-snug">
            These cameras lie on the reconstructed route but recorded nothing in the
            relevant interval. Either the vehicle was missed, or coverage there needs
            attention — both are actionable, and neither is visible in a system that
            only reports what it found.
          </p>
        </div>
      )}

      {result.rejected.length > 0 && (
        <div className="p-3 space-y-2">
          <div className="text-xs font-medium text-bad">Rejected candidates</div>
          {result.rejected.map((r, i) => (
            <div key={i} className="text-[11px] bg-ink-900 rounded px-2 py-1.5">
              <span className="mono text-slate-300">{r.camera_ref}</span>{" "}
              <span className="text-muted">{r.reason}</span>
            </div>
          ))}
          <p className="text-[10px] text-muted leading-snug">
            Shown rather than dropped: an investigator needs to know a candidate was
            considered and why it was excluded.
          </p>
        </div>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-muted text-[10px] uppercase tracking-wide">{label}</div>
      <div className="text-slate-300">{value}</div>
    </div>
  );
}
