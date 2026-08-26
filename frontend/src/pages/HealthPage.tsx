import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type CameraHealth } from "../lib/api";
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

  if (isLoading) return <Spinner label="Loading camera health…" />;
  if (error) return <div className="p-4"><ErrorBox error={error} /></div>;

  const counts = (data ?? []).reduce<Record<string, number>>((acc, c) => {
    acc[c.status] = (acc[c.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="h-full flex flex-col">
      <div className="p-3 border-b border-edge bg-ink-800 flex items-center gap-3 flex-wrap">
        <div className="font-medium">Feed health</div>
        {Object.entries(counts).map(([status, n]) => (
          <span key={status} className="flex items-center gap-1.5 text-xs">
            <StatusDot status={status} />
            <span className="text-muted">{status}</span>
            <span className="text-slate-300">{n}</span>
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
        <table className="w-full text-xs">
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
                    <div className="mono text-slate-300">{c.camera_ref}</div>
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

        <div className="p-3 text-[11px] text-muted max-w-3xl leading-snug">
          <strong className="text-slate-300">Declared versus measured frame rate.</strong>{" "}
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
