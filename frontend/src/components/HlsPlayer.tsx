import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";

/**
 * Live preview for one camera.
 *
 * The unavailable state is deliberate and specific. The evaluation gateway has been
 * returning 502 on every playlist for over a day; a spinner that never resolves, or a
 * blank black box, tells an operator nothing and looks broken in a recorded demo.
 * Naming the failure and showing the camera's last known status is the honest and
 * more useful behaviour.
 */
export default function HlsPlayer({
  url,
  lastKnownStatus,
  cameraRef,
}: {
  url: string | null;
  lastKnownStatus: string;
  cameraRef: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<"idle" | "loading" | "playing" | "unavailable">("idle");
  const [detail, setDetail] = useState<string>("");

  useEffect(() => {
    if (!url || !videoRef.current) return;
    const video = videoRef.current;
    setState("loading");
    setDetail("");

    let hls: Hls | null = null;
    let cancelled = false;

    const fail = (why: string) => {
      if (cancelled) return;
      setState("unavailable");
      setDetail(why);
    };

    // Not every camera is an HLS stream. The replay cameras are backed by a bundled
    // MP4 served directly by the API, and hls.js cannot play one -- handing it a
    // progressive file produces a manifest-parse error that reads as "the camera is
    // down". The element plays MP4 natively, with range requests for seeking.
    const isHls = /\.m3u8(\?|$)/i.test(url);

    if (!isHls) {
      video.src = url;
      video.addEventListener("loadedmetadata", () => {
        if (cancelled) return;
        setState("playing");
        video.play().catch(() => {
          /* autoplay refusal is not a stream failure */
        });
      });
      video.addEventListener("error", () => fail("clip could not be opened"));
    } else if (Hls.isSupported()) {
      hls = new Hls({ manifestLoadingMaxRetry: 1, levelLoadingMaxRetry: 1, fragLoadingMaxRetry: 1 });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        if (cancelled) return;
        setState("playing");
        video.play().catch(() => {
          /* autoplay refusal is not a stream failure; the poster stays visible */
        });
      });
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (!data.fatal) return;
        const status = (data.response as { code?: number } | undefined)?.code;
        fail(status ? `upstream returned HTTP ${status}` : `${data.type}: ${data.details}`);
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      video.addEventListener("loadedmetadata", () => !cancelled && setState("playing"));
      video.addEventListener("error", () => fail("stream could not be opened"));
    } else {
      fail("this browser cannot play HLS");
    }

    return () => {
      cancelled = true;
      hls?.destroy();
    };
  }, [url]);

  if (!url) return null;

  if (state === "unavailable") {
    return (
      <div className="panel bg-ink-900 p-4 text-center space-y-2">
        <div className="text-bad text-sm font-medium">Live feed unavailable</div>
        <div className="text-xs text-muted">{detail}</div>
        <div className="text-xs text-muted">
          Camera <span className="mono">{cameraRef}</span> · last known status{" "}
          <span className="text-slate-300">{lastKnownStatus}</span>
        </div>
        <div className="text-[11px] text-muted/80 pt-1 border-t border-edge">
          The registry entry, coordinates and recorded detections for this camera remain
          available. Only live playback is affected.
        </div>
      </div>
    );
  }

  return (
    <div className="relative">
      <video
        ref={videoRef}
        muted
        playsInline
        controls
        className="w-full rounded border border-edge bg-black aspect-video"
      />
      {state === "loading" && (
        <div className="absolute inset-0 grid place-items-center text-xs text-muted">
          connecting…
        </div>
      )}
    </div>
  );
}
