/**
 * The control-room dashboard: what the estate is doing right now.
 *
 * Every other screen answers a question somebody came with. This one answers the
 * question nobody asks out loud — "is everything alright?" — which is what a duty
 * officer actually has open on the second monitor for eight hours.
 *
 * Three design rules it follows, all of them from the brief and all of them visible:
 *
 *   The summary comes before the detail. Stat tiles across the top, then the shapes,
 *   then the lists. A dashboard that opens on a table makes the reader do the
 *   aggregation.
 *
 *   State is encoded in form as well as number. A camera that is unreachable gets a
 *   colour and a label; an alert's severity sets its pulse rate, so urgency is
 *   perceptible before the text is read.
 *
 *   An absent measurement reads as absent. Every tile distinguishes "none in this
 *   window" from "we have not measured this", because treating the second as zero is
 *   how a dashboard reports a dead ingest as a quiet night.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import {
  BarList,
  BarSeries,
  SERIES,
  SkeletonChart,
  SkeletonStat,
  StackedBar,
  Stat,
} from "../components/charts";
import { GradeBadge, type Grade } from "../components/tiers";
import { ErrorBox } from "../components/ui";
import { WhatAmILookingAt } from "../components/explain";

const WINDOWS = [
  { hours: 6, label: "6h" },
  { hours: 24, label: "24h" },
  { hours: 168, label: "7d" },
] as const;

export default function DashboardPage() {
  const [hours, setHours] = useState<number>(24);

  const counts = useQuery({
    queryKey: ["counts", hours],
    queryFn: () => api.vehicleCounts(hours, hours > 48 ? "day" : "hour"),
    refetchInterval: 60_000,
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(),
    refetchInterval: 60_000,
  });
  const alerts = useQuery({
    queryKey: ["alerts"],
    queryFn: () => api.alerts(),
    refetchInterval: 30_000,
  });
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: () => api.readiness() });

  const window_ = useMemo(() => {
    const to = new Date();
    const from = new Date(to.getTime() - hours * 3600_000);
    return { from: from.toISOString(), to: to.toISOString() };
  }, [hours]);

  const anomalies = useQuery({
    queryKey: ["anomalies", window_.from, window_.to],
    queryFn: () => api.anomalies(window_.from, window_.to),
  });

  /* --- Derived, all of it from data already fetched ------------------------------ */

  const cameraHealth = useMemo(() => {
    const rows = health.data ?? [];
    const byStatus = new Map<string, number>();
    for (const row of rows) byStatus.set(row.status, (byStatus.get(row.status) ?? 0) + 1);
    // `measured_fps` is null until a camera has been probed, which is a different
    // thing from a camera running at 0 fps.
    const measured = rows.filter((r) => r.measured_fps != null);
    const diverging = measured.filter(
      (r) => r.declared_fps != null && Math.abs((r.declared_fps ?? 0) - (r.measured_fps ?? 0)) > 2,
    );
    return { rows, byStatus, measured: measured.length, diverging: diverging.length };
  }, [health.data]);

  const alertRows = alerts.data ?? [];
  const openAlerts = alertRows.filter((a) => a.state !== "RESOLVED");

  const busiest = useMemo(() => {
    const perCamera = counts.data?.by_camera ?? [];
    return perCamera.slice(0, 8).map((row) => ({
      key: row.camera_ref,
      label: row.camera_name || row.camera_ref,
      value: row.reads,
    }));
  }, [counts.data]);

  const series = useMemo(
    () =>
      (counts.data?.windows ?? []).map((w) => ({
        x: w.bucket_start_utc,
        y: w.distinct_plates,
      })),
    [counts.data],
  );

  const gradeMix = useMemo(() => {
    const c = readiness.data?.counts_by_grade ?? {};
    return [
      { key: "capable", value: c.anpr_capable ?? 0, colour: "ok" as const, label: "ANPR capable" },
      { key: "marginal", value: c.marginal_reaim ?? 0, colour: "warn" as const, label: "Marginal" },
      { key: "unviable", value: c.not_viable ?? 0, colour: "muted" as const, label: "Watch only" },
      {
        key: "ungraded",
        value: c.insufficient_sample ?? 0,
        colour: "candidate" as const,
        label: "Not yet graded",
      },
    ];
  }, [readiness.data]);

  const loading = counts.isLoading || health.isLoading;

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <header className="px-4 py-2.5 border-b border-edge flex items-center gap-4">
        <div className="flex-1">
          <h1 className="t-title">Dashboard</h1>
          <p className="t-micro text-muted">Estate activity, health and anomalies</p>
        </div>
        <div className="flex gap-1" role="group" aria-label="Time window">
          {WINDOWS.map((w) => (
            <button
              key={w.hours}
              type="button"
              className={`btn t-small ${hours === w.hours ? "btn-primary" : ""}`}
              aria-pressed={hours === w.hours}
              onClick={() => setHours(w.hours)}
            >
              {w.label}
            </button>
          ))}
        </div>
      </header>

      <WhatAmILookingAt
        what="Live estate activity: vehicles seen, alerts raised, camera health, and anything anomalous in the selected window."
        data="Plate reads and vehicle sightings from the ingest pipeline, alerts from the watchlist matcher, and health from the per-camera probes."
        limits="Counts are of plate reads, not of vehicles — one car in view for two seconds is one read, and a camera that cannot resolve plates contributes almost none. The readiness mix below says which cameras those are."
      />

      {counts.error ? <ErrorBox error={counts.error} /> : null}

      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-3">
        {/* --- Summary before detail ------------------------------------------- */}
        <section
          className="grid gap-2 grid-cols-2 md:grid-cols-3 xl:grid-cols-5"
          aria-label="Summary"
        >
          {loading ? (
            Array.from({ length: 5 }, (_, i) => <SkeletonStat key={i} />)
          ) : (
            <>
              <Stat
                label="Plate reads"
                value={counts.data?.total_reads ?? null}
                hint={`in the last ${hours}h`}
                tone="accent"
              />
              <Stat
                label="Distinct plates"
                value={counts.data?.total_distinct_plates ?? null}
                hint="the honest vehicle proxy"
                tone="accent"
              />
              <Stat
                label="Open alerts"
                value={openAlerts.length}
                hint={`${alertRows.length} total in view`}
                tone={openAlerts.length ? "warn" : "ok"}
              />
              <Stat
                label="Cameras reporting"
                value={cameraHealth.measured}
                unit={`/ ${cameraHealth.rows.length}`}
                hint="have delivered a measurable frame rate"
                tone={cameraHealth.measured ? "ok" : "muted"}
              />
              {/* A count, not a percentage.
                  This tile read "ANPR capable 100%" while the readiness mix beside it
                  read "ANPR capable 0%". Both were arithmetically right -- 2 of 2
                  graded, 2 of 512 overall -- and together they were a contradiction on
                  one screen, with the larger number being the misleading one: a
                  reviewer reads 100% as "the estate can read plates".
                  A percentage of a handful of graded cameras is not an estate
                  statistic. The count is, and the hint says what it is out of. */}
              <Stat
                label="ANPR capable"
                value={
                  readiness.data == null
                    ? null
                    : (readiness.data.counts_by_grade?.anpr_capable ?? 0)
                }
                unit={readiness.data == null ? undefined : `/ ${cameraHealth.rows.length}`}
                hint={
                  !readiness.data?.graded
                    ? "no camera has enough observations to grade yet"
                    : `${readiness.data.graded} camera${
                        readiness.data.graded === 1 ? "" : "s"
                      } graded so far`
                }
                tone="ok"
              />
            </>
          )}
        </section>

        {/* --- Shapes ----------------------------------------------------------- */}
        <section className="grid gap-3 lg:grid-cols-3">
          <div className="panel p-3 lg:col-span-2">
            <h2 className="t-section mb-2">Distinct vehicles over time</h2>
            {counts.isLoading ? (
              <SkeletonChart />
            ) : (
              <BarSeries
                data={series}
                label="Distinct plates"
                colour="accent"
                formatX={(x) =>
                  new Date(x).toLocaleString("en-IN", {
                    hour: "2-digit",
                    minute: "2-digit",
                    day: hours > 48 ? "2-digit" : undefined,
                    month: hours > 48 ? "short" : undefined,
                  })
                }
              />
            )}
          </div>

          <div className="panel p-3">
            <h2 className="t-section mb-2">What the estate can do</h2>
            <StackedBar segments={gradeMix} label="ANPR readiness mix" />
            <p className="t-micro text-muted mt-2">
              Capability, not configuration. A camera is assigned the analytics its
              measured pixel budget can actually support.
            </p>
          </div>
        </section>

        <section className="grid gap-3 lg:grid-cols-2">
          <div className="panel p-3">
            <h2 className="t-section mb-2">Busiest cameras</h2>
            {counts.isLoading ? <SkeletonChart height={160} /> : <BarList rows={busiest} label="Reads per camera" />}
          </div>

          <div className="panel p-3">
            <h2 className="t-section mb-2 flex items-center gap-2">
              Camera health
              {cameraHealth.diverging > 0 ? (
                <span className="badge" style={{ color: SERIES.warn }}>
                  {cameraHealth.diverging} declaring the wrong frame rate
                </span>
              ) : null}
            </h2>
            <BarList
              label="Cameras by status"
              colour="muted"
              rows={[...cameraHealth.byStatus.entries()]
                .sort((a, b) => b[1] - a[1])
                .map(([status, count]) => ({
                  key: status,
                  label: status,
                  value: count,
                  badge:
                    status === "UNREACHABLE" ? (
                      <span
                        className="inline-block w-1.5 h-1.5 rounded-full breathing"
                        style={{ background: SERIES.bad }}
                        aria-label="unreachable"
                      />
                    ) : undefined,
                }))}
            />
            <p className="t-micro text-muted mt-2">
              The divergence badge is the integration guide&rsquo;s own warning made
              visible: a camera claiming 25 fps and delivering 15 is reported, not
              trusted.
            </p>
          </div>
        </section>

        {/* --- Anomalies -------------------------------------------------------- */}
        <section className="panel p-3">
          <h2 className="t-section mb-2">Anomalies in this window</h2>
          <div className="grid gap-3 sm:grid-cols-3">
            {(
              [
                ["cloned_plates", "Cloned plates", "a registration in two places at once"],
                ["convoys", "Convoys", "vehicles repeatedly travelling together"],
                ["dwells", "Dwelling vehicles", "stopped and stayed"],
              ] as const
            ).map(([key, label, hint]) => {
              const rows = (anomalies.data?.[key] ?? []) as Record<string, unknown>[];
              return (
                <div key={key}>
                  <div className="flex items-baseline gap-2">
                    <span className="text-xl font-semibold num">{rows.length}</span>
                    <span className="t-small">{label}</span>
                  </div>
                  <p className="t-micro text-muted">{hint}</p>
                  {anomalies.data?.limits?.[key] ? (
                    <p className="t-micro text-fg3 mt-1 border-l-2 border-edge pl-2">
                      {anomalies.data.limits[key]}
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </section>

        {/* --- Readiness detail, because a grade drives a procurement decision --- */}
        {readiness.data?.cameras?.length ? (
          <section className="panel overflow-hidden">
            <h2 className="t-section px-3 py-2 border-b border-edge">
              Cameras needing attention
            </h2>
            <div className="overflow-x-auto max-h-72">
              <table className="tbl tbl--dense">
                <thead>
                  <tr>
                    <th>Camera</th>
                    <th>Grade</th>
                    <th className="n">Plate px</th>
                    <th className="n">Vehicle px</th>
                    <th>Recommended</th>
                  </tr>
                </thead>
                <tbody>
                  {readiness.data.cameras
                    .filter((c) => c.remedy)
                    .slice(0, 20)
                    .map((camera) => (
                      <tr key={camera.camera_id}>
                        <td>
                          <div className="t-small">{camera.camera_name}</div>
                          <div className="t-micro mono text-muted">{camera.camera_ref}</div>
                        </td>
                        <td>
                          <GradeBadge grade={camera.grade as Grade} />
                        </td>
                        <td className="n">{camera.plate_px_median ?? "—"}</td>
                        <td className="n">{camera.vehicle_px_median ?? "—"}</td>
                        <td className="t-micro text-fg3">{camera.remedy}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}
