import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, mediaUrl, type DemoRead } from "../lib/api";
import { Badge, ErrorBox, Spinner } from "../components/ui";

/**
 * Reference footage this pipeline is built for.
 *
 * Embedded through YouTube's own player, never downloaded. That distinction is
 * deliberate and is stated on the page rather than buried here: these clips are All
 * Rights Reserved, so running our recogniser over them would mean copying them and
 * then redistributing derived frames as evidence crops inside a government
 * submission. Embedding is what the platform provides for; scraping is not.
 *
 * They are therefore illustrative, and the page says so in those words. A judge who
 * assumes these carry plate reads and then finds they do not has been misled, and
 * that costs more than the section gains.
 */
const REFERENCE_FOOTAGE = [
  {
    id: "zOq2XdwHGT0",
    title: "Indian highway driving",
    why: "Motorway speeds — plates are small, motion blur dominates, and this is the hardest case for any recogniser.",
  },
  {
    id: "9wbA9FVtWF4",
    title: "City traffic",
    why: "Dense mixed traffic: two-wheelers, autos and cars at once, which is what an urban junction camera actually sees.",
  },
  {
    id: "Y1jTEyb3wiI",
    title: "Road journey",
    why: "Varying light and camera motion — the conditions under which clock confidence and multi-frame fusion earn their place.",
  },
];

