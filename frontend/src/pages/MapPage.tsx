import { useEffect, useMemo, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Camera } from "../lib/api";
import { circlePolygon, statusColour } from "../lib/map";
import { useMapLibre } from "../lib/useMap";
import {
  Badge,
  Empty,
  ErrorBox,
  NoDataBadge,
  SourceBadge,
  Spinner,
  StatusDot,
} from "../components/ui";
import HlsPlayer from "../components/HlsPlayer";

export default function MapPage() {
  const qc = useQueryClient();
  const { ref: mapContainerRef, mapRef } = useMapLibre();
  const [selected, setSelected] = useState<Camera | null>(null);
  const [placing, setPlacing] = useState<Camera | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [deptFilter, setDeptFilter] = useState<string>("");

  const { data: cameras, isLoading, error } = useQuery({
    queryKey: ["cameras"],
    queryFn: () => api.cameras(),
  });

  const { data: streamUrl } = useQuery({
    queryKey: ["stream", selected?.id],
    queryFn: () => api.streamUrl(selected!.id),
    enabled: !!selected,
    retry: false,
  });

  const patchGeom = useMutation({
    mutationFn: ({ id, lat, lon }: { id: string; lat: number; lon: number }) =>
      api.patchGeom(id, lat, lon, "Placed from the GIS pin editor"),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["cameras"] });
      setSelected(updated);
      setPlacing(null);
    },
  });

  const filtered = useMemo(() => {
    if (!cameras) return [];
    return cameras.filter(
      (c) =>
        (!statusFilter || c.status === statusFilter) &&
        (!deptFilter || c.department_code === deptFilter),
    );
  }, [cameras, statusFilter, deptFilter]);

  const placed = filtered.filter((c) => c.lat != null && c.lon != null);
  const missing = filtered.filter((c) => c.coordinate_missing);
  const departments = useMemo(
    () => [...new Set((cameras ?? []).map((c) => c.department_code).filter(Boolean))] as string[],
    [cameras],
  );

  // ---- map init -----------------------------------------------------------

  // ---- render cameras -----------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    // Only the map itself is a precondition. An empty `placed` is a real result --
    // it is what a status filter matching nothing produces -- and it has to reach
    // the GeoJSON source so the previous filter's pins are cleared. Returning early
    // on it left the map showing the last non-empty selection, which reads as the
    // filter having silently failed rather than having matched nothing.
    if (!map) return;

    const draw = () => {
      // Confidence circles first, so precise pins draw on top of the uncertainty
      // they sit inside.
      const circles = {
        type: "FeatureCollection" as const,
        features: placed
          .filter((c) => (c.confidence_radius_m ?? 0) > 500)
          .map((c) => ({
            type: "Feature" as const,
            properties: { colour: statusColour(c.status), radius: c.confidence_radius_m },
            geometry: circlePolygon(c.lon!, c.lat!, c.confidence_radius_m!),
          })),
      };
      const points = {
        type: "FeatureCollection" as const,
        features: placed.map((c) => ({
          type: "Feature" as const,
          properties: {
            id: c.id,
            colour: statusColour(c.status),
            precise: (c.confidence_radius_m ?? 0) <= 500 ? 1 : 0,
            // A registry position with nothing behind it is drawn hollow. Thirty
            // government pins and four own-feed pins looked identical here, and a
            // reviewer reasonably read the map as thirty working cameras.
            hasData: (c.detection_count ?? 0) > 0 ? 1 : 0,
          },
          geometry: { type: "Point" as const, coordinates: [c.lon!, c.lat!] },
        })),
      };

      if (map.getSource("cam-circles")) {
        (map.getSource("cam-circles") as maplibregl.GeoJSONSource).setData(circles);
        (map.getSource("cam-points") as maplibregl.GeoJSONSource).setData(points);
        return;
      }

      map.addSource("cam-circles", { type: "geojson", data: circles });
      map.addLayer({
        id: "cam-circles-fill",
        type: "fill",
        source: "cam-circles",
        paint: { "fill-color": ["get", "colour"], "fill-opacity": 0.1 },
      });
      map.addLayer({
        id: "cam-circles-line",
        type: "line",
        source: "cam-circles",
        paint: {
          "line-color": ["get", "colour"],
          "line-opacity": 0.5,
          "line-width": 1,
          "line-dasharray": [2, 2],
        },
      });

      map.addSource("cam-points", { type: "geojson", data: points });
      map.addLayer({
        id: "cam-points-circle",
        type: "circle",
        source: "cam-points",
        paint: {
          "circle-color": ["get", "colour"],
          // An approximate camera gets a smaller, softer dot: the circle around it
          // carries the real information about where it might be.
          "circle-radius": ["case", ["==", ["get", "precise"], 1], 6, 4],
          // Hollow means "in the registry, has never produced a detection". Filled
          // means there is evidence behind this pin. Opacity already carries
          // positional confidence, so presence-of-data uses fill instead of fading
          // further -- two meanings on one channel would be unreadable.
          "circle-opacity": [
            "case",
            ["==", ["get", "hasData"], 0],
            0.12,
            ["==", ["get", "precise"], 1],
            1,
            0.65,
          ],
          "circle-stroke-width": ["case", ["==", ["get", "hasData"], 0], 1.5, 1.5],
          "circle-stroke-color": [
            "case",
            ["==", ["get", "hasData"], 0],
            ["get", "colour"],
            "#0b0f14",
          ],
        },
      });

      map.on("click", "cam-points-circle", (e) => {
        const id = e.features?.[0]?.properties?.id as string | undefined;
        const cam = placed.find((c) => c.id === id);
        if (cam) setSelected(cam);
      });
      map.on("mouseenter", "cam-points-circle", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "cam-points-circle", () => {
        map.getCanvas().style.cursor = "";
      });
    };

    if (map.isStyleLoaded()) draw();
    else map.once("load", draw);
  }, [placed]);

  // ---- pin placement ------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (!placing) {
      map.getCanvas().style.cursor = "";
      return;
    }
    map.getCanvas().style.cursor = "crosshair";
    const onClick = (e: maplibregl.MapMouseEvent) => {
      patchGeom.mutate({ id: placing.id, lat: e.lngLat.lat, lon: e.lngLat.lng });
    };
    map.once("click", onClick);
    return () => {
      map.off("click", onClick);
    };
  }, [placing, patchGeom]);

  return (
    // Below md the map and the camera list stack instead of sitting side by side.
    // The list is w-96 -- 384px, wider than a 375px phone -- so in a row layout the
    // map's flex-1 resolved to zero and the GIS page rendered with no map on it at
    // all. The map keeps a fixed share of the height on mobile so it is actually
    // present, and the list scrolls beneath it.
    <div className="h-full flex flex-col lg:flex-row">
      <div className="relative min-w-0 h-[42vh] shrink-0 lg:h-auto lg:flex-1">
        <div ref={mapContainerRef} className="absolute inset-0" />

        {/* Legend. The provenance explanation is the point of this panel. */}
        <div className="hidden lg:block absolute bottom-4 right-4 panel p-3 text-xs space-y-2 w-72 bg-ink-800/95">
          <div className="font-medium text-slate-200">Legend</div>
          <div className="flex items-center gap-2"><StatusDot status="ACTIVE" /> Active</div>
          <div className="flex items-center gap-2"><StatusDot status="DEGRADED" /> Degraded</div>
          <div className="flex items-center gap-2"><StatusDot status="UNREACHABLE" /> Unreachable</div>
          <div className="pt-2 border-t border-edge space-y-1.5">
            <div className="flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-accent" /> Precise position
            </div>
            <div className="flex items-center gap-2">
              <span className="w-3.5 h-3.5 rounded-full border border-dashed border-accent/60 bg-accent/10" />
              Approximate — circle is the confidence radius
            </div>
            <p className="text-muted/90 leading-snug pt-1">
              A camera we can only place to a district is drawn as a circle, not a
              precise pin. Showing the uncertainty is deliberate: a false pin would
              produce an authoritative-looking route that is wrong.
            </p>
          </div>
        </div>

        {placing && (
          <div className="absolute top-4 left-1/2 -translate-x-1/2 panel px-4 py-2 bg-accent/20 border-accent/60 text-sm">
            Click the map to place <span className="mono">{placing.camera_ref}</span>
            <button className="btn ml-3 py-0.5" onClick={() => setPlacing(null)}>Cancel</button>
          </div>
        )}
      </div>

      {/* ---- side panel ---- */}
      <div className="w-full lg:w-96 shrink-0 border-t lg:border-t-0 lg:border-l border-edge bg-ink-800 overflow-y-auto flex-1 lg:flex-none">
        <div className="p-3 border-b border-edge space-y-2">
          <div className="flex items-center justify-between">
            <div className="font-medium">Camera registry</div>
            <Badge tone="muted">{filtered.length} of {cameras?.length ?? 0}</Badge>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <select className="input" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">All statuses</option>
              {["ACTIVE", "DEGRADED", "UNREACHABLE", "DRAFT", "PROBING"].map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <select className="input" value={deptFilter} onChange={(e) => setDeptFilter(e.target.value)}>
              <option value="">All departments</option>
              {departments.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </div>
        </div>

        {missing.length > 0 && (
          <div className="p-3 border-b border-edge">
            <div className="flex items-center gap-2 mb-2">
              <Badge tone="warn">coordinate missing</Badge>
              <span className="text-xs text-muted">{missing.length} camera(s)</span>
            </div>
            <p className="text-[11px] text-muted mb-2 leading-snug">
              These cameras have no position, so they are excluded from spatial queries
              and route reconstruction. They are listed rather than hidden — an operator
              must be able to see that a camera could have contributed evidence and did
              not.
            </p>
            <div className="space-y-1">
              {missing.map((c) => (
                <div key={c.id} className="flex items-center justify-between gap-2 text-xs bg-ink-900 rounded px-2 py-1.5">
                  <div className="min-w-0">
                    <div className="mono text-slate-300">{c.camera_ref}</div>
                    <div className="text-muted truncate">{c.location_text || c.name}</div>
                  </div>
                  <button className="btn py-0.5 text-[11px]" onClick={() => setPlacing(c)}>
                    Place pin
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {isLoading && <Spinner label="Loading camera registry…" />}
        {error && <div className="p-3"><ErrorBox error={error} /></div>}

        {selected ? (
          <CameraDetail
            camera={selected}
            streamUrl={streamUrl?.url ?? null}
            onClose={() => setSelected(null)}
            onPlace={() => setPlacing(selected)}
          />
        ) : (
          <div className="p-3 space-y-1">
            {placed.map((c) => (
              <button
                key={c.id}
                onClick={() => {
                  setSelected(c);
                  mapRef.current?.flyTo({ center: [c.lon!, c.lat!], zoom: 11 });
                }}
                className="w-full text-left px-2 py-1.5 rounded hover:bg-ink-700 flex items-center gap-2 text-xs"
              >
                <StatusDot status={c.status} />
                <span className="mono text-slate-300 w-8">{c.camera_ref}</span>
                <span className="truncate flex-1 text-muted">{c.location_text || c.name}</span>
                {(c.confidence_radius_m ?? 0) > 500 && (
                  <span className="text-[10px] text-warn shrink-0">
                    ±{(c.confidence_radius_m! / 1000).toFixed(1)}km
                  </span>
                )}
              </button>
            ))}
            {!placed.length && <Empty title="No cameras match these filters" />}
          </div>
        )}
      </div>
    </div>
  );
}

function CameraDetail({
  camera,
  streamUrl,
  onClose,
  onPlace,
}: {
  camera: Camera;
  streamUrl: string | null;
  onClose: () => void;
  onPlace: () => void;
}) {
  const approximate = (camera.confidence_radius_m ?? 0) > 500;
  return (
    <div className="p-3 space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-medium">{camera.name}</div>
          <div className="text-xs text-muted">{camera.location_text}</div>
        </div>
        <button className="btn py-0.5" onClick={onClose}>Close</button>
      </div>

      <div className="flex flex-wrap gap-1.5">
        <Badge tone={camera.status === "ACTIVE" ? "ok" : camera.status === "UNREACHABLE" ? "bad" : "warn"}>
          {camera.status}
        </Badge>
        <Badge tone="muted">{camera.department_code}</Badge>
        <SourceBadge sourceType={camera.source_type} />
        <NoDataBadge count={camera.detection_count ?? 0} />
        {approximate && (
          <Badge tone="warn" title="Positioned to a district centroid, not surveyed">
            ±{(camera.confidence_radius_m! / 1000).toFixed(1)} km
          </Badge>
        )}
      </div>

      <HlsPlayer url={streamUrl} lastKnownStatus={camera.status} cameraRef={camera.camera_ref} />

      <dl className="text-xs space-y-1.5">
        <Row k="Reference" v={<span className="mono">{camera.camera_ref}</span>} />
        <Row k="Coordinates" v={
          camera.lat != null
            ? <span className="mono">{camera.lat.toFixed(5)}, {camera.lon!.toFixed(5)}</span>
            : <span className="text-warn">coordinate missing</span>
        } />
        <Row k="Provenance" v={camera.geom_source} />
        <Row k="Resolved by" v={camera.resolved_by ?? "—"} />
        <Row k="Codec" v={camera.codec ?? "unknown"} />
        <Row k="Resolution" v={
          camera.resolution_w ? `${camera.resolution_w}×${camera.resolution_h}` : "unknown"
        } />
        <Row k="Declared fps" v={camera.declared_fps?.toFixed(2) ?? "not declared"} />
        <Row k="Measured fps" v={
          camera.measured_fps
            ? <span className="text-ok mono">{camera.measured_fps.toFixed(2)}</span>
            : <span className="text-muted">not yet measured</span>
        } />
        <Row k="Transport" v={camera.transport ?? "—"} />
      </dl>

      <button className="btn w-full" onClick={onPlace}>
        {camera.coordinate_missing ? "Place pin" : "Correct position"}
      </button>
      <p className="text-[10px] text-muted leading-snug">
        Placing a pin records the coordinate as <span className="mono">manual_survey</span>{" "}
        against your account and writes an audit entry. It takes effect immediately, with
        no redeploy.
      </p>
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted shrink-0">{k}</dt>
      <dd className="text-right text-slate-300 min-w-0 truncate">{v}</dd>
    </div>
  );
}
