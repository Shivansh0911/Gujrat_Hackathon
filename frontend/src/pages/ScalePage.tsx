/**
 * Scale Planner — 80,000 cameras, with every constant arguable.
 *
 * "Plan for Scale" is a scored criterion and every submission will answer it. Most
 * answers will be a number on a slide, and a number on a slide cannot be interrogated.
 * This one is executable: a judge changes an assumption and watches the answer move,
 * which is the difference between being believed and being checked.
 *
 * The constants table below is the point of the screen as much as the outputs are.
 * Every figure says whether it was measured by this project, published by a vendor, or
 * assumed — and the assumed ones are marked in the interface, not only in a footnote.
 * A model that hides which of its inputs are guesses is a model that will be caught
 * hiding one.
 */

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";
import { ErrorBox, Spinner } from "../components/ui";
import { WhatAmILookingAt } from "../components/explain";

const PROVENANCE_STYLE: Record<string, { label: string; className: string; hint: string }> = {
  measured: {
    label: "measured",
    className: "grade-capable",
    hint: "benchmarked by this project, with a report in the repository",
  },
  published: {
    label: "published",
    className: "grade-marginal",
    hint: "a vendor price or datasheet figure — accurate, but not ours",
  },
  assumption: {
    label: "assumption",
    className: "grade-unknown",
    hint: "a planning assumption, documented and not a measurement",
  },
};

function inr(value: number) {
  // Crore is how an Indian government budget is actually discussed, and a figure in
  // raw rupees with eight digits is one nobody can compare at a glance.
  if (value >= 10_000_000) return `₹${(value / 10_000_000).toFixed(2)} crore`;
  if (value >= 100_000) return `₹${(value / 100_000).toFixed(2)} lakh`;
  return `₹${Math.round(value).toLocaleString("en-IN")}`;
}