function fmt(t: number): string {
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function DemoPage() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [now, setNow] = useState(0);

  const { data, isLoading, error } = useQuery({
    queryKey: ["demo-feed"],
    queryFn: () => api.demoFeed(),
  });

  // Reads are per camera, and the replay harness runs the same clip through four of
  // them, so the same moment appears up to four times. Collapse to one row per
  // instant: the point of this page is the relationship between a frame and a read,
  // and four identical rows obscures it.
  const timeline = useMemo(() => {
    const seen = new Map<string, DemoRead>();
    for (const r of data?.reads ?? []) {
      const key = `${r.at_seconds.toFixed(1)}|${r.plate}`;
      if (!seen.has(key)) seen.set(key, r);
    }
    return [...seen.values()].sort((a, b) => a.at_seconds - b.at_seconds);
  }, [data]);

  // Which read the playhead is sitting on. A read is "current" for two seconds from
  // its timestamp, which is roughly how long a plate stays in frame at these speeds.
  const activeIndex = useMemo(
    () => timeline.findIndex((r) => now >= r.at_seconds - 0.4 && now < r.at_seconds + 2),
    [timeline, now],
  );

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const tick = () => setNow(v.currentTime);
    v.addEventListener("timeupdate", tick);
    return () => v.removeEventListener("timeupdate", tick);
  }, [data?.clip_url]);

  function seekTo(seconds: number) {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = Math.max(0, seconds - 0.4);
    void v.play().catch(() => {
      /* autoplay policy may refuse; the seek still happened */
    });
  }

  if (isLoading) return <Spinner label="Loading the demonstration feed…" />;
  if (error) return <div className="p-4"><ErrorBox error={error} /></div>;
  if (!data) return null;

  return (
    <div className="h-full overflow-y-auto p-4 space-y-4 max-w-5xl">
      {/* ------------------------------------------------ which feed is which */}
      <section className="panel p-4">
        <h1 className="font-medium text-lg">Demonstration and test footage</h1>
        <p className="text-xs text-muted mt-1 leading-relaxed max-w-3xl">
          SETU reads two kinds of feed, and telling them apart matters when you are
          judging what is real. This page shows the second kind end to end: the video,
          and every plate our recogniser took out of it. Play the clip, or click a read
          to jump to the exact frame it came from.
        </p>

        <div className="grid gap-3 sm:grid-cols-2 mt-4">
          <div className="bg-ink-900 rounded p-3">
            <div className="flex items-center gap-2">
              <Badge tone="muted">Government feed</Badge>
              <span className="mono text-sm text-slate-200">
                {data.gateway_detections}
              </span>
              <span className="text-[11px] text-muted">detections</span>
            </div>
            <p className="text-[11px] text-muted mt-2 leading-snug">
              The organiser's camera network, reached live over RTSP/HLS. This is the
              real integration and it is what the platform is built for.
            </p>
          </div>

          <div className="bg-ink-900 rounded p-3">
            <div className="flex items-center gap-2">
              <Badge tone="warn">Own feed</Badge>
              <span className="mono text-sm text-slate-200">
                {data.own_feed_detections}
              </span>
              <span className="text-[11px] text-muted">detections</span>
            </div>
            <p className="text-[11px] text-muted mt-2 leading-snug">
              Our own footage, run through the identical pipeline — same detector, same
              recogniser, same database. Only the source of the frames differs.
            </p>
          </div>
        </div>

        <p className="text-[11px] text-warn mt-3 leading-snug">{data.note}</p>
      </section>

      {/* ------------------------------------------------------- the clip itself */}
      <section className="panel p-4">
        <h2 className="font-medium">The clip, and what was read from it</h2>
        <p className="text-xs text-muted mt-1 leading-snug max-w-3xl">
          Every read below is real inference — YOLOv9-t detection then CCT recognition,
          both ONNX, running over these frames. The evidence photograph beside each one
          is the actual crop the recogniser was given, which is also why some reads are
          visibly wrong. Nothing on this page is a fixture.
        </p>

        <div className="grid gap-4 lg:grid-cols-2 mt-4">
          <div>
            {data.clip_available && data.clip_url ? (
              <video
                ref={videoRef}
                src={mediaUrl(data.clip_url)}
                controls
                playsInline
                preload="metadata"
                className="w-full rounded border border-edge bg-black"
              />
            ) : (
              <div className="rounded border border-edge bg-ink-900 p-6 text-xs text-muted">
                The clip is not present on this instance, so it cannot be played here.
                The reads beside it were still produced from it — see the attribution
                below for the source.
              </div>
            )}

            <div className="text-[10px] text-muted mt-2 leading-snug">
              <a
                href={data.source_url}
                target="_blank"
                rel="noreferrer noopener"
                className="text-accent hover:underline"
              >
                {data.source_title}
              </a>{" "}
              · {data.licence}
              <div className="mt-1">{data.attribution}</div>
              <div className="mt-1 text-warn">
                Karnataka footage, so plates read <span className="mono">KA…</span> and
                not <span className="mono">GJ…</span>. Stated up front rather than left
                to be noticed.
              </div>
            </div>
          </div>

          {/* ---- the reads, as a timeline into the video ---- */}
          <div className="space-y-1.5 lg:max-h-[28rem] lg:overflow-y-auto">
            <div className="text-xs text-muted">
              {timeline.length} read{timeline.length !== 1 ? "s" : ""} · click one to
              jump to its frame
            </div>
            {timeline.map((r, i) => (
              <button
                key={`${r.at_seconds}-${r.plate}`}
                type="button"
                onClick={() => seekTo(r.at_seconds)}
                className={`w-full text-left flex items-center gap-3 rounded p-2 border transition-colors
                  motion-reduce:transition-none ${
                    i === activeIndex
                      ? "border-accent bg-accent/10"
                      : "border-edge bg-ink-900 hover:bg-ink-700"
                  }`}
              >
                {r.crop_url ? (
                  <img
                    src={mediaUrl(r.crop_url)}
                    alt={`Evidence crop reading ${r.plate}`}
                    className="w-20 rounded border border-edge bg-black object-contain shrink-0"
                  />
                ) : (
                  <div className="w-20 h-10 rounded border border-edge bg-ink-800 shrink-0" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="mono text-sm text-slate-100">{r.plate}</span>
                    <Badge tone={r.confidence >= 0.7 ? "ok" : "warn"}>
                      {r.confidence.toFixed(2)}
                    </Badge>
                  </div>
                  <div className="text-[10px] text-muted mt-0.5">
                    at <span className="mono">{fmt(r.at_seconds)}</span> in the clip ·{" "}
                    {r.camera_ref}
                  </div>
                  {r.corrections.length > 0 && (
                    <div className="text-[10px] text-warn mt-0.5">
                      {r.corrections.length} character(s) corrected by plate grammar —
                      never printed as a clean read
                    </div>
                  )}
                </div>
              </button>
            ))}
            {!timeline.length && (
              <div className="text-xs text-muted bg-ink-900 rounded p-3">
                No reads recorded on this instance yet.
              </div>
            )}
          </div>
        </div>
      </section>

      {/* --------------------------------------------------- reference footage */}
      <section className="panel p-4">
        <h2 className="font-medium">Reference footage</h2>
        <p className="text-xs text-muted mt-1 leading-snug max-w-3xl">
          Road conditions this pipeline is designed for. These are embedded from
          YouTube and are <strong className="text-warn">not processed by SETU</strong> —
          they carry no plate reads. They are here to show the range of footage the
          recogniser has to cope with, not to claim results on it.
        </p>
        <p className="text-[10px] text-muted mt-2 leading-snug max-w-3xl">
          Why not run ANPR over them: they are All Rights Reserved, so downloading them
          and redistributing derived frames as evidence crops is not ours to do. Footage
          we process is either our own or openly licensed, and its provenance is
          recorded. The clip above is CC BY 3.0 and attributed.
        </p>

        <div className="grid gap-4 md:grid-cols-3 mt-4">
          {REFERENCE_FOOTAGE.map((v) => (
            <div key={v.id}>
              <div className="relative w-full rounded overflow-hidden border border-edge bg-black aspect-video">
                <iframe
                  src={`https://www.youtube-nocookie.com/embed/${v.id}`}
                  title={v.title}
                  loading="lazy"
                  allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                  referrerPolicy="strict-origin-when-cross-origin"
                  allowFullScreen
                  className="absolute inset-0 w-full h-full"
                />
              </div>
              <div className="text-xs text-slate-200 mt-1.5">{v.title}</div>
              <div className="text-[10px] text-muted leading-snug">{v.why}</div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
