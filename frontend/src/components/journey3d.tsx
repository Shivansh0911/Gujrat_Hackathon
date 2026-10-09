/**
 * Time as altitude: a journey drawn as a polyline rising through time.
 *
 * X and Y are geography; Z is the clock. The reason to build this rather than animate
 * a dot along a 2D route is that three things an investigator looks for stop being
 * things you reason about and become shapes you recognise:
 *
 *   A vertical segment is loitering. Same place, time passing.
 *   A near-horizontal segment is impossible travel. Distance crossed with almost no
 *   time spent -- the signature of a misread plate or a cloned one.
 *   Two polylines running parallel at the same altitude are a convoy.
 *
 * None of those is a new measurement. They are the same hops the 2D map draws, in a
 * projection where the anomaly is the silhouette instead of a column of numbers.
 *
 * Three things this deliberately does not do:
 *
 *   It does not interpolate between hops. The line between two sightings is drawn
 *   because the eye needs it to read order, and it is drawn as a straight chord, which
 *   no vehicle travelled. The hops are evidence; the line is a connector. Where the
 *   journey crosses a coverage gap the line breaks outright rather than guessing.
 *
 *   It does not promote anything. Segment colour comes from the tier of the hop it
 *   arrives at, and a candidate hop keeps its tier colour here exactly as it does in
 *   the list. A 3D view is a better way to look at evidence, not a better grade of it.
 *
 *   It is never the only way to read the route. The flat fallback is one click away
 *   and is what `prefers-reduced-motion` gets by default, because a pitched, rotating
 *   camera is precisely what that setting exists to decline.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";

import type { CoverageGap, JourneyHop } from "../lib/api";
import { BASEMAP, BASEMAP_BACKGROUND, BASEMAP_PAINT } from "../lib/map";
import { useTheme } from "../lib/theme";
import type { Tier } from "./tiers";

/** A hop's tier, on the same rule the hop list uses. */
function tierOf(hop: JourneyHop): Tier {
  return hop.evidence_type === "anpr_exact" ? "confirmed" : "probable";
}

/** Read a theme token as a WebGL colour. Tokens are `r g b`, so the themes track. */
function tokenRgb(name: string): [number, number, number] {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const parts = raw
    .split(/[\s,/]+/)
    .map(Number)
    .filter((n) => Number.isFinite(n));
  if (parts.length < 3) return [0.5, 0.5, 0.5];
  return [parts[0] / 255, parts[1] / 255, parts[2] / 255];
}

const VERTEX_SRC = `
  uniform mat4 u_matrix;
  attribute vec3 a_pos;
  attribute vec4 a_colour;
  varying vec4 v_colour;
  void main() {
    v_colour = a_colour;
    gl_Position = u_matrix * vec4(a_pos, 1.0);
  }
`;

const FRAGMENT_SRC = `
  precision mediump float;
  varying vec4 v_colour;
  void main() { gl_FragColor = vec4(v_colour.rgb * v_colour.a, v_colour.a); }
`;

function compile(gl: WebGLRenderingContext, kind: number, src: string): WebGLShader {
  const shader = gl.createShader(kind);
  if (!shader) throw new Error("shader allocation failed");
  gl.shaderSource(shader, src);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader) ?? "shader compile failed");
  }
  return shader;
}

/**
 * The projection matrix MapLibre hands a custom layer. Taken from the library's own
 * signature rather than written as `number[]`: MapLibre 4 passes gl-matrix's `mat4`,
 * an IndexedCollection, and a hand-written type makes the layer unassignable to
 * CustomLayerInterface.
 */
type RenderMatrix = Parameters<maplibregl.CustomRenderMethod>[1];

type Rgba = [number, number, number, number];
type Vertex = { x: number; y: number; z: number; colour: Rgba };

/** Geometry for one frame: a ribbon for the trajectory, lines for everything else. */
type Geometry = { ribbon: Vertex[]; lines: Vertex[] };

/**
 * The custom layer. Rendering mode "3d" so MapLibre hands over the full projection
 * matrix and depth state rather than compositing us as a flat overlay.
 */
class TrajectoryLayer implements maplibregl.CustomLayerInterface {
  id = "journey-3d";
  type = "custom" as const;
  renderingMode = "3d" as const;

