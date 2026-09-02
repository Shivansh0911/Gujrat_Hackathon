import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Camera, type DetectionPoint, type Zone } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Badge, Empty, ErrorBox, Spinner } from "../components/ui";

/**
 * Draw an intrusion zone on a camera's frame.
 *
 * The surface is deliberately not a live video still. A zone is stored in frame pixels
 * (see migration 0006), so what matters when drawing it is the coordinate space, not the
 * picture — and this estate's media plane is intermittent enough that a page which needs
 * a live frame to function would be unusable half the time.
 *
 * What is shown instead is better for the job anyway: **every place a vehicle has
 * actually been detected on this camera**. Drawing a polygon on an empty rectangle is
 * guesswork — an operator cannot tell which part of the frame traffic passes through,
 * and a zone drawn over the sky alerts on nothing while looking entirely reasonable.
 * Plotting real detections makes zone placement answerable from evidence, the same way
 * the coverage report derives gaps from queries that really ran.
 */

const DEFAULT_W = 1920;
const DEFAULT_H = 1080;

/** Canvas width in CSS pixels; the frame is scaled to fit and the maths done in frame space. */
const CANVAS_W = 760;

export default function ZonesPage() {
  const { role } = useAuth();
  const isAdmin = role === "admin";
  const qc = useQueryClient();

  const [cameraId, setCameraId] = useState<string>("");
  const [points, setPoints] = useState<[number, number][]>([]);
  const [name, setName] = useState("Restricted area");
  const [refW, setRefW] = useState(DEFAULT_W);
  const [refH, setRefH] = useState(DEFAULT_H);
  const [error, setError] = useState<string | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const cameras = useQuery({
    queryKey: ["cameras", "zones"],
    queryFn: () => api.cameras({ limit: "500" }),
  });

  // Default to a camera that has detections behind it: a zone editor opened on a camera
  // with nothing to show teaches an operator that the feature does nothing.
  useEffect(() => {
    if (!cameraId && cameras.data?.length) {
      const withDetections = cameras.data.find((c) => (c.detection_count ?? 0) > 0);
      setCameraId((withDetections ?? cameras.data[0]).id);
    }
  }, [cameras.data, cameraId]);

  const zones = useQuery({
    queryKey: ["zones", cameraId],
    queryFn: () => api.zones(cameraId),
    enabled: Boolean(cameraId),
  });

  const detections = useQuery({
    queryKey: ["detection-points", cameraId],
    queryFn: () => api.detectionPoints(cameraId),
    enabled: Boolean(cameraId),
  });

  const camera: Camera | undefined = cameras.data?.find((c) => c.id === cameraId);

  // Fit the frame into the canvas. All coordinates are stored in frame pixels; this
  // scale exists only so a 1920-wide frame is drawable on a laptop.
  const scale = CANVAS_W / refW;
  const canvasH = Math.round(refH * scale);

  const save = useMutation({
    mutationFn: () =>
      api.createZone(cameraId, {
        name: name.trim(),
        points,
        reference_width: refW,
        reference_height: refH,
        active: true,
      }),
    onSuccess: () => {
      setPoints([]);
      setError(null);
      void qc.invalidateQueries({ queryKey: ["zones", cameraId] });
    },
    onError: (e) => setError(e instanceof Error ? e.message : "Could not save the zone"),
  });

  /**
   * Ask whether anything already on record matches what was just configured.
   *
   * Alerts are raised as detections arrive, so a zone drawn today would otherwise only
   * ever apply to tomorrow's traffic -- and an operator draws a zone precisely because
   * of somewhere that has already been driven through. No video is re-read; the stored
   * detections are re-evaluated.
   */
  const rescan = useMutation({
    mutationFn: () => api.rescan(24 * 30),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["alerts"] });
    },
  });

  const remove = useMutation({
    mutationFn: (zoneId: string) => api.deleteZone(cameraId, zoneId),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["zones", cameraId] }),
  });

  function addPoint(e: React.MouseEvent<SVGSVGElement>) {
    if (!isAdmin) return;
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    // Back to frame pixels immediately, so what is stored is what was clicked.
    const x = Math.round(((e.clientX - rect.left) / rect.width) * refW);
    const y = Math.round(((e.clientY - rect.top) / rect.height) * refH);
    setPoints((p) => [...p, [x, y]]);
  }

  const ringPath = useMemo(
    () => points.map(([x, y]) => `${x * scale},${y * scale}`).join(" "),
    [points, scale],
  );

  /** How many recorded detections the pending polygon would have caught. */
  const wouldCatch = useMemo(() => {
    if (points.length < 3 || !detections.data) return null;
    return detections.data.filter((d) => inside(d, points)).length;
  }, [points, detections.data]);

  return (
    <div className="h-full overflow-y-auto p-4 space-y-4">
      <section className="panel p-4">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <h1 className="font-medium text-lg">Intrusion zones</h1>
            <p className="text-xs text-muted mt-1 leading-snug max-w-3xl">
              Draw a region on a camera's view. A vehicle whose bounding box{" "}
              <strong>centres</strong> inside it raises an alert on the Alert Desk, with
              the same cooldown the movement alerts use so a parked vehicle is one alert
              with a count rather than one per frame.
            </p>
          </div>
          <select
            className="input w-72 text-sm"
            value={cameraId}
            onChange={(e) => {
              setCameraId(e.target.value);
              setPoints([]);
            }}
          >
            {(cameras.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.camera_ref} — {c.name}
                {c.detection_count ? ` (${c.detection_count} detections)` : ""}
              </option>
            ))}
          </select>
        </div>
        {!isAdmin && (
          <p className="text-[11px] text-warn mt-2">
            Zones are read-only for operators. Configuring one asserts that vehicles
            entering a place should be alerted on, so it is an admin act and is audited.
          </p>
        )}
      </section>

      {cameras.isLoading && <Spinner label="Loading cameras…" />}
      {cameras.error && <ErrorBox error={cameras.error} />}

      {cameraId && (
        <div className="grid gap-4 lg:grid-cols-[auto,20rem]">
          {/* ------------------------------------------------ drawing surface */}
          <section className="panel p-4">
            <div className="flex items-center justify-between gap-2 flex-wrap mb-2">
              <div className="text-xs text-muted">
                {isAdmin ? "Click to add a corner." : "Viewing only."}{" "}
                {detections.data?.length
                  ? `${detections.data.length} recorded detection${
                      detections.data.length === 1 ? "" : "s"
                    } shown.`
                  : "No detections recorded on this camera yet."}
              </div>
              <div className="flex items-center gap-2">
                <label className="text-[11px] text-muted">frame</label>
                <input
                  className="input w-20 text-xs py-1"
                  type="number"
                  value={refW}
                  onChange={(e) => setRefW(Math.max(1, Number(e.target.value) || DEFAULT_W))}
                />
                <span className="text-muted text-xs">×</span>
                <input
                  className="input w-20 text-xs py-1"
                  type="number"
                  value={refH}
                  onChange={(e) => setRefH(Math.max(1, Number(e.target.value) || DEFAULT_H))}
                />
              </div>
            </div>

            <svg
              ref={svgRef}
              onClick={addPoint}
              viewBox={`0 0 ${CANVAS_W} ${canvasH}`}
              className={`w-full rounded border border-edge bg-ink-900 ${
                isAdmin ? "cursor-crosshair" : ""
              }`}
              style={{ aspectRatio: `${refW} / ${refH}` }}
            >
              {/* A grid, so the frame reads as a coordinate space rather than a void. */}
              {Array.from({ length: 8 }, (_, i) => (
                <line
                  key={`v${i}`}
                  x1={(CANVAS_W / 8) * i}
                  y1={0}
                  x2={(CANVAS_W / 8) * i}
                  y2={canvasH}
                  stroke="#1e2733"
                  strokeWidth={1}
                />
              ))}
              {Array.from({ length: 5 }, (_, i) => (
                <line
                  key={`h${i}`}
                  x1={0}
                  y1={(canvasH / 5) * i}
                  x2={CANVAS_W}
                  y2={(canvasH / 5) * i}
                  stroke="#1e2733"
                  strokeWidth={1}
                />
              ))}

              {/* Where vehicles have actually been. This is the evidence a zone is
                  drawn against, not decoration. */}
              {(detections.data ?? []).map((d, i) => (
                <circle
                  key={i}
                  cx={d.x * scale}
                  cy={d.y * scale}
                  r={3}
                  fill={points.length >= 3 && inside(d, points) ? "#f05252" : "#4da3ff"}
                  opacity={0.75}
                />
              ))}

              {/* Zones already saved on this camera. */}
              {(zones.data ?? []).map((z) => (
                <polygon
                  key={z.id}
                  points={z.points.map(([x, y]) => `${x * scale},${y * scale}`).join(" ")}
                  fill="#31c48d"
                  fillOpacity={0.12}
                  stroke="#31c48d"
                  strokeWidth={1.5}
                  strokeDasharray={z.active ? undefined : "4 3"}
                />
              ))}

              {/* The polygon being drawn. */}
              {points.length > 0 && (
                <polygon
                  points={ringPath}
                  fill="#f0a13a"
                  fillOpacity={0.18}
                  stroke="#f0a13a"
                  strokeWidth={2}
                />
              )}
              {points.map(([x, y], i) => (
                <circle
                  key={`p${i}`}
                  cx={x * scale}
                  cy={y * scale}
                  r={4}
                  fill="#f0a13a"
                  stroke="#0b0f14"
                  strokeWidth={1.5}
                />
              ))}
            </svg>

            {isAdmin && (
              <div className="flex items-end gap-2 flex-wrap mt-3">
                <div className="flex-1 min-w-[12rem]">
                  <label className="label">Zone name</label>
                  <input
                    className="input"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </div>
                <button
                  className="btn"
                  onClick={() => setPoints((p) => p.slice(0, -1))}
                  disabled={!points.length}
                >
                  Undo point
                </button>
                <button className="btn" onClick={() => setPoints([])} disabled={!points.length}>
                  Clear
                </button>
                <button
                  className="btn btn-primary"
                  onClick={() => save.mutate()}
                  disabled={points.length < 3 || !name.trim() || save.isPending}
                >
                  {save.isPending ? "Saving…" : "Save zone"}
                </button>
                <button
                  className="btn"
                  onClick={() => rescan.mutate()}
                  disabled={rescan.isPending}
                  title="Re-evaluate detections already on record against the current zones and watchlist"
                >
                  {rescan.isPending ? "Checking…" : "Check recorded detections"}
                </button>
              </div>
            )}

            {points.length > 0 && points.length < 3 && (
              <p className="text-[11px] text-muted mt-2">
                A zone needs at least three corners — two points make a line, and a line
                contains nothing.
              </p>
            )}
            {wouldCatch !== null && (
              <p className="text-[11px] mt-2 text-muted">
                This shape encloses{" "}
                <strong className={wouldCatch ? "text-warn" : ""}>{wouldCatch}</strong> of
                the {detections.data?.length ?? 0} recorded detections.{" "}
                {wouldCatch === 0 &&
                  "Nothing has been seen there yet, so it would not have alerted."}
              </p>
            )}
            {rescan.data && (
              <p className="text-[11px] mt-2 text-muted">
                Re-evaluated {rescan.data.detections_scanned} recorded detection
                {rescan.data.detections_scanned === 1 ? "" : "s"}:{" "}
                <strong className={rescan.data.zone_alerts ? "text-warn" : ""}>
                  {rescan.data.zone_alerts}
                </strong>{" "}
                zone alert{rescan.data.zone_alerts === 1 ? "" : "s"},{" "}
                {rescan.data.watchlist_alerts} watchlist,{" "}
                {rescan.data.speed_alerts} speed. They appear on the Alert Desk.
              </p>
            )}
            {rescan.error != null && (
              <p className="text-[11px] text-bad mt-2">
                {rescan.error instanceof Error ? rescan.error.message : "Re-check failed"}
              </p>
            )}
            {error && <p className="text-[11px] text-bad mt-2">{error}</p>}
          </section>

          {/* ------------------------------------------------------ zone list */}
          <section className="panel p-4 h-fit">
            <h2 className="font-medium text-sm mb-2">
              Zones on {camera?.camera_ref ?? "this camera"}
            </h2>
            {zones.isLoading && <Spinner label="Loading…" />}
            {zones.error && <ErrorBox error={zones.error} />}
            {zones.data && !zones.data.length && (
              <Empty
                title="No zones yet"
                detail="Click on the frame to place corners, then save. The blue dots are places a vehicle has actually been detected."
              />
            )}
            <div className="space-y-2">
              {(zones.data ?? []).map((z) => (
                <ZoneRow
                  key={z.id}
                  zone={z}
                  isAdmin={isAdmin}
                  onDelete={() => remove.mutate(z.id)}
                  deleting={remove.isPending}
                />
              ))}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function ZoneRow({
  zone,
  isAdmin,
  onDelete,
  deleting,
}: {
  zone: Zone;
  isAdmin: boolean;
  onDelete: () => void;
  deleting: boolean;
}) {
  return (
    <div className="bg-ink-900 rounded p-2.5 space-y-1">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm text-slate-100">{zone.name}</span>
        <Badge tone={zone.active ? "ok" : "muted"}>{zone.active ? "active" : "inactive"}</Badge>
      </div>
      <div className="text-[10px] text-muted">
        {zone.points.length} corners · drawn against {zone.reference_width}×
        {zone.reference_height}
        {zone.created_by ? ` · by ${zone.created_by}` : ""}
      </div>
      {isAdmin && (
        <button
          className="btn text-[11px] py-0.5 px-2"
          onClick={onDelete}
          disabled={deleting}
        >
          Delete
        </button>
      )}
    </div>
  );
}

/**
 * Ray casting, matching what PostGIS will decide.
 *
 * Only ever used to preview how many recorded detections a pending shape would have
 * caught — the alert itself is decided by `ST_Contains` on the server. A second
 * implementation is acceptable here precisely because it is advisory: if the two ever
 * disagreed at a boundary, the number on screen would be off by one and nothing would
 * be wrongly alerted.
 */
function inside(p: DetectionPoint, ring: [number, number][]): boolean {
  let hit = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > p.y !== yj > p.y && p.x < ((xj - xi) * (p.y - yi)) / (yj - yi) + xi) {
      hit = !hit;
    }
  }
  return hit;
}
