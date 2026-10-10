/**
 * The Onboarding Studio: adding a camera, with the platform showing its working.
 *
 * The problem statement is 26 departments with heterogeneous systems, so onboarding is
 * not a setup screen — it is the product. This page exists because the previous version
 * of onboarding worked and proved nothing: a URL went in, a row came out, and whether
 * the platform had actually talked to the camera was something you took on trust.
 *
 * Every step of the probe is rendered as the platform performs it, and a step that was
 * never reached is drawn as explicitly skipped rather than left out. That distinction
 * is the whole point: in a list of ticks, an absent step and a passing step look the
 * same, and "we could not get that far" is the most useful thing a failed onboarding
 * can say.
 *
 * The declared-versus-measured frame rate is given its own panel rather than a row in a
 * table. The organiser's integration guide warns not to trust the declared rate; a
 * camera on screen announcing 25 fps while delivering 15 demonstrates that the warning
 * was taken seriously in a way no slide does.
 *
 * Probing and creating are deliberately separate. The probe creates nothing. An
 * operator reads the steps, looks at the frame the camera actually produced, sees the
 * grade with its reasoning, and then decides — because showing the working is only
 * worth anything if the person watching is allowed to disagree with it.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ProbeResult, type ProbeStep } from "../lib/api";
import { GradeBadge, type Grade } from "../components/tiers";
import { ErrorBox } from "../components/ui";
import { WhatAmILookingAt } from "../components/explain";

const STATUS_STYLE: Record<string, { colour: string; glyph: string; label: string }> = {
  ok: { colour: "rgb(var(--ok))", glyph: "✓", label: "passed" },
  warn: { colour: "rgb(var(--warn))", glyph: "!", label: "passed with a caveat" },
  fail: { colour: "rgb(var(--bad))", glyph: "✕", label: "failed" },
  skipped: { colour: "rgb(var(--muted))", glyph: "·", label: "never reached" },
};

function StepRow({ step, running }: { step: ProbeStep; running: boolean }) {
  const style = STATUS_STYLE[step.status] ?? STATUS_STYLE.skipped;
  return (
    <li className="flex gap-2.5 py-2 border-b border-edge last:border-0">
      <span
        className={`shrink-0 w-5 h-5 rounded-full grid place-items-center t-micro font-semibold ${
          running ? "breathing" : ""
        }`}
        style={{ color: style.colour, border: `1px solid ${style.colour}` }}
        aria-label={style.label}
      >
        {style.glyph}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="t-small font-medium">{step.label}</span>
          {step.duration_ms != null ? (
            <span className="t-micro num text-muted tabular-nums">
              {step.duration_ms < 1000
                ? `${step.duration_ms.toFixed(0)} ms`
                : `${(step.duration_ms / 1000).toFixed(1)} s`}
            </span>
          ) : null}
        </div>
        <p className="t-micro text-fg3">{step.detail}</p>
      </div>
    </li>
  );
}

/** Declared against measured, given the prominence the integration guide's warning earns. */
function FrameRatePanel({ result }: { result: ProbeResult }) {
  const declared = result.declared_fps;
  const measured = result.measured_fps;
  const divergent = declared != null && measured != null && Math.abs(declared - measured) > 2;

  return (
    <div className="panel p-3">
      <h3 className="t-section mb-2">Frame rate</h3>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="t-micro uppercase tracking-wider text-muted">Camera declares</div>
          <div className="text-2xl font-semibold num tabular-nums text-fg3">
            {declared == null ? "—" : declared.toFixed(1)}
            <span className="t-small font-normal ml-1">fps</span>
          </div>
        </div>
        <div>
          <div className="t-micro uppercase tracking-wider text-muted">Actually delivers</div>
          <div
            className="text-2xl font-semibold num tabular-nums"
            style={{ color: divergent ? "rgb(var(--warn))" : "rgb(var(--ok))" }}
          >
            {measured == null ? "—" : measured.toFixed(1)}
            <span className="t-small font-normal ml-1">fps</span>
          </div>
        </div>
      </div>
      <p className="t-micro text-muted mt-2 pt-2 border-t border-edge">
        {divergent ? (
          <>
            <span className="text-fg3">
              This camera is {Math.abs((declared ?? 0) - (measured ?? 0)).toFixed(1)} fps
              away from what it advertises.
            </span>{" "}
            Nothing on this platform computes a time from the declared rate. Dwell,
            implied speed and the gap between two hops all come from presentation
            timestamps, which is why the discrepancy is reportable rather than harmful.
          </>
        ) : (
          <>
            Measured from presentation timestamps, not from frame count over the
            declared rate. The declared figure is recorded only so the two can be
            compared.
          </>
        )}
      </p>
    </div>
  );
}

