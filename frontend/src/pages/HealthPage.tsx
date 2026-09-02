import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type CameraHealth, type GatewayStatus, type VehicleCounts } from "../lib/api";
import { Badge, ErrorBox, Spinner, StatusDot } from "../components/ui";

type SortKey = "status" | "camera_ref" | "drift" | "measured";

/** Degraded and unreachable cameras sort first: they are what an operator is here for. */
const STATUS_ORDER: Record<string, number> = {
  UNREACHABLE: 0, DEGRADED: 1, PROBING: 2, DRAFT: 3, ACTIVE: 4,
};

export default function HealthPage() {
  const [sort, setSort] = useState<SortKey>("status");
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [report, setReport] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(),
    refetchInterval: 20_000,
  });

  const { data: alerts } = useQuery({ queryKey: ["alerts", ""], queryFn: () => api.alerts() });

  // Polled rather than probed: the endpoint returns the watcher's last observation,
  // so refreshing this page never puts load on somebody else's infrastructure.
  const gateway = useQuery({
    queryKey: ["gateway-status"],
    queryFn: () => api.gatewayStatus(),
    refetchInterval: 30_000,
  });

  const [countHours, setCountHours] = useState(24);
  const vehicleCounts = useQuery({
    queryKey: ["vehicle-counts", countHours],
    queryFn: () => api.vehicleCounts(countHours, countHours > 48 ? "day" : "hour"),
    refetchInterval: 60_000,
  });

  // False-positive rate per camera, from operator dispositions. This is the platform
  // measuring its own precision rather than asserting it.
  const fpRate = useMemo(() => {
    const tally: Record<string, { fp: number; resolved: number }> = {};
    for (const a of alerts ?? []) {
      if (a.state !== "RESOLVED" || !a.disposition) continue;
      const key = a.camera_id;
      tally[key] ??= { fp: 0, resolved: 0 };
      tally[key].resolved += 1;
      if (a.disposition === "false_positive") tally[key].fp += 1;
    }
    return tally;
  }, [alerts]);

  const rows = useMemo(() => {
    let list = [...(data ?? [])];
    if (onlyProblems) list = list.filter((c) => c.status !== "ACTIVE");
    list.sort((a, b) => {
      if (sort === "status")
        return (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9);
      if (sort === "drift")
        return Math.abs(b.fps_drift_pct ?? 0) - Math.abs(a.fps_drift_pct ?? 0);
      if (sort === "measured") return (b.measured_fps ?? 0) - (a.measured_fps ?? 0);
      return a.camera_ref.localeCompare(b.camera_ref, undefined, { numeric: true });
    });
    return list;
  }, [data, sort, onlyProblems]);

  // The gateway card renders above these early returns on purpose. It answers a
  // question that has nothing to do with whether the camera table has loaded, and a
  // page that shows nothing at all while it loads cannot be called always-visible --
  // which is the entire point of a passive indicator.
  if (isLoading)
    return (
      <div className="h-full flex flex-col">
        <GatewayCard status={gateway.data} />
        <Spinner label="Loading camera health…" />
      </div>
    );
  if (error)
    return (
      <div className="h-full flex flex-col">
        <GatewayCard status={gateway.data} />
        <div className="p-4">
          <ErrorBox error={error} />
        </div>
      </div>
    );

  const counts = (data ?? []).reduce<Record<string, number>>((acc, c) => {
    acc[c.status] = (acc[c.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="h-full flex flex-col">
      <GatewayCard status={gateway.data} />
      <div className="p-3 border-b border-edge bg-ink-800 flex items-center gap-3 flex-wrap">
        <div className="font-medium">Feed health</div>
        {Object.entries(counts).map(([status, n]) => (
          <span key={status} className="flex items-center gap-1.5 text-xs">
            <StatusDot status={status} />
            <span className="text-muted">{status}</span>
            <span className="text-fg2">{n}</span>
          </span>
        ))}
        <div className="flex-1" />
        <label className="text-xs flex items-center gap-1.5 text-muted">
          <input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} />
          problems only
        </label>
        <select className="input w-44" value={sort} onChange={(e) => setSort(e.target.value as SortKey)}>
          <option value="status">Sort: status</option>
          <option value="camera_ref">Sort: reference</option>
          <option value="drift">Sort: fps drift</option>
          <option value="measured">Sort: measured fps</option>
        </select>
      </div>

      <div className="flex-1 overflow-auto">
        <div className="p-3 pb-0">
          <VehicleCountCard
            data={vehicleCounts.data}
            hours={countHours}
            onHours={setCountHours}
            loading={vehicleCounts.isLoading}
          />
        </div>

        {/*
          The table has ten columns and does not fit a phone. It gets its own
          horizontal scroller rather than being allowed to widen the page: wide content
          scrolls inside its container, the page body never does. `min-w-[52rem]` stops
          the columns collapsing into an unreadable concertina on the way.
        */}
        <div className="overflow-x-auto">
        <table className="w-full min-w-[52rem] text-xs">
          <thead className="sticky top-0 bg-ink-800 border-b border-edge">
            <tr className="text-left text-muted">
              <th className="px-3 py-2 font-medium">Camera</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Transport</th>
              <th className="px-3 py-2 font-medium text-right">Declared fps</th>
              <th className="px-3 py-2 font-medium text-right">Measured fps</th>
              <th className="px-3 py-2 font-medium text-right">Drift</th>
              <th className="px-3 py-2 font-medium text-right">False-positive</th>
              <th className="px-3 py-2 font-medium">Position</th>
              <th className="px-3 py-2 font-medium" />
            </tr>
          </thead>
          <tbody className="divide-y divide-edge/60">
            {rows.map((c) => {
              const fp = fpRate[c.camera_id];
              return (
                <tr key={c.camera_id} className="hover:bg-ink-700/40">
                  <td className="px-3 py-2">
                    <div className="mono text-fg2">{c.camera_ref}</div>
                    <div className="text-muted truncate max-w-[16rem]">{c.name}</div>
                  </td>
                  <td className="px-3 py-2">
                    <span className="flex items-center gap-1.5">
                      <StatusDot status={c.status} /> {c.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-muted">{c.transport ?? "—"}</td>
                  <td className="px-3 py-2 text-right mono text-muted">
                    {c.declared_fps?.toFixed(2) ?? "not declared"}
                  </td>
                  <td className="px-3 py-2 text-right mono">
                    {c.measured_fps != null
                      ? <span className="text-ok">{c.measured_fps.toFixed(2)}</span>
                      : <span className="text-muted">—</span>}
                  </td>
                  <td className="px-3 py-2 text-right mono">
                    {c.fps_drift_pct != null ? (
                      <span className={Math.abs(c.fps_drift_pct) > 15 ? "text-warn" : "text-muted"}>
                        {c.fps_drift_pct > 0 ? "+" : ""}{c.fps_drift_pct.toFixed(1)}%
                      </span>
                    ) : <span className="text-muted">—</span>}
                  </td>
                  <td className="px-3 py-2 text-right mono">
                    {fp ? (
                      <span className={fp.fp / fp.resolved > 0.3 ? "text-warn" : "text-muted"}>
                        {((fp.fp / fp.resolved) * 100).toFixed(0)}% of {fp.resolved}
                      </span>
                    ) : <span className="text-muted">no dispositions</span>}
                  </td>
                  <td className="px-3 py-2">
                    {c.coordinate_missing
                      ? <Badge tone="warn">coordinate missing</Badge>
                      : <span className="text-muted">placed</span>}
                  </td>
                  <td className="px-3 py-2">
                    <button className="btn py-0.5 text-[11px]" onClick={() => setReport(buildFaultReport(c))}>
                      Fault report
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        </div>

        <div className="p-3 text-[11px] text-muted max-w-3xl leading-snug">
          <strong className="text-fg2">Declared versus measured frame rate.</strong>{" "}
          The integration guide warns that a camera's reported frame rate cannot be
          trusted, and on this estate most cameras declare no rate at all. Every measured
          figure here is derived from stream presentation timestamps, never from the
          declared value — showing both is how that warning becomes a visible property of
          the platform rather than an assumption buried in the pipeline.
        </div>
      </div>

      {report && (
        <div className="fixed inset-0 bg-black/70 grid place-items-center p-6 z-50" onClick={() => setReport(null)}>
          <div className="panel p-4 max-w-2xl w-full space-y-3" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <div className="font-medium">Support fault report</div>
              <button className="btn py-0.5" onClick={() => setReport(null)}>Close</button>
            </div>
            <p className="text-[11px] text-muted">
              Formatted exactly as the organiser's support protocol requires: camera id,
              exact URL, client and version, UTC timestamp and the client-side error log.
            </p>
            <pre className="bg-ink-900 border border-edge rounded p-3 text-[11px] mono overflow-auto max-h-96 whitespace-pre-wrap">
              {report}
            </pre>
            <button
              className="btn btn-primary w-full"
              onClick={() => navigator.clipboard?.writeText(report)}
            >
              Copy to clipboard
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function buildFaultReport(c: CameraHealth): string {
  const now = new Date().toISOString();
  return [
    "SETU — CAMERA FAULT REPORT",
    "=".repeat(48),
    `Camera ID          : ${c.camera_ref}`,
    `Camera name        : ${c.name}`,
    `Reported status    : ${c.status}`,
    `Transport in use   : ${c.transport ?? "not established"}`,
    `UTC timestamp      : ${now}`,
    "",
    "STREAM PROPERTIES",
    `  Declared fps     : ${c.declared_fps ?? "not declared by catalogue"}`,
    `  Measured fps     : ${c.measured_fps ?? "no frames decoded"}`,
    `  Drift            : ${c.fps_drift_pct != null ? `${c.fps_drift_pct.toFixed(1)}%` : "n/a"}`,
    `  Last frame       : ${c.last_seen_at ?? "never"}`,
    "",
    "CLIENT",
    "  Software         : SETU ingest (OpenCV/FFmpeg, RTSP forced TCP with HLS fallback)",
    `  User agent       : ${navigator.userAgent}`,
    "",
    "CLIENT-SIDE OBSERVATION",
    `  Catalogue status verified before reporting: camera is listed as live in /api/ingest.`,
    `  Platform status  : ${c.status}`,
    c.coordinate_missing ? "  Note             : no coordinate on record for this camera." : "",
    "",
    "REQUEST",
    "  Please confirm whether this camera is publishing, and whether a direct origin",
    "  endpoint is available for RTSP/WHEP (ports 8554/8889 are not reachable through",
    "  the Cloudflare-fronted host).",
  ].filter(Boolean).join("\n");
}

function VehicleCountCard({
  data,
  hours,
  onHours,
  loading,
}: {
  data: VehicleCounts | undefined;
  hours: number;
  onHours: (h: number) => void;
  loading: boolean;
}) {
  const peak = data?.windows.reduce(
    (m, w) => (w.reads > m ? w.reads : m),
    0,
  );

  return (
    <div className="panel p-4 mb-4">
      <div className="flex items-start justify-between gap-4 flex-wrap mb-3">
        <div>
          <h2 className="font-medium">Vehicles identified</h2>
          <p className="text-xs text-muted max-w-xl mt-0.5">
            Derived from the same detections the alert desk uses — no second pass over
            the video, and no extra load on any camera.
          </p>
        </div>
        <div className="flex gap-1">
          {[1, 24, 168].map((h) => (
            <button
              key={h}
              className={`btn text-xs ${hours === h ? "btn-primary" : ""}`}
              onClick={() => onHours(h)}
            >
              {h === 1 ? "1 hour" : h === 24 ? "24 hours" : "7 days"}
            </button>
          ))}
        </div>
      </div>

      {loading && <Spinner label="Counting" />}

      {data && (
        <>
          <div className="flex items-baseline gap-6 flex-wrap">
            <Stat label="Distinct registrations" value={data.total_distinct_plates} />
            <Stat label="Plate reads" value={data.total_reads} />
            <Stat label="Cameras reporting" value={data.by_camera.length} />
          </div>

          {peak !== undefined && peak > 0 && (
            <ReadsChart windows={data.windows} bucket={data.bucket} peak={peak} />
          )}

          {data.by_camera.length > 0 && (
            <div className="mt-4 overflow-x-auto rounded border border-edge">
              <table className="w-full text-sm">
                <thead className="bg-ink-700 text-muted text-[11px] uppercase tracking-wide">
                  <tr>
                    <th className="text-left px-3 py-2">Camera</th>
                    <th className="text-right px-3 py-2">Distinct</th>
                    <th className="text-right px-3 py-2">Reads</th>
                    <th className="text-left px-3 py-2">Last seen</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_camera.slice(0, 8).map((c) => (
                    <tr key={c.camera_id} className="border-t border-edge">
                      <td className="px-3 py-2">
                        <span className="mono">{c.camera_ref}</span>{" "}
                        <span className="text-muted text-xs">{c.camera_name}</span>
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {c.distinct_plates}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-muted">
                        {c.reads}
                      </td>
                      <td className="px-3 py-2 text-xs text-muted">
                        {c.last_seen_utc
                          ? new Date(c.last_seen_utc).toLocaleString()
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {data.by_camera.length === 0 && (
            <p className="text-xs text-muted mt-3">
              No vehicle was identified in this period.
            </p>
          )}

          <p className="text-[10px] text-muted mt-3 max-w-3xl">{data.caveat}</p>
        </>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
    </div>
  );
}


/** How long ago, in words an operator can read at a glance. */
function since(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(ms / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"}`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? "" : "s"}`;
  const days = Math.floor(hrs / 24);
  return `${days} day${days === 1 ? "" : "s"}`;
}

/**
 * Gateway reachability, always on screen.
 *
 * Before this, the only way to learn the government feed was down was to press
 * "Compare with gateway" on another page. That answers "is it up now" and never
 * "when did it stop", which is the question actually asked — including, most
 * awkwardly, mid-demonstration.
 *
 * The distinction the card is careful about: an empty Alert Desk because nothing
 * matched, and an empty Alert Desk because the feed died forty minutes ago, look
 * identical. Only one of them is a problem with this platform.
 */
function GatewayCard({ status }: { status: GatewayStatus | undefined }) {
  if (!status) return null;

  const notYetChecked = status.reachable === null || status.reachable === undefined;
  const up = status.reachable === true;

  const tone = notYetChecked ? "muted" : up ? "ok" : "bad";
  const label = notYetChecked
    ? "not yet checked"
    : up
      ? "government gateway reachable"
      : "government gateway unreachable";

  return (
    <div
      className={`px-3 py-2 border-b border-edge flex items-center gap-3 flex-wrap text-xs ${
        up || notYetChecked ? "bg-ink-800" : "bg-bad/10"
      }`}
    >
      <Badge tone={tone as "ok" | "bad" | "muted"}>{label}</Badge>

      {!up && !notYetChecked && status.unreachable_since && (
        <span className="text-bad">
          down for <span className="font-medium">{since(status.unreachable_since)}</span>
          <span className="text-muted">
            {" "}
            (since {new Date(status.unreachable_since).toLocaleTimeString()})
          </span>
        </span>
      )}

      {up && status.cameras_in_catalogue != null && (
        <span className="text-muted tabular-nums">
          {status.cameras_in_catalogue} cameras in catalogue
        </span>
      )}

      <span className="text-muted">
        {status.last_checked_at
          ? // `since` already returns "just now" for anything under a minute, and
            // "just now ago" is not a phrase. Only the durations take the suffix.
            since(status.last_checked_at) === "just now"
            ? "checked just now"
            : `checked ${since(status.last_checked_at)} ago`
          : "awaiting first check"}
      </span>

      {status.last_success_at && !up && (
        <span className="text-muted">
          {since(status.last_success_at) === "just now"
            ? "last reached moments ago"
            : `last reached ${since(status.last_success_at)} ago`}
        </span>
      )}

      <div className="flex-1" />

      {!up && !notYetChecked && (
        <span className="text-muted max-w-md leading-snug" title={status.last_error ?? ""}>
          This is the organiser's feed, not this platform. Everything below is from our
          own records and is unaffected.
        </span>
      )}
    </div>
  );
}


/**
 * Reads per time bucket.
 *
 * The previous version was a row of bars with a native `title` and nothing else: no
 * axis, no scale, no indication of the period covered. A bar's height meant nothing
 * you could name, and "what am I looking at" had to be asked out loud, which for a
 * chart is the whole failure.
 *
 * Four things fix that, and none of them needs a charting library: the y-axis states
 * its own maximum, the x-axis states the span in local time, hovering names the exact
 * bucket, and a caption says in one line what a bar counts. Empty buckets are drawn as
 * a faint floor rather than omitted, because "no vehicle passed in this hour" is a
 * reading and a missing bar looks like missing data.
 */
function ReadsChart({
  windows,
  bucket,
  peak,
}: {
  windows: VehicleCounts["windows"];
  bucket: string;
  peak: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  if (windows.length === 0) return null;

  const active = hover !== null ? windows[hover] : null;
  const first = windows[0];
  const last = windows[windows.length - 1];
  const fmt = (iso: string) =>
    new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });

  return (
    <div className="mt-4">
      <div className="flex items-baseline justify-between gap-3 flex-wrap mb-1.5">
        <div className="label !mb-0">Plate reads per {bucket}</div>
        <div className="text-[11px] text-muted tabular-nums h-4">
          {active ? (
            <>
              <span className="text-fg">{fmt(active.bucket_start_utc)}</span>
              {" — "}
              <span className="text-accent">{active.reads} reads</span>
              {", "}
              {active.distinct_plates} distinct vehicle
              {active.distinct_plates === 1 ? "" : "s"}
            </>
          ) : (
            <span>hover a bar for its exact count</span>
          )}
        </div>
      </div>

      <div className="flex gap-2">
        {/* y-axis: the scale the bars are drawn against, stated rather than implied */}
        <div className="flex flex-col justify-between text-[10px] text-muted tabular-nums h-16 shrink-0 text-right w-8">
          <span>{peak}</span>
          <span>0</span>
        </div>

        <div className="flex-1 min-w-0">
          <div
            className="flex items-end gap-[3px] h-16 border-l border-b border-edge pl-1 pb-px"
            onMouseLeave={() => setHover(null)}
          >
            {windows.map((w, i) => (
              <div
                key={w.bucket_start_utc}
                className={`flex-1 min-w-[3px] rounded-sm transition-colors ${
                  hover === i ? "bg-accent" : w.reads > 0 ? "bg-accent/70" : "bg-edge"
                }`}
                style={{
                  height: w.reads > 0 ? `${Math.max(6, (w.reads / peak) * 100)}%` : "2px",
                }}
                onMouseEnter={() => setHover(i)}
                title={`${fmt(w.bucket_start_utc)} — ${w.reads} reads, ${w.distinct_plates} distinct`}
              />
            ))}
          </div>

          {/* x-axis: the period actually covered, in the reader's own timezone */}
          <div className="flex justify-between text-[10px] text-muted mt-1 tabular-nums">
            <span>{fmt(first.bucket_start_utc)}</span>
            <span>{fmt(last.bucket_start_utc)}</span>
          </div>
        </div>
      </div>

      <p className="text-[10px] text-muted mt-1.5 leading-snug">
        Each bar is one {bucket}: how many number plates were read in it, across every
        camera. A flat bar is an interval where nothing was read — which on this estate
        usually means the feed was down, not that the road was empty.
      </p>
    </div>
  );
}