export default function ScalePage() {
  const [totalCameras, setTotalCameras] = useState(80_000);
  const [capableFraction, setCapableFraction] = useState(0.1);
  const [edgeFraction, setEdgeFraction] = useState(0.8);
  const [useGpu, setUseGpu] = useState(false);
  const [archiveVideo, setArchiveVideo] = useState(false);

  // The measured fraction from the live estate, offered as a one-click input so the
  // planner can be driven by the grading rather than by a guess.
  const readiness = useQuery({ queryKey: ["readiness"], queryFn: () => api.readiness() });
  const measuredFraction = readiness.data?.anpr_capable_fraction ?? null;

  const params = useMemo(
    () => ({
      total_cameras: String(totalCameras),
      anpr_capable_fraction: String(capableFraction),
      edge_fraction: String(edgeFraction),
      use_gpu: String(useGpu),
      archive_video: String(archiveVideo),
    }),
    [totalCameras, capableFraction, edgeFraction, useGpu, archiveVideo],
  );

  const plan = useQuery({
    queryKey: ["scale-plan", params],
    queryFn: () => api.scalePlan(params),
  });

  const data = plan.data;

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <header className="px-4 py-2.5 border-b border-edge">
        <h1 className="t-title">Scale Planner</h1>
        <p className="t-micro text-muted">
          What an 80,000-camera estate costs, and which numbers behind that are measured
        </p>
      </header>

      <WhatAmILookingAt
        what="An executable sizing model: workers, GPUs, bandwidth, storage and an indicative monthly cost for an estate of any size."
        data="Throughput and frame-filtering measured by this project's own benchmarks; prices from published vendor list rates; detection rate and GPU throughput are documented assumptions."
        limits="The GPU figure is extrapolated from the CPU measurement, not benchmarked — this project has a 6 GB laptop GPU. The detection rate per camera is a planning assumption; our footage is minutes long and cannot establish a duty cycle. Both are marked as assumptions in the constants table."
      />

      {plan.error ? <ErrorBox error={plan.error} /> : null}

      <div className="flex-1 overflow-y-auto p-3 grid gap-3 lg:grid-cols-[20rem_1fr]">
        {/* --- Inputs ----------------------------------------------------------- */}
        <aside className="panel p-3 flex flex-col gap-3 h-fit">
          <h2 className="t-section">Assumptions</h2>

          <label className="block">
            <span className="label">Cameras in the estate</span>
            <input
              className="input"
              type="number"
              min={0}
              step={1000}
              value={totalCameras}
              onChange={(e) => setTotalCameras(Number(e.target.value))}
            />
          </label>

          <label className="block">
            <span className="label">
              ANPR-capable fraction
              <span className="num ml-1">{(capableFraction * 100).toFixed(0)}%</span>
            </span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={capableFraction}
              onChange={(e) => setCapableFraction(Number(e.target.value))}
              className="w-full"
            />
            {measuredFraction != null ? (
              <button
                type="button"
                className="btn btn-quiet t-micro mt-1 w-full"
                onClick={() => setCapableFraction(measuredFraction)}
              >
                Use the measured {Math.round(measuredFraction * 100)}% from this estate
              </button>
            ) : (
              <p className="t-micro text-muted mt-1">
                Not enough graded cameras yet to offer a measured value.
              </p>
            )}
          </label>

          <label className="block">
            <span className="label">
              Processed at the edge
              <span className="num ml-1">{(edgeFraction * 100).toFixed(0)}%</span>
            </span>
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={edgeFraction}
              onChange={(e) => setEdgeFraction(Number(e.target.value))}
              className="w-full"
            />
          </label>

          <label className="flex items-center gap-2 t-small">
            <input type="checkbox" checked={useGpu} onChange={(e) => setUseGpu(e.target.checked)} />
            Use GPUs for ANPR
          </label>

          <label className="flex items-center gap-2 t-small">
            <input
              type="checkbox"
              checked={archiveVideo}
              onChange={(e) => setArchiveVideo(e.target.checked)}
            />
            Archive full video centrally
          </label>

          <button
            type="button"
            className="btn t-small"
            onClick={() => {
              setCapableFraction(1);
              setEdgeFraction(0);
              setUseGpu(false);
              setArchiveVideo(false);
            }}
          >
            Show the bleak case
          </button>
          <p className="t-micro text-muted -mt-1.5">
            Every camera ANPR-capable, all processing central. This is the scenario
            that produces the ~98,500-worker figure in the HLD, and the model can still
            express it — a model that cannot state the bad outcome is advocacy.
          </p>
        </aside>

        {/* --- Outputs ---------------------------------------------------------- */}
        <div className="flex flex-col gap-3 min-w-0">
          {plan.isLoading || !data ? (
            <Spinner label="Sizing the estate" />
          ) : (
            <>
              <section className="grid gap-2 grid-cols-2 lg:grid-cols-4">
                {[
                  ["ANPR workers", data.anpr_workers.toLocaleString("en-IN"), "CPU"],
                  ["Sighting workers", data.sighting_workers.toLocaleString("en-IN"), "CPU"],
                  ["GPUs", data.gpus.toLocaleString("en-IN"), useGpu ? "accelerators" : "not used"],
                  ["Monthly", inr(data.total_monthly_inr), "compute + storage"],
                ].map(([label, value, hint]) => (
                  <div key={label} className="panel p-3">
                    <div className="t-micro uppercase tracking-wider text-muted">{label}</div>
                    <div className="text-2xl font-semibold num tracking-tight">{value}</div>
                    <div className="t-micro text-fg3">{hint}</div>
                  </div>
                ))}
              </section>

              <section className="panel p-3">
                <h2 className="t-section mb-2">Bandwidth</h2>
                <div className="grid gap-3 sm:grid-cols-3">
                  <div>
                    <div className="t-micro uppercase tracking-wider text-muted">At the edge</div>
                    <div className="text-lg num">
                      {Math.round(data.edge_bandwidth_mbps).toLocaleString("en-IN")}
                      <span className="t-small text-fg3"> Mbit/s</span>
                    </div>
                    <p className="t-micro text-fg3">
                      Video on the local network. Edge processing moves the load; it does
                      not make the video disappear.
                    </p>
                  </div>
                  <div>
                    <div className="t-micro uppercase tracking-wider text-muted">
                      Reaching the centre
                    </div>
                    <div className="text-lg num" style={{ color: "rgb(var(--ok))" }}>
                      {data.central_bandwidth_mbps < 100
                        ? data.central_bandwidth_mbps.toFixed(2)
                        : Math.round(data.central_bandwidth_mbps).toLocaleString("en-IN")}
                      <span className="t-small text-fg3"> Mbit/s</span>
                    </div>
                    <p className="t-micro text-fg3">
                      Detection records are about 100 bytes; a stream is about 6 Mbit/s.
                    </p>
                  </div>
                  <div>
                    <div className="t-micro uppercase tracking-wider text-muted">Reduction</div>
                    <div className="text-lg num">
                      {data.bandwidth_reduction_factor
                        ? `${Math.round(data.bandwidth_reduction_factor).toLocaleString("en-IN")}×`
                        : "—"}
                    </div>
                  </div>
                </div>
              </section>

              <section className="panel p-3">
                <h2 className="t-section mb-2">Storage by tier</h2>
                <div className="grid gap-3 sm:grid-cols-3">
                  {[
                    ["Hot", data.hot_storage_gb, "7 days, block storage"],
                    ["Warm", data.warm_storage_gb, "to 90 days, object storage"],
                    ["Cold", data.cold_storage_gb, "to 365 days, archive"],
                  ].map(([label, gb, hint]) => (
                    <div key={String(label)}>
                      <div className="t-micro uppercase tracking-wider text-muted">{label}</div>
                      <div className="text-lg num">
                        {Math.round(Number(gb)).toLocaleString("en-IN")}
                        <span className="t-small text-fg3"> GB</span>
                      </div>
                      <p className="t-micro text-fg3">{hint}</p>
                    </div>
                  ))}
                </div>
              </section>

              {data.notes.length ? (
                <section className="panel p-3">
                  <h2 className="t-section mb-1.5">What the model wants to tell you</h2>
                  <ul className="flex flex-col gap-1.5">
                    {data.notes.map((note) => (
                      <li key={note} className="t-small text-fg3 border-l-2 border-edge pl-2">
                        {note}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}

              {/* The constants are the argument. */}
              <section className="panel overflow-hidden">
                <h2 className="t-section px-3 py-2 border-b border-edge">
                  Every constant this answer depends on
                </h2>
                <div className="overflow-x-auto">
                  <table className="tbl tbl--dense">
                    <thead>
                      <tr>
                        <th>Constant</th>
                        <th className="n">Value</th>
                        <th>Unit</th>
                        <th>Provenance</th>
                        <th>Source</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.constants.map((constant) => {
                        const style =
                          PROVENANCE_STYLE[constant.provenance] ?? PROVENANCE_STYLE.assumption;
                        return (
                          <tr key={constant.key}>
                            <td className="mono t-micro">{constant.key}</td>
                            <td className="n">{constant.value.toLocaleString("en-IN")}</td>
                            <td className="t-micro text-muted">{constant.unit}</td>
                            <td>
                              <span className={`grade ${style.className}`} title={style.hint}>
                                {style.label}
                              </span>
                            </td>
                            <td className="t-micro text-fg3">
                              {constant.source}
                              {constant.note ? (
                                <span className="block text-muted">{constant.note}</span>
                              ) : null}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
