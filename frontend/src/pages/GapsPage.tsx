import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect } from "react";
import { api } from "../lib/api";
import { circlePolygon } from "../lib/map";
import { useMapLibre } from "../lib/useMap";
import { Badge, Empty, ErrorBox, Spinner } from "../components/ui";

const KIND_LABEL: Record<string, string> = {
  no_coordinate: "No coordinate",
  low_confidence: "Low spatial confidence",
  degraded: "Degraded",
  unreachable: "Unreachable",
};

const KIND_TONE: Record<string, "bad" | "warn" | "muted"> = {
  no_coordinate: "bad",
  low_confidence: "warn",
  degraded: "warn",
  unreachable: "bad",
};

/** Coverage confidence to a colour. Red is not "bad camera", it is "we cannot see". */
function confidenceColour(c: number): string {
  if (c >= 0.85) return "#31c48d";
  if (c >= 0.7) return "#a3d977";
  if (c >= 0.55) return "#f0a13a";
  return "#f05252";
}

export default function GapsPage() {
  const { ref: mapContainerRef, mapRef } = useMapLibre();
  const [kindFilter, setKindFilter] = useState("");
  const [showJourneyGaps, setShowJourneyGaps] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["gap-analysis"],
    queryFn: () => api.gapAnalysis(),
  });

  const gaps = useMemo(
    () => (data?.camera_gaps ?? []).filter((g) => !kindFilter || g.kind === kindFilter),
    [data, kindFilter],
  );

  // ---- overlay ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !data) return;

    const draw = () => {
      // Uncertainty circles for every low-confidence camera. The area of the circle
      // is the honest statement of what we do not know about where it is.
      const circles = {
        type: "FeatureCollection" as const,
        features: data.camera_gaps
          .filter((g) => g.kind === "low_confidence" && g.lat != null && g.lon != null)
          .map((g) => ({
            type: "Feature" as const,
            properties: { ref: g.camera_ref },
            geometry: circlePolygon(g.lon!, g.lat!, g.confidence_radius_m ?? 1000),
          })),
      };

      const journeyPoints = {
        type: "FeatureCollection" as const,
        features: (showJourneyGaps ? data.journey_gaps : []).map((g) => ({
          type: "Feature" as const,
          properties: { ref: g.camera_ref, weight: g.times_implied },
          geometry: { type: "Point" as const, coordinates: [g.lon, g.lat] },
        })),
      };

      const setOrAdd = (id: string, payload: unknown, add: () => void) => {
        const src = map.getSource(id) as maplibregl.GeoJSONSource | undefined;
        if (src) src.setData(payload as never);
        else add();
      };

      setOrAdd("gap-circles", circles, () => {
        map.addSource("gap-circles", { type: "geojson", data: circles as never });
        map.addLayer({
          id: "gap-circles-fill", type: "fill", source: "gap-circles",
          paint: { "fill-color": "#f0a13a", "fill-opacity": 0.12 },
        });
        map.addLayer({
          id: "gap-circles-line", type: "line", source: "gap-circles",
          paint: {
            "line-color": "#f0a13a", "line-width": 1,
            "line-opacity": 0.55, "line-dasharray": [2, 2],
          },
        });
      });

      setOrAdd("gap-journey", journeyPoints, () => {
        map.addSource("gap-journey", { type: "geojson", data: journeyPoints as never });
        map.addLayer({
          id: "gap-journey-layer", type: "circle", source: "gap-journey",
          paint: {
            "circle-color": "#f05252",
            // Radius scales with how often investigations needed this position.
            "circle-radius": ["interpolate", ["linear"], ["get", "weight"], 1, 5, 20, 16],
            "circle-opacity": 0.55,
            "circle-stroke-width": 1.5,
            "circle-stroke-color": "#f05252",
          },
        });
      });
    };

    if (map.isStyleLoaded()) draw();
    else map.once("load", draw);
  }, [data, showJourneyGaps, mapRef]);

  const summary = data?.summary as Record<string, number> | undefined;

  /**
   * Take the report away as a document.
   *
   * The same blob-URL route the evidence export uses, and for the same reason: the
   * endpoint needs the bearer token, so a plain link would issue an unauthenticated
   * GET and download a 401 page named like a report.
   */
  async function download() {
    setDownloading(true);
    setDownloadError(null);
    try {
      const { blob, filename } = await api.exportGapAnalysis();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }

  return (
    // Column on phones and tablets, row from `lg` up. Both children were previously flex items in
    // a row: at 375px the 27rem panel took the whole viewport, the map's flex-1
    // resolved to nothing, and the panel itself was clipped by `main`'s overflow-hidden
    // with no way to scroll to the half hanging off the right edge.
    <div className="h-full flex flex-col lg:flex-row">
      <div className="relative min-w-0 h-[42vh] shrink-0 lg:h-auto lg:flex-1">
        <div ref={mapContainerRef} className="absolute inset-0" />

        {/* The floating legend is desktop-only (`lg` and up). On a phone it would cover a third of a
            map that is already only 42vh tall; the same content is rendered inline in
            the panel below instead, so nothing is lost. */}
        <div className="hidden lg:block absolute bottom-4 right-4 panel p-3 text-xs w-80 bg-ink-800/95 space-y-2">
          <div className="font-medium text-fg">Coverage overlay</div>
          <div className="flex items-center gap-2">
            <span className="w-3.5 h-3.5 rounded-full border border-dashed border-warn/70 bg-warn/15" />
            Positional uncertainty — the circle is what we do not know
          </div>
          <div className="flex items-center gap-2">
            <span className="w-3.5 h-3.5 rounded-full bg-bad/50 border border-bad" />
            Investigations passed here and saw nothing
          </div>
          <label className="flex items-center gap-2 pt-1 text-muted">
            <input
              type="checkbox"
              checked={showJourneyGaps}
              onChange={(e) => setShowJourneyGaps(e.target.checked)}
            />
            show investigation-derived gaps
          </label>
        </div>
      </div>

      <div
        className="w-full lg:w-[27rem] shrink-0 border-t lg:border-t-0 lg:border-l
                   border-edge bg-ink-800 overflow-y-auto flex-1 lg:flex-none"
      >
        <div className="p-3 border-b border-edge">
          <div className="flex items-start justify-between gap-2">
            <div className="font-medium">Coverage gap analysis</div>
            <button
              type="button"
              className="btn text-[11px] py-1 px-2 shrink-0"
              onClick={download}
              disabled={!data || downloading}
            >
              {downloading ? "Preparing…" : "Download report (PDF)"}
            </button>
          </div>
          <p className="text-[11px] text-muted mt-1 leading-snug">
            Where this network cannot see, and why. Gaps are separated by remedy because
            the cost of each differs enormously — a missing coordinate is a pin drop, an
            approximate one needs a survey, a degraded camera needs maintenance on
            capital already spent, and uncovered ground needs procurement.
          </p>
          {downloadError && (
            <p className="text-[11px] text-bad mt-1.5">{downloadError}</p>
          )}
        </div>

        {/* Mobile stand-in for the map legend hidden above. */}
        <label className="lg:hidden flex items-center gap-2 p-3 border-b border-edge text-[11px] text-muted">
          <input
            type="checkbox"
            checked={showJourneyGaps}
            onChange={(e) => setShowJourneyGaps(e.target.checked)}
          />
          show investigation-derived gaps on the map
        </label>

        {isLoading && <Spinner label="Analysing coverage…" />}
        {error && <div className="p-3"><ErrorBox error={error} /></div>}

        {summary && (
          <div className="p-3 border-b border-edge grid grid-cols-2 gap-2">
            <Stat label="Cameras" value={summary.cameras_total} />
            <Stat label="Districts" value={summary.districts_covered} />
            <Stat label="No coordinate" value={summary.no_coordinate} tone="bad" />
            <Stat label="Low confidence" value={summary.low_confidence} tone="warn" />
            <Stat label="Unreachable" value={summary.unreachable} tone="bad" />
            <Stat label="Investigation gaps" value={summary.journey_implied_gaps} tone="warn" />
          </div>
        )}

        {data && (
          <div className="p-3 border-b border-edge">
            <div className="text-xs font-medium mb-2">Coverage confidence by district</div>
            <div className="space-y-1.5">
              {data.districts.map((d) => (
                <div key={d.district} className="text-[11px]">
                  {/* min-w-0 on the label so a long district name shrinks rather
                      than shoving the camera count off the right edge on a phone. */}
                  <div className="flex items-center justify-between gap-2 min-w-0">
                    <span className="text-fg2 truncate min-w-0">{d.district}</span>
                    <span className="text-muted whitespace-nowrap shrink-0">
                      {d.cameras_total} camera{d.cameras_total !== 1 ? "s" : ""} ·{" "}
                      {(d.coverage_confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="h-1.5 bg-ink-900 rounded mt-0.5 overflow-hidden">
                    <div
                      className="h-full rounded"
                      style={{
                        width: `${d.coverage_confidence * 100}%`,
                        background: confidenceColour(d.coverage_confidence),
                      }}
                    />
                  </div>
                  <div className="text-muted mt-0.5">{d.findings.join("; ")}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {data && data.journey_gaps.length > 0 && showJourneyGaps && (
          <div className="p-3 border-b border-edge">
            <div className="text-xs font-medium text-bad mb-1">
              Investigation-derived gaps
            </div>
            <p className="text-[11px] text-muted mb-2 leading-snug">
              Cameras that were in scope for real plate queries and recorded nothing.
              This is the strongest finding in the report: a position investigations keep
              needing, where nothing was seen, is an evidence-backed case for where the
              next camera should go — not an opinion about it.
            </p>
            <div className="space-y-1">
              {data.journey_gaps.slice(0, 12).map((g) => (
                <div key={g.camera_id} className="flex items-center justify-between gap-2 text-[11px] bg-ink-900 rounded px-2 py-1.5">
                  <span className="mono text-fg2">{g.camera_ref}</span>
                  <span className="text-muted truncate flex-1">{g.name}</span>
                  <Badge tone="bad">{g.times_implied}×</Badge>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="p-3">
          <div className="flex items-center justify-between mb-2">
            <div className="text-xs font-medium">Camera findings</div>
            <select
              className="input w-40 text-[11px] py-1"
              value={kindFilter}
              onChange={(e) => setKindFilter(e.target.value)}
            >
              <option value="">All kinds</option>
              {Object.entries(KIND_LABEL).map(([k, label]) => (
                <option key={k} value={k}>{label}</option>
              ))}
            </select>
          </div>

          {!gaps.length && <Empty title="No findings of this kind" />}

          <div className="space-y-2">
            {gaps.map((g, i) => (
              <div key={`${g.camera_id}-${i}`} className="bg-ink-900 rounded p-2 space-y-1">
                <div className="flex items-center gap-2">
                  <Badge tone={KIND_TONE[g.kind] ?? "muted"}>{KIND_LABEL[g.kind] ?? g.kind}</Badge>
                  <span className="mono text-[11px] text-fg2">{g.camera_ref}</span>
                  {g.district && <span className="text-[10px] text-muted">{g.district}</span>}
                </div>
                <div className="text-[11px] text-muted">{g.location_text || g.name}</div>
                <div className="text-[10px] text-muted leading-snug">{g.detail}</div>
              </div>
            ))}
          </div>
        </div>

        {data && (
          <div className="p-3 border-t border-edge">
            <p className="text-[10px] text-muted leading-snug">{data.interpretation}</p>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone = "muted",
}: {
  label: string;
  value: number;
  tone?: "bad" | "warn" | "muted";
}) {
  const colour = tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-fg";
  return (
    <div className="bg-ink-900 rounded px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-lg font-medium ${colour}`}>{value}</div>
    </div>
  );
}