export default function OnboardingPage() {
  const [url, setUrl] = useState("");
  const [gradeReadiness, setGradeReadiness] = useState(true);
  const [csv, setCsv] = useState<File | null>(null);
  const [deptCode, setDeptCode] = useState("");
  const [deptName, setDeptName] = useState("");
  const queryClient = useQueryClient();

  const departments = useQuery({ queryKey: ["departments"], queryFn: () => api.departments() });

  const createDepartment = useMutation({
    mutationFn: () => api.createDepartment({ code: deptCode.trim(), name: deptName.trim() }),
    onSuccess: () => {
      setDeptCode("");
      setDeptName("");
      void queryClient.invalidateQueries({ queryKey: ["departments"] });
    },
  });

  const probe = useMutation({
    mutationFn: () => api.probeSource(url.trim(), gradeReadiness),
  });

  const bulk = useMutation({
    mutationFn: (file: File) => api.bulkImportCameras(file),
  });

  const result = probe.data;
  const running = probe.isPending;

  // While the probe is in flight there is no step list yet, so the stages are rendered
  // as pending rows. A spinner would say "something is happening"; this says which
  // thing, which is what somebody waiting on a dead host needs to see.
  const pendingSteps: ProbeStep[] = [
    { key: "validate", label: "URL accepted", status: "skipped", detail: "checking…", data: {} },
    { key: "tcp", label: "Host reachable", status: "skipped", detail: "waiting", data: {} },
    { key: "transport", label: "Transport chosen", status: "skipped", detail: "waiting", data: {} },
    { key: "stream", label: "Stream opened", status: "skipped", detail: "waiting", data: {} },
    { key: "fps", label: "Frame rate", status: "skipped", detail: "waiting", data: {} },
    { key: "frame", label: "First frame decoded", status: "skipped", detail: "waiting", data: {} },
    { key: "readiness", label: "ANPR readiness", status: "skipped", detail: "waiting", data: {} },
    { key: "analytics", label: "Analytics assigned", status: "skipped", detail: "waiting", data: {} },
  ];

  const steps = result?.steps ?? (running ? pendingSteps : []);

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <header className="px-4 py-2.5 border-b border-edge">
        <h1 className="t-title">Onboarding Studio</h1>
        <p className="t-micro text-muted">
          Bring a camera, or a whole department, onto the platform
        </p>
      </header>

      <WhatAmILookingAt
        what="Adding a camera to the platform, with every step of the connection shown as it happens."
        data="A live probe of the source you give it: DNS and address checks, a TCP connection, transport negotiation, a decoded frame, and a readiness grade measured from that frame."
        limits="A probe is a snapshot. It says what the camera was doing in the seconds it was watched, not what it does at night or in rain. The readiness grade from a handful of frames is provisional and is recomputed from real traffic once the camera has been watched."
      />

      <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-3">
        {/* --- One source ----------------------------------------------------- */}
        <section className="panel p-3">
          <h2 className="t-section mb-2">Probe a source</h2>
          <form
            className="flex flex-wrap gap-2 items-end"
            onSubmit={(event) => {
              event.preventDefault();
              if (url.trim()) probe.mutate();
            }}
          >
            <label className="flex-1 min-w-[18rem]">
              <span className="t-micro uppercase tracking-wider text-muted">
                RTSP, HLS or ONVIF device URL
              </span>
              <input
                className="input mono"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="rtsp://camera.department.gov.in:554/stream1"
                spellCheck={false}
                autoComplete="off"
              />
            </label>
            <label className="flex items-center gap-1.5 t-small pb-2">
              <input
                type="checkbox"
                checked={gradeReadiness}
                onChange={(event) => setGradeReadiness(event.target.checked)}
              />
              Grade ANPR readiness
            </label>
            <button
              className="btn btn-primary h-[34px]"
              disabled={running || !url.trim()}
              type="submit"
            >
              {running ? "Probing…" : "Probe"}
            </button>
          </form>
          <p className="t-micro text-muted mt-2">
            The probe opens an outbound connection to the host you name, so it is
            admin-only and every attempt is written to the audit ledger &mdash; the
            failures too, because eleven attempts against a camera that never answers is
            a finding worth having. It creates nothing: you decide afterwards.
          </p>
          {probe.error ? <ErrorBox error={probe.error} /> : null}
        </section>

        {/* --- The working ---------------------------------------------------- */}
        {steps.length > 0 ? (
          <section className="grid gap-3 lg:grid-cols-2">
            <div className="panel p-3">
              <h2 className="t-section mb-1">What the platform did</h2>
              <ul>
                {steps.map((step) => (
                  <StepRow
                    key={step.key}
                    step={step}
                    running={running && step.status === "skipped"}
                  />
                ))}
              </ul>
            </div>

            <div className="flex flex-col gap-3">
              {result?.first_frame_jpeg_b64 ? (
                <figure className="panel p-3">
                  <figcaption className="t-section mb-2">
                    What this camera sees
                    <span className="t-micro text-muted font-normal ml-2 num">
                      {result.width}&times;{result.height}
                      {result.codec ? ` · ${result.codec}` : ""}
                    </span>
                  </figcaption>
                  <img
                    src={`data:image/jpeg;base64,${result.first_frame_jpeg_b64}`}
                    alt="The first frame decoded from this camera"
                    className="w-full rounded border border-edge"
                  />
                </figure>
              ) : null}

              {/* Only once something was measured. A frame-rate panel reading
                  "— fps / — fps" on a host that never answered is two em dashes
                  pretending to be a measurement. */}
              {result && (result.measured_fps != null || result.declared_fps != null) ? (
                <FrameRatePanel result={result} />
              ) : null}

              {result?.grade ? (
                <div className="panel p-3">
                  <h3 className="t-section mb-2 flex items-center gap-2">
                    Readiness
                    <GradeBadge grade={result.grade as Grade} />
                  </h3>
                  <p className="t-small text-fg2">{result.grade_reasoning}</p>
                  {result.recommended_analytics?.length ? (
                    <p className="t-micro text-muted mt-2 pt-2 border-t border-edge">
                      Analytics assigned from the measured pixel budget rather than from
                      configuration:{" "}
                      <span className="mono text-fg3">
                        {result.recommended_analytics?.join(", ")}
                      </span>
                      . A camera is only asked to do what its pixels can support.
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          </section>
        ) : null}

        {/* --- A whole department --------------------------------------------- */}
        <section className="panel p-3">
          <h2 className="t-section mb-2">Onboard a department</h2>

          {/* The problem statement is twenty-six departments with heterogeneous
              systems, so "a new department joins" has to be something an operator can
              do on screen rather than a seed script somebody runs on the server. The
              department is the tenant boundary row-level security is enforced against,
              which is why creating one is admin-only and written to the ledger. */}
          <div className="flex flex-wrap gap-2 items-end mb-3 pb-3 border-b border-edge">
            <label className="flex flex-col">
              <span className="t-micro uppercase tracking-wider text-muted">Code</span>
              <input
                className="input mono w-28"
                value={deptCode}
                onChange={(event) => setDeptCode(event.target.value)}
                placeholder="RTO"
                maxLength={32}
              />
            </label>
            <label className="flex flex-col flex-1 min-w-[14rem]">
              <span className="t-micro uppercase tracking-wider text-muted">
                Department name
              </span>
              <input
                className="input"
                value={deptName}
                onChange={(event) => setDeptName(event.target.value)}
                placeholder="Regional Transport Office, Gandhinagar"
              />
            </label>
            <button
              type="button"
              className="btn h-[34px]"
              disabled={
                deptCode.trim().length < 2 ||
                deptName.trim().length < 2 ||
                createDepartment.isPending
              }
              onClick={() => createDepartment.mutate()}
            >
              {createDepartment.isPending ? "Adding…" : "Add department"}
            </button>
          </div>

          {createDepartment.error ? <ErrorBox error={createDepartment.error} /> : null}

          {departments.data?.length ? (
            <div className="flex flex-wrap gap-1.5 mb-3">
              {departments.data.map((d) => (
                <span
                  key={d.id}
                  className="badge"
                  title={d.contact_email ?? undefined}
                >
                  <span className="mono">{d.code}</span>
                  <span className="text-muted ml-1 num">{d.cameras}</span>
                </span>
              ))}
            </div>
          ) : null}

          <p className="t-small text-muted mb-2">
            A departmental CSV, validated row by row. Rows that fail validation are
            rejected individually with a reason and the rest still import &mdash; a
            spreadsheet with four bad rows should onboard the other four hundred. A
            <span className="mono"> department_code</span> column files each camera
            under its owner; an unknown code falls back to the default and is reported
            rather than silently accepted.
          </p>
          <div className="flex flex-wrap gap-2 items-center">
            <input
              type="file"
              accept=".csv,text/csv"
              className="t-small"
              onChange={(event) => setCsv(event.target.files?.[0] ?? null)}
              aria-label="Departmental camera CSV"
            />
            <button
              className="btn"
              disabled={!csv || bulk.isPending}
              onClick={() => csv && bulk.mutate(csv)}
              type="button"
            >
              {bulk.isPending ? "Importing…" : "Import"}
            </button>
          </div>

          {bulk.error ? <ErrorBox error={bulk.error} /> : null}

          {bulk.data ? (
            <div className="mt-3">
              <div className="flex flex-wrap gap-4 t-small">
                {(
                  [
                    ["created", bulk.data.created, "rgb(var(--ok))"],
                    ["updated", bulk.data.updated, "rgb(var(--accent))"],
                    ["rejected", bulk.data.rejected, "rgb(var(--bad))"],
                    ["rows read", bulk.data.rows_read, "rgb(var(--muted))"],
                  ] as const
                ).map(([label, value, colour]) => (
                  <span key={label}>
                    <span className="num text-xl font-semibold" style={{ color: colour }}>
                      {value}
                    </span>{" "}
                    {label}
                  </span>
                ))}
              </div>
              {bulk.data.unset_coordinates > 0 ? (
                <p className="t-micro text-muted mt-1">
                  {bulk.data.unset_coordinates} row(s) carried no usable coordinate. Those
                  cameras exist but cannot appear on a map or in a journey until one is
                  recorded &mdash; they are not silently placed at a district centroid.
                </p>
              ) : null}
              {bulk.data.note ? (
                <p className="t-micro text-fg3 mt-1">{bulk.data.note}</p>
              ) : null}
              {bulk.data.rejections.length ? (
                <div className="overflow-x-auto max-h-64 mt-2">
                  <table className="tbl tbl--dense">
                    <thead>
                      <tr>
                        <th className="n">Line</th>
                        <th>Reference</th>
                        <th>Why it was rejected</th>
                      </tr>
                    </thead>
                    <tbody>
                      {bulk.data.rejections.map((row, i) => (
                        <tr key={`${row.line}-${i}`}>
                          <td className="n num">{row.line}</td>
                          <td className="mono t-micro">{row.camera_ref || "—"}</td>
                          <td className="t-micro text-fg3">{row.reasons.join("; ")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