  private program: WebGLProgram | null = null;
  private buffer: WebGLBuffer | null = null;
  private aPos = 0;
  private aColour = 0;
  private uMatrix: WebGLUniformLocation | null = null;
  private ribbonCount = 0;
  private lineCount = 0;
  private pending: Geometry = { ribbon: [], lines: [] };
  private dirty = false;

  constructor(private readonly repaint: () => void) {}

  onAdd(_map: maplibregl.Map, gl: WebGLRenderingContext): void {
    const program = gl.createProgram();
    if (!program) throw new Error("program allocation failed");
    gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX_SRC));
    gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SRC));
    gl.linkProgram(program);
    this.program = program;
    this.aPos = gl.getAttribLocation(program, "a_pos");
    this.aColour = gl.getAttribLocation(program, "a_colour");
    this.uMatrix = gl.getUniformLocation(program, "u_matrix");
    this.buffer = gl.createBuffer();
    this.dirty = true;
  }

  onRemove(_map: maplibregl.Map, gl: WebGLRenderingContext): void {
    if (this.program) gl.deleteProgram(this.program);
    if (this.buffer) gl.deleteBuffer(this.buffer);
    this.program = null;
    this.buffer = null;
  }

  /**
   * Hand the layer new geometry. It uploads on the next frame rather than now.
   *
   * The first version took a GL context from `map.painter.context.gl` and uploaded
   * immediately. That is an internal path, and the property names do not survive the
   * library's own minification -- so in a production build `gl` came back undefined,
   * the upload never happened, and the layer rendered the empty buffer it was created
   * with. In the dev build it worked, which is the worst way for this to fail.
   *
   * `render()` is the one place MapLibre gives a custom layer a context through the
   * documented interface. Uploading there costs a flag and removes the dependency on
   * internals entirely.
   */
  setGeometry(geometry: Geometry): void {
    this.pending = geometry;
    this.dirty = true;
    this.repaint();
  }

  private upload(gl: WebGLRenderingContext): void {
    // Ribbon triangles first, then lines, in one buffer: two draw calls, one upload.
    const all = [...this.pending.ribbon, ...this.pending.lines];
    this.ribbonCount = this.pending.ribbon.length;
    this.lineCount = this.pending.lines.length;
    const data = new Float32Array(all.length * 7);
    all.forEach((v, i) => {
      data.set([v.x, v.y, v.z, v.colour[0], v.colour[1], v.colour[2], v.colour[3]], i * 7);
    });
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW);
  }

  // MapLibre 4 types the matrix as gl-matrix's `mat4`, an IndexedCollection rather
  // than a plain array. uniformMatrix4fv accepts it either way; the signature has to
  // match or the layer is not assignable to CustomLayerInterface.
  render(gl: WebGLRenderingContext, matrix: RenderMatrix): void {
    if (!this.program || !this.buffer) return;
    if (this.dirty) {
      this.upload(gl);
      this.dirty = false;
    }
    if (this.ribbonCount + this.lineCount === 0) return;
    gl.useProgram(this.program);
    gl.uniformMatrix4fv(this.uMatrix, false, matrix as Float32Array);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);

    const stride = 7 * 4;
    gl.enableVertexAttribArray(this.aPos);
    gl.vertexAttribPointer(this.aPos, 3, gl.FLOAT, false, stride, 0);
    gl.enableVertexAttribArray(this.aColour);
    gl.vertexAttribPointer(this.aColour, 4, gl.FLOAT, false, stride, 3 * 4);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    // Culling off. MapLibre leaves CULL_FACE enabled from its own fill-extrusion pass,
    // and WebGL state is global to the context -- so the ribbon's triangles were being
    // discarded by winding order while the drop lines, which culling does not apply to,
    // drew perfectly. That asymmetry is what identified it: same buffer, same shader,
    // same depth state, only the triangles missing. A trajectory is a two-sided
    // surface anyway; it is meant to be read from either side.
    gl.disable(gl.CULL_FACE);
    // Depth test on, depth write off: the trajectory is occluded by the globe but its
    // own overlapping segments blend instead of punching holes in one another.
    gl.enable(gl.DEPTH_TEST);
    gl.depthMask(false);

    if (this.ribbonCount) gl.drawArrays(gl.TRIANGLES, 0, this.ribbonCount);
    if (this.lineCount) gl.drawArrays(gl.LINES, this.ribbonCount, this.lineCount);

    gl.depthMask(true);
  }
}

