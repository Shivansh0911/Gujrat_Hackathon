/**
 * Coverage — where the state can actually see, and what it can see there.
 *
 * This is the signature view, and it earns the 3D rather than decorating with it.
 *
 * A flat pin map answers "where are the cameras". Every submission will have one. The
 * question a senior officer actually has is "where can we see, and where can we read a
 * plate" -- and the answer to that is a *shape*, not a set of points. Each camera
 * becomes a viewshed wedge oriented by its heading and coloured by its ANPR readiness
 * grade, so the map says at a glance which parts of Gujarat can read a registration and
 * which parts can merely be watched.
 *
 * The thing a pin map physically cannot show is the negative space. Blind spots are
 * the gaps *between* cones, and under pitch they are the most visible feature on the
 * screen. That turns the existing Gap Analysis from a table of numbers into something
 * a person understands in one glance, which is the whole justification for the third
 * dimension.
 *
 * Uncertainty stays visible. A camera located only to a district centroid is drawn as a
 * translucent dome of its confidence radius rather than a cone, because a cone implies
 * a known orientation from a known point and we have neither. That is the same rule the
 * 2D map already follows; 3D does not get to be more confident than the data.
 */

import { useEffect, useMemo, useState } from "react";
import type maplibregl from "maplibre-gl";
import { useQuery } from "@tanstack/react-query";
import { api, type CameraReadiness, type ReadinessGrade } from "../lib/api";
import {
  ASSUMED_FOV_DEG,
  ASSUMED_RANGE_M,
  circlePolygon,
  sectorPolygon,
} from "../lib/map";
import { useMapLibre } from "../lib/useMap";
import { GradeBadge, type Grade } from "../components/tiers";
import { ErrorBox } from "../components/ui";
import { WhatAmILookingAt } from "../components/explain";

/** Analytics a camera may be running, as layer filters. */
const ANALYTIC_LAYERS = [
  { key: "anpr", label: "Plate reading", hint: "where a registration can be read" },
  { key: "appearance", label: "Appearance", hint: "where a vehicle can be fingerprinted" },
  { key: "vehicle_counting", label: "Counting", hint: "where traffic can be counted" },
  { key: "intrusion_zones", label: "Intrusion", hint: "where a zone can be watched" },
] as const;

type AnalyticKey = (typeof ANALYTIC_LAYERS)[number]["key"];

const GRADE_FILL: Record<ReadinessGrade, string> = {
  anpr_capable: "#047857",
  marginal_reaim: "#b45309",
  not_viable: "#475569",
  insufficient_sample: "#94a3b8",
};

