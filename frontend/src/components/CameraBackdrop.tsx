import Hls from "hls.js";
import { useEffect, useRef, useState } from "react";
import { mediaUrl } from "../lib/api";

/**
 * The camera's own picture, behind something drawn on it.
 *
 * Unlike the Control Room player this renders no chrome and reports no failure to the
 * viewer. It is a backdrop: when the stream is unavailable the caller keeps working
 * without it, which is the whole reason the zone editor never depended on a frame in
 * the first place. Announcing "live feed unavailable" behind a drawing surface would
 * make a working page look broken.
 *
 * `object-fit: fill` is deliberate and load-bearing. A zone is stored in the camera's
 * frame pixels, and the SVG over this element maps those pixels onto the same box. Any
 * fit that preserves aspect ratio — `cover` crops, `contain` letterboxes — would move
 * the picture relative to that box, and every polygon would sit a little off the thing
 * it was drawn around. Stretching keeps the two coordinate spaces identical.
 */
export default function CameraBackdrop({
  url,
  onReady,
}: {
  url: string | null;
  onReady?: (ok: boolean) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [ok, setOk] = useState(false);

  useEffect(() => {
    const src = mediaUrl(url);
    if (!src || !videoRef.current) {
      setOk(false);
      onReady?.(false);
      return;
    }
    const video = videoRef.current;
    let hls: Hls | null = null;
    let cancelled = false;

    const settle = (good: boolean) => {
      if (cancelled) return;
      setOk(good);
      onReady?.(good);
    };

    // The replay cameras are a bundled MP4 served directly; hls.js cannot play one.
    const isHls = /\.m3u8(\?|$)/i.test(src);

    if (!isHls) {
      video.src = src;
      video.addEventListener("loadeddata", () => settle(true), { once: true });
      video.addEventListener("error", () => settle(false), { once: true });
    } else if (Hls.isSupported()) {
      // The same timeouts the Control Room uses: this estate answers a playlist in
      // about 25 seconds and hls.js gives up after 10 by default.
      hls = new Hls({
        manifestLoadingTimeOut: 60000,
        levelLoadingTimeOut: 60000,
        fragLoadingTimeOut: 90000,
        maxBufferLength: 6,
        backBufferLength: 12,
        startPosition: -1,
      });
      hls.loadSource(src);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => settle(true));
      hls.on(Hls.Events.ERROR, (_e, data) => {
        if (data.fatal) settle(false);
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = src;
      video.addEventListener("loadeddata", () => settle(true), { once: true });
      video.addEventListener("error", () => settle(false), { once: true });
    } else {
      settle(false);
    }

    video.play().catch(() => {
      /* autoplay refusal is not a stream failure; the first frame still paints */
    });

    return () => {
      cancelled = true;
      hls?.destroy();
      video.removeAttribute("src");
      video.load();
    };
  }, [url, onReady]);

  return (
    <video
      ref={videoRef}
      muted
      playsInline
      autoPlay
      loop
      aria-hidden="true"
      className="absolute inset-0 h-full w-full rounded"
      style={{ objectFit: "fill", opacity: ok ? 1 : 0 }}
    />
  );
}