export function Journey3D({
  hops,
  gaps,
  plate,
}: {
  hops: JourneyHop[];
  gaps: CoverageGap[];
  plate: string;
}) {
  const { theme } = useTheme();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const layerRef = useRef<TrajectoryLayer | null>(null);
  /** The zoom the flat fit chose, so the pitch effect can offset from it rather than
      compounding its own zoom-out every time the projection is toggled. */
  const fitZoomRef = useRef<number | null>(null);
  const [ready, setReady] = useState(false);

  // Reduced motion gets the flat view by default. A pitched camera the user did not
  // ask for is exactly what the setting declines, and the route is fully readable
  // without it -- so this is a default, not a lockout.
  const reduceMotion =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
  const [threeD, setThreeD] = useState(!reduceMotion);

  /** Scrub position, 0..1 of the journey's duration. 1 is the whole route. */
  const [cursor, setCursor] = useState(1);

  const placed = useMemo(
    () => hops.filter((h) => Number.isFinite(h.lat) && Number.isFinite(h.lon)),
    [hops],
  );

  const times = useMemo(
    () => placed.map((h) => new Date(h.observed_at_utc).getTime()),
    [placed],
  );
  const t0 = times.length ? times[0] : 0;
  const span = times.length ? Math.max(times[times.length - 1] - t0, 1) : 1;

  /**
   * Altitude is scaled against the journey's own horizontal extent rather than fixed
   * in metres. A 4 km city route and a 300 km corridor both need the time axis to be
   * comparable to the ground distance, or one renders as a flat smear and the other
   * as a vertical spike.
   */
  const extentM = useMemo(() => {
    if (placed.length < 2) return 5000;
    const lats = placed.map((h) => h.lat);
    const lons = placed.map((h) => h.lon);
    const dLat = (Math.max(...lats) - Math.min(...lats)) * 111_320;
    const midLat = (Math.max(...lats) + Math.min(...lats)) / 2;
    const dLon =
      (Math.max(...lons) - Math.min(...lons)) * 111_320 * Math.cos((midLat * Math.PI) / 180);
    return Math.max(Math.hypot(dLat, dLon), 2000);
  }, [placed]);

  /**
   * How tall the time column is, as a fraction of the route's ground extent.
   *
   * Tuned against the screen rather than reasoned about, because a pitched camera
   * exaggerates vertical distance severely: at 0.55 and then 0.35 of a 250 km route,
   * the last hop sat 137 km and then 87 km above the ground and most of the trajectory
   * projected above the horizon, leaving a single drop line visible near the first
   * camera. 0.12 keeps the whole column inside the frame while still making a vertical
   * segment -- a vehicle that stayed put -- unmistakable against a sloped one.
   */
  const maxAltitudeM = extentM * 0.12;

  const build = useCallback((): Geometry => {
    const ribbon: Vertex[] = [];
    const lines: Vertex[] = [];
    if (placed.length === 0) return { ribbon, lines };

    const colours: Record<Tier, [number, number, number]> = {
      confirmed: tokenRgb("--tier-confirmed"),
      probable: tokenRgb("--tier-probable"),
      candidate: tokenRgb("--tier-candidate"),
    };
    const muted = tokenRgb("--muted");

    const cutoff = t0 + span * cursor;
    const scale = maplibregl.MercatorCoordinate.fromLngLat(
      [placed[0].lon, placed[0].lat],
      0,
    ).meterInMercatorCoordinateUnits();
    // A ribbon rather than gl.LINES, because WebGL line width above 1 px is
    // unimplemented in every browser that matters and a 1 px trajectory at a pitch
    // disappears into the basemap.
    const halfWidth = extentM * 0.015 * scale;

    const point = (hop: JourneyHop, altitudeM: number) => {
      const c = maplibregl.MercatorCoordinate.fromLngLat(
        [hop.lon, hop.lat],
        threeD ? altitudeM : 0,
      );
      return { x: c.x, y: c.y, z: c.z };
    };

    const altitudeOf = (index: number) =>
      threeD ? ((times[index] - t0) / span) * maxAltitudeM : 0;

    placed.forEach((hop, i) => {
      if (times[i] > cutoff) return;
      const colour = colours[tierOf(hop)];
      const here = point(hop, altitudeOf(i));

      // The drop line to the ground: where on the map this moment happened. Without
      // it an altitude reads as a position and the plot lies about geography.
      if (threeD) {
        const ground = point(hop, 0);
        lines.push(
          { ...here, colour: [colour[0], colour[1], colour[2], 0.55] },
          { ...ground, colour: [colour[0], colour[1], colour[2], 0.06] },
        );
      }

      const next = placed[i + 1];
      if (!next || times[i + 1] > cutoff) return;

      const there = point(next, altitudeOf(i + 1));
      const arriving = colours[tierOf(next)];

      // Perpendicular in the ground plane, so the ribbon stays readable from above
      // and from a low pitch alike.
      const dx = there.x - here.x;
      const dy = there.y - here.y;
      const length = Math.hypot(dx, dy) || 1e-9;
      const px = (-dy / length) * halfWidth;
      const py = (dx / length) * halfWidth;

      const from: Rgba = [colour[0], colour[1], colour[2], 0.9];
      const to: Rgba = [arriving[0], arriving[1], arriving[2], 0.9];
      const a = { x: here.x + px, y: here.y + py, z: here.z, colour: from };
      const b = { x: here.x - px, y: here.y - py, z: here.z, colour: from };
      const c = { x: there.x + px, y: there.y + py, z: there.z, colour: to };
      const d = { x: there.x - px, y: there.y - py, z: there.z, colour: to };
      ribbon.push(a, b, c, b, d, c);
    });

    // Coverage gaps as spurs, which is what they are and what the 2D map already
    // draws: a camera on the corridor that produced no detection, reached from the hop
    // it follows. The first version of this treated a gap as a break in the route and
    // started a new line at each one. That was a misreading -- this journey carries 244
    // of them across four hops, so every segment broke and the trajectory rendered as
    // nothing at all. A gap qualifies a leg; it does not sever it.
    for (const gap of gaps) {
      const index = placed.findIndex((h) => h.seq === gap.after_seq);
      if (index < 0 || times[index] > cutoff) continue;
      if (!Number.isFinite(gap.lat) || !Number.isFinite(gap.lon)) continue;
      const altitude = altitudeOf(index);
      const anchor = point(placed[index], altitude);
      const blind = maplibregl.MercatorCoordinate.fromLngLat(
        [gap.lon, gap.lat],
        threeD ? altitude : 0,
      );
      lines.push(
        { ...anchor, colour: [muted[0], muted[1], muted[2], 0.5] },
        { x: blind.x, y: blind.y, z: blind.z, colour: [muted[0], muted[1], muted[2], 0] },
      );
    }

    return { ribbon, lines };
  }, [placed, times, t0, span, cursor, threeD, maxAltitudeM, extentM, gaps]);

  /* --- Map lifecycle ------------------------------------------------------------- */

  useEffect(() => {
    const container = containerRef.current;
    if (!container || mapRef.current || placed.length === 0) return;

    const lats = placed.map((h) => h.lat);
    const lons = placed.map((h) => h.lon);
    const bounds = new maplibregl.LngLatBounds(
      [Math.min(...lons), Math.min(...lats)],
      [Math.max(...lons), Math.max(...lats)],
    );

    const map = new maplibregl.Map({
      container,
      style: BASEMAP,
      bounds,
      // Modest padding. The container is 416 px tall, and the 140 px this started at
      // left 136 px of vertical space for the fit -- which zoomed a 250 km route out
      // to most of western India. Headroom for the time column comes from the explicit
      // zoom-out below, where it can be a fixed amount rather than a side effect of
      // padding fighting the container height.
      fitBoundsOptions: { padding: 48, maxZoom: 12 },
      // Fit flat, then pitch. MapLibre fits bounds against the *pitched* frustum, and
      // at 55 degrees the far field covers several times the ground the route occupies
      // -- constructing with the pitch already set zoomed a 112 km journey out to half
      // of western India. The pitch effect below tilts the camera once the fit is done,
      // which keeps the centre and zoom the flat fit chose.
      pitch: 0,
      bearing: 0,
      attributionControl: { compact: true },
    });
    map.addControl(
      new maplibregl.NavigationControl({ showCompass: true, visualizePitch: true }),
      "top-right",
    );
    mapRef.current = map;

    const observer = new ResizeObserver(() => map.resize());
    observer.observe(container);

    map.on("load", () => {
      fitZoomRef.current = map.getZoom();
      const layer = new TrajectoryLayer(() => map.triggerRepaint());
      layerRef.current = layer;
      map.addLayer(layer);
      setReady(true);
    });

    return () => {
      observer.disconnect();
      map.remove();
      mapRef.current = null;
      layerRef.current = null;
      setReady(false);
    };
  }, [placed, reduceMotion]);

  // Rebuild on every input that changes the shape, the theme included: the tier
  // colours are theme tokens, so a theme switch has to re-read them.
  useEffect(() => {
    const map = mapRef.current;
    const layer = layerRef.current;
    if (!map || !layer || !ready) return;
    layer.setGeometry(build());
  }, [build, ready, theme]);

  // Recolour the basemap in place, for the same reason the shared hook does: setStyle
  // would take the custom layer with it and the trajectory would vanish.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !map.getLayer("osm")) return;
    for (const [k, v] of Object.entries(BASEMAP_PAINT[theme])) {
      map.setPaintProperty("osm", k as never, v as never);
    }
    map.setPaintProperty("bg", "background-color", BASEMAP_BACKGROUND[theme]);
  }, [theme, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    // The pitch is applied here rather than in the constructor so the bounds are fit
    // against a flat frustum. The zoom-out makes room for the time column, which the
    // ground-footprint fit knows nothing about.
    map.easeTo({
      pitch: threeD ? 55 : 0,
      bearing: threeD ? -20 : 0,
      zoom: fitZoomRef.current == null ? undefined : fitZoomRef.current - (threeD ? 0.4 : 0),
      duration: reduceMotion ? 0 : 500,
    });
  }, [threeD, ready, reduceMotion]);

  /* --- Readout ------------------------------------------------------------------- */

  if (placed.length < 2) {
    return (
      <p className="t-small text-muted panel p-3">
        A time-as-altitude plot needs at least two placed hops. This journey has{" "}
        {placed.length}.
      </p>
    );
  }

  const cursorTime = new Date(t0 + span * cursor);
  const shown = times.filter((t) => t <= t0 + span * cursor).length;

  return (
    <section className="flex flex-col gap-2">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="t-section">
          The shape of this movement
          <span className="t-micro text-muted font-normal ml-2">
            geography across, time upward
          </span>
        </h2>
        <div className="flex gap-1" role="group" aria-label="Plot projection">
          <button
            type="button"
            className={`btn t-small ${threeD ? "btn-primary" : ""}`}
            aria-pressed={threeD}
            onClick={() => setThreeD(true)}
          >
            Time as altitude
          </button>
          <button
            type="button"
            className={`btn t-small ${!threeD ? "btn-primary" : ""}`}
            aria-pressed={!threeD}
            onClick={() => setThreeD(false)}
          >
            Flat
          </button>
        </div>
      </header>

      <div
        ref={containerRef}
        className="w-full h-[26rem] rounded border border-edge overflow-hidden"
        role="img"
        aria-label={`Journey of ${plate}: ${placed.length} placed hops over ${Math.round(
          span / 60000,
        )} minutes, drawn with time as altitude.`}
      />

      <div className="flex items-center gap-3">
        <label className="t-micro uppercase tracking-wider text-muted" htmlFor="scrub">
          Replay
        </label>
        <input
          id="scrub"
          type="range"
          min={0}
          max={1}
          step={0.005}
          value={cursor}
          onChange={(event) => setCursor(Number(event.target.value))}
          className="flex-1"
          aria-label="Scrub through the journey"
        />
        <span className="t-micro num tabular-nums text-fg2 w-44 text-right">
          {cursorTime.toLocaleString("en-IN", { hour12: false })}
        </span>
        <span className="t-micro num text-muted w-20 text-right">
          {shown}/{placed.length} hops
        </span>
      </div>

      <p className="t-micro text-muted">
        A <span className="text-fg3">vertical segment</span> is a vehicle that stayed put
        while time passed. A <span className="text-fg3">near-horizontal segment</span> is
        distance crossed with almost no time spent &mdash; the signature of a misread or
        cloned plate, not of a fast car. Two routes running{" "}
        <span className="text-fg3">parallel at the same altitude</span> were travelling
        together. The line between two hops is a connector drawn so the eye can read
        order, not a path anybody observed. The faint spurs reach out to cameras on the
        corridor that recorded nothing &mdash; {gaps.length} of them on this route. A
        spur is a camera that could have seen this vehicle and did not; it qualifies the
        leg it hangs from rather than breaking it.
      </p>
    </section>
  );
}