export default function CoveragePage() {
  const { ref: mapContainerRef, mapRef, ready } = useMapLibre();
  const [analytic, setAnalytic] = useState<AnalyticKey>("anpr");
  const [pitched, setPitched] = useState(true);
  const [selected, setSelected] = useState<CameraReadiness | null>(null);

  const cameras = useQuery({ queryKey: ["cameras"], queryFn: () => api.cameras() });
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: () => api.readiness() });

  /** Grade per camera id, so a camera with no grading still draws. */
  const gradeOf = useMemo(() => {
    const out = new Map<string, CameraReadiness>();
    for (const row of readiness.data?.cameras ?? []) out.set(row.camera_id, row);
    return out;
  }, [readiness.data]);

  /** Cones for placed cameras, domes for ones we only know approximately. */
  const features = useMemo(() => {
    const cones: GeoJSON.Feature[] = [];
    const domes: GeoJSON.Feature[] = [];

    for (const camera of cameras.data ?? []) {
      if (camera.lat == null || camera.lon == null) continue;

      const grading = gradeOf.get(camera.id);
      const grade = (grading?.grade ?? "insufficient_sample") as ReadinessGrade;
      // A camera is only drawn on a layer if it is actually running that analytic.
      // The assignment comes from §4.2 -- capability, not configuration -- so the map
      // shows what the estate can do rather than what somebody enabled.
      const analytics = grading?.analytics ?? ["anpr", "appearance", "vehicle_counting"];
      if (!analytics.includes(analytic)) continue;

      const properties = {
        id: camera.id,
        ref: camera.camera_ref,
        name: camera.name,
        grade,
        colour: GRADE_FILL[grade],
      };

      // The distinction that keeps this honest: a heading means we know which way it
      // points. Without one, a cone would be an invention.
      if (camera.heading_deg != null && camera.geom_source === "manual_survey") {
        cones.push({
          type: "Feature",
          properties,
          geometry: sectorPolygon(
            camera.lon,
            camera.lat,
            ASSUMED_RANGE_M,
            camera.heading_deg,
            ASSUMED_FOV_DEG,
          ),
        });
      } else if (camera.heading_deg != null) {
        cones.push({
          type: "Feature",
          properties: { ...properties, approximate: true },
          geometry: sectorPolygon(
            camera.lon,
            camera.lat,
            ASSUMED_RANGE_M,
            camera.heading_deg,
            ASSUMED_FOV_DEG,
          ),
        });
      } else {
        domes.push({
          type: "Feature",
          properties,
          geometry: circlePolygon(
            camera.lon,
            camera.lat,
            camera.confidence_radius_m ?? 300,
          ),
        });
      }
    }
    return { cones, domes };
  }, [cameras.data, gradeOf, analytic]);

  /* Draw. Keyed on `styledata` rather than `load`: a theme change replaces the style
     and silently drops every layer added against the old one. */
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;

    const draw = () => {
      // MapLibre throws "Style is not done loading" from addSource if the style is
      // still in flight, and the layer is then never added -- a blank map with one
      // console error. The styledata listener below is the retry.
      if (!map.isStyleLoaded()) return;

      for (const id of [
        "cones-fill",
        "cones-line",
        "cones-line-approx",
        "domes-fill",
        "domes-point",
      ]) {
        if (map.getLayer(id)) map.removeLayer(id);
      }
      for (const id of ["cones", "domes", "points"]) {
        if (map.getSource(id)) map.removeSource(id);
      }

      map.addSource("cones", {
        type: "geojson",
        data: { type: "FeatureCollection", features: features.cones },
      });
      map.addSource("domes", {
        type: "geojson",
        data: { type: "FeatureCollection", features: features.domes },
      });

      // Domes first, under the cones: an uncertain position should not obscure a
      // known one.
      map.addLayer({
        id: "domes-fill",
        type: "fill",
        source: "domes",
        paint: {
          "fill-color": ["get", "colour"],
          // Opacity rises with zoom. At state-wide zoom a 300 m dome is sub-pixel and
          // a 0.1 fill renders as nothing at all -- the first version of this screen
          // drew 263 cameras and showed an empty map. The dome is the honest shape for
          // a camera known only to a position; it just needs to be visible.
          "fill-opacity": ["interpolate", ["linear"], ["zoom"], 6, 0.35, 12, 0.15],
          "fill-outline-color": ["get", "colour"],
        },
      });

      // And a point, because existence must be legible before extent is. A camera that
      // disappears when you zoom out is a camera an officer will believe is not there.
      map.addLayer({
        id: "domes-point",
        type: "circle",
        source: "domes",
        paint: {
          "circle-color": ["get", "colour"],
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 2.5, 10, 5, 14, 7],
          "circle-opacity": 0.9,
          "circle-stroke-width": 1,
          "circle-stroke-color": "rgba(255,255,255,0.7)",
        },
      });

      map.addLayer({
        id: "cones-fill",
        type: "fill-extrusion",
        source: "cones",
        paint: {
          "fill-extrusion-color": ["get", "colour"],
          // Low extrusion. The height carries no information -- range and grade are
          // already encoded -- so it is just enough to separate overlapping cones
          // under pitch and no more. A tall extrusion would imply a measurement.
          "fill-extrusion-height": 25,
          "fill-extrusion-base": 0,
          "fill-extrusion-opacity": 0.55,
        },
      });

      // Two layers rather than one with an expression. MapLibre refuses a data
      // expression on `line-dasharray` -- "data expressions not supported" -- so the
      // solid and dashed cases are separate layers distinguished by a filter. An
      // approximate cone is dashed, exactly as a Candidate hop is: one visual grammar
      // for "this is provisional" across the whole console.
      map.addLayer({
        id: "cones-line",
        type: "line",
        source: "cones",
        filter: ["!=", ["get", "approximate"], true],
        paint: {
          "line-color": ["get", "colour"],
          "line-width": 1,
          "line-opacity": 0.9,
        },
      });

      map.addLayer({
        id: "cones-line-approx",
        type: "line",
        source: "cones",
        filter: ["==", ["get", "approximate"], true],
        paint: {
          "line-color": ["get", "colour"],
          "line-dasharray": [2, 2],
          "line-width": 1,
          "line-opacity": 0.9,
        },
      });

      map.on("click", "cones-fill", (event: maplibregl.MapLayerMouseEvent) => {
        const id = event.features?.[0]?.properties?.id as string | undefined;
        if (id) setSelected(gradeOf.get(id) ?? null);
      });
      map.on("click", "domes-point", (event: maplibregl.MapLayerMouseEvent) => {
        const id = event.features?.[0]?.properties?.id as string | undefined;
        if (id) setSelected(gradeOf.get(id) ?? null);
      });
      map.on("mouseenter", "domes-point", () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", "domes-point", () => (map.getCanvas().style.cursor = ""));
      map.on("mouseenter", "cones-fill", () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", "cones-fill", () => (map.getCanvas().style.cursor = ""));
    };

    draw();
    // `on`, not `once`: a theme change replaces the style, and a single-shot listener
    // is consumed by the first styledata and silently absent for the second.
    map.on("styledata", draw);
    return () => {
      map.off("styledata", draw);
    };
  }, [ready, mapRef, features, gradeOf]);

  /* Pitch is the whole point of this view, so it is on by default and reversible. */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.easeTo({ pitch: pitched ? 55 : 0, bearing: pitched ? -20 : 0, duration: 600 });
  }, [mapRef, ready, pitched]);

  // Stated in the legend rather than discovered. The estate publishes no heading for
  // any camera, which means the cone layer -- the thing this screen was designed
  // around -- has nothing to draw from yet.
  const headedCameras = (cameras.data ?? []).filter((c) => c.heading_deg != null).length;

  const summary = readiness.data;
  const counts = summary?.counts_by_grade ?? {};

  return (
    <div className="h-full flex flex-col">
      <header className="px-4 py-2.5 border-b border-edge flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex-1 min-w-[12rem]">
          <h1 className="t-title">Coverage</h1>
          <p className="t-micro text-muted">
            Where the estate can see, and what it can do there
          </p>
        </div>

        <div className="flex items-center gap-1" role="group" aria-label="Analytic layer">
          {ANALYTIC_LAYERS.map((layer) => (
            <button
              key={layer.key}
              type="button"
              onClick={() => setAnalytic(layer.key)}
              title={layer.hint}
              aria-pressed={analytic === layer.key}
              className={`btn t-small ${analytic === layer.key ? "btn-primary" : ""}`}
            >
              {layer.label}
            </button>
          ))}
        </div>

        <button
          type="button"
          className="btn t-small"
          onClick={() => setPitched((v) => !v)}
          aria-pressed={pitched}
        >
          {pitched ? "Flat view" : "3D view"}
        </button>
      </header>

      <WhatAmILookingAt
        what="Every camera drawn as the area it can actually see, coloured by whether a number plate is readable there."
        data="Camera positions and headings from the registry, graded against the plate and vehicle pixel widths measured from real detections."
        limits="Field of view is assumed at 55° and range at 120 m — neither is published by the estate, and cones drawn from them are indicative. A camera with no heading is drawn as a dome of its position uncertainty instead, because a cone would imply an orientation nobody recorded."
      />

      {cameras.error ? <ErrorBox error={cameras.error} /> : null}

      <div className="flex-1 relative min-h-0">
        <div ref={mapContainerRef} className="absolute inset-0" />

        {/* The legend is permanent, not a tooltip. An officer who has to discover what
            a colour means has already misread one. */}
        <div className="absolute left-3 bottom-3 panel p-2.5 max-w-xs">
          <div className="t-micro uppercase tracking-wider text-muted mb-1.5">
            ANPR readiness
          </div>
          <div className="flex flex-col gap-1">
            {(
              [
                ["anpr_capable", "plates readable"],
                ["marginal_reaim", "close; re-aim would fix it"],
                ["not_viable", "watch only — counting, flow, appearance"],
                ["insufficient_sample", "not enough observations yet"],
              ] as [ReadinessGrade, string][]
            ).map(([grade, hint]) => (
              <span key={grade} className="flex items-center gap-2">
                <GradeBadge grade={grade as Grade} />
                <span className="t-micro text-fg3">{hint}</span>
                <span className="t-micro num text-muted ml-auto">{counts[grade] ?? 0}</span>
              </span>
            ))}
          </div>
          <p className="t-micro text-muted mt-2 pt-2 border-t border-edge">
            {headedCameras === 0 ? (
              <>
                <span className="text-fg3">
                  No camera in this estate has a recorded heading, so no viewshed cone
                  can be drawn.
                </span>{" "}
                Each camera is shown as a dome of its position uncertainty instead. A
                cone would imply an orientation nobody surveyed. Onboarding a camera
                over ONVIF, or recording a heading during a survey, turns its dome into
                a cone.
              </>
            ) : (
              <>
                A dashed outline marks an approximate position. Blind spots are the gaps
                between cones.
              </>
            )}
          </p>
        </div>

        {selected ? (
          <aside className="absolute right-3 top-3 panel p-3 w-80 max-w-[calc(100%-1.5rem)]">
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="t-section">{selected.camera_name}</div>
                <div className="t-micro mono text-muted">{selected.camera_ref}</div>
              </div>
              <button
                className="btn-quiet btn t-micro"
                onClick={() => setSelected(null)}
                aria-label="Close camera detail"
              >
                ✕
              </button>
            </div>

            <div className="mt-2">
              <GradeBadge grade={selected.grade as Grade} />
            </div>
            <p className="t-small text-fg3 mt-2">{selected.reasoning}</p>
            {selected.remedy ? (
              <p className="t-small mt-2 pt-2 border-t border-edge">
                <span className="t-micro uppercase tracking-wider text-muted block mb-0.5">
                  Recommended
                </span>
                {selected.remedy}
              </p>
            ) : null}

            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 mt-2.5 pt-2.5 border-t border-edge">
              {[
                ["Plate width", selected.plate_px_median, "px median"],
                ["Vehicle width", selected.vehicle_px_median, "px median"],
                ["Plate:vehicle", selected.plate_to_vehicle_ratio, ""],
                ["Observations", selected.plate_samples, "plates"],
              ].map(([label, value, unit]) => (
                <div key={String(label)}>
                  <dt className="t-micro text-muted">{label}</dt>
                  <dd className="t-small num">
                    {value == null ? "—" : String(value)}{" "}
                    <span className="t-micro text-muted">{unit}</span>
                  </dd>
                </div>
              ))}
            </dl>

            <div className="mt-2.5 pt-2.5 border-t border-edge">
              <span className="t-micro uppercase tracking-wider text-muted block mb-1">
                Running here
              </span>
              <div className="flex flex-wrap gap-1">
                {selected.analytics.map((a) => (
                  <span key={a} className="badge bg-ink-700 text-fg3">
                    {a.replace(/_/g, " ")}
                  </span>
                ))}
              </div>
            </div>
          </aside>
        ) : null}
      </div>
    </div>
  );
}
