import { useMemo, useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { api, type Camera } from "../lib/api";
import HlsPlayer from "../components/HlsPlayer";
import { Badge, Empty, ErrorBox, NoDataBadge, SourceBadge, Spinner } from "../components/ui";

/**
 * A video wall: several cameras side by side, plus the recorded evidence behind them.
 *
 * Model 2's reference architecture lists "configurable video walls and multi-camera
 * grid views" as a capability, and until now the console could only play one camera
 * at a time, from the map's detail panel.
 *
 * Two things this page is careful about, both learned from the rest of the platform.
 *
 * **It does not pretend a dark camera is a live one.** Every tile says which feed it
 * belongs to and whether that feed is currently reachable. When the government gateway
 * is down — which, measured across 27–31 August, is most of the time — the tiles say
 * so and the page keeps working, rather than filling with spinners that never resolve.
 *
 * **It caps how many streams open at once.** Each tile is an HLS session against
 * infrastructure we do not own, and opening thirty of them because a grid has thirty
 * cells would be exactly the thundering herd the ingest pool's jittered backoff exists
 * to avoid. The wall is deliberately small, and adding a camera is a choice.
 */

const LAYOUTS = [
  { id: "1", label: "1", cols: "grid-cols-1", max: 1 },
  { id: "4", label: "2 × 2", cols: "grid-cols-1 sm:grid-cols-2", max: 4 },
  { id: "6", label: "2 × 3", cols: "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3", max: 6 },
] as const;

export default function ControlRoomPage() {
  const [layout, setLayout] = useState<(typeof LAYOUTS)[number]>(LAYOUTS[1]);
  const [selected, setSelected] = useState<string[]>([]);
  const [picking, setPicking] = useState(false);

  const camerasQuery = useQuery({ queryKey: ["cameras"], queryFn: () => api.cameras() });

  // A retired camera has no stream by definition, so offering one on a video wall is
  // offering a guaranteed failure. Thirty of them appeared here the moment the estate
  // renamed its cameras, and picking one showed "Live feed unavailable" -- which reads
  // as the platform being broken rather than as a camera that no longer exists. They
  // remain in the registry and on the map, because detections still reference them.
  const cameras = {
    ...camerasQuery,
    data: camerasQuery.data?.filter((c) => c.status !== "DECOMMISSIONED"),
  };
  const gateway = useQuery({
    queryKey: ["gateway-status"],
    queryFn: () => api.gatewayStatus(),
    refetchInterval: 30_000,
  });

  // Default to cameras that have actually produced evidence: on a wall of thirty
  // registry positions, the ones with detections behind them are the ones worth
  // watching, and they are also the ones most likely to still be delivering frames.
  const wall = useMemo(() => {
    const all = cameras.data ?? [];
    if (selected.length) {
      return selected
        .map((id) => all.find((c) => c.id === id))
        .filter((c): c is Camera => Boolean(c));
    }
    const withData = all.filter((c) => (c.detection_count ?? 0) > 0);
    return (withData.length ? withData : all).slice(0, layout.max);
  }, [cameras.data, selected, layout.max]);

  const streams = useQueries({
    queries: wall.map((c) => ({
      queryKey: ["stream-url", c.id],
      queryFn: () => api.streamUrl(c.id),
      retry: false,
      staleTime: 60_000,
    })),
  });

  if (cameras.isLoading) return <Spinner label="Loading cameras…" />;
  if (cameras.error) return <div className="p-4"><ErrorBox error={cameras.error} /></div>;

  const gatewayDown = gateway.data?.reachable === false;
  const all = cameras.data ?? [];

  const toggle = (id: string) =>
    setSelected((cur) =>
      cur.includes(id)
        ? cur.filter((x) => x !== id)
        : cur.length >= layout.max
          ? [...cur.slice(1), id]
          : [...cur, id],
    );

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <div className="p-3 border-b border-edge bg-ink-800 flex items-center gap-3 flex-wrap shrink-0">
        <div className="font-medium">Control room</div>
        <Badge tone="muted">
          {wall.length} of {layout.max} tiles
        </Badge>

        {gatewayDown && (
          <Badge tone="bad" title={gateway.data?.last_error ?? ""}>
            government gateway unreachable
          </Badge>
        )}

        <div className="flex-1" />

        <div className="flex gap-1">
          {LAYOUTS.map((l) => (
            <button
              key={l.id}
              className={`btn text-xs ${layout.id === l.id ? "btn-primary" : ""}`}
              onClick={() => {
                setLayout(l);
                setSelected((cur) => cur.slice(0, l.max));
              }}
            >
              {l.label}
            </button>
          ))}
        </div>

        <button className="btn text-xs" onClick={() => setPicking((v) => !v)}>
          {picking ? "Done" : "Choose cameras"}
        </button>
        {selected.length > 0 && (
          <button className="btn text-xs" onClick={() => setSelected([])}>
            Reset
          </button>
        )}
      </div>

      {gatewayDown && (
        <div className="px-3 py-2 bg-bad/10 border-b border-edge text-xs text-muted">
          The government feed has been unreachable
          {gateway.data?.unreachable_since && (
            <> since {new Date(gateway.data.unreachable_since).toLocaleString()}</>
          )}
          . Live tiles for its cameras will not play. Recorded evidence below is
          unaffected — it is already in our own records.
        </div>
      )}

      {picking && (
        <div className="p-3 border-b border-edge bg-ink-900/60 shrink-0 max-h-56 overflow-y-auto">
          <div className="text-[11px] text-muted mb-2">
            Pick up to {layout.max}. Cameras with recorded detections are listed first —
            a registry position with nothing behind it has nothing to show.
          </div>
          <div className="flex flex-wrap gap-1.5">
            {[...all]
              .sort((a, b) => (b.detection_count ?? 0) - (a.detection_count ?? 0))
              .map((c) => {
                const on = wall.some((w) => w.id === c.id);
                return (
                  <button
                    key={c.id}
                    onClick={() => toggle(c.id)}
                    className={`text-[11px] px-2 py-1 rounded border transition-colors ${
                      on
                        ? "bg-accent/15 text-accent border-accent/50"
                        : "bg-ink-800 border-edge text-fg2 hover:bg-ink-700"
                    }`}
                  >
                    <span className="mono">{c.camera_ref}</span>
                    {(c.detection_count ?? 0) > 0 && (
                      <span className="text-muted"> · {c.detection_count}</span>
                    )}
                  </button>
                );
              })}
          </div>
        </div>
      )}

      <div className="flex-1 overflow-auto p-3">
        {wall.length === 0 ? (
          <Empty
            title="No cameras selected"
            detail="Choose cameras to build a wall."
          />
        ) : (
          <div className={`grid ${layout.cols} gap-3`}>
            {wall.map((c, i) => (
              <Tile key={c.id} camera={c} url={streams[i]?.data?.url ?? null} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * One camera on the wall.
 *
 * The player already distinguishes "loading" from "upstream refused", which matters
 * more here than anywhere else: six tiles that all spin forever look like a broken
 * page, where six tiles that each say why they are dark look like a working one
 * reporting bad news.
 */
function Tile({ camera, url }: { camera: Camera; url: string | null }) {
  return (
    <div className="panel overflow-hidden">
      <div className="px-2.5 py-1.5 border-b border-edge flex items-center gap-2 flex-wrap">
        <span className="mono text-xs text-fg2">{camera.camera_ref}</span>
        <span className="text-[11px] text-muted truncate flex-1 min-w-0">{camera.name}</span>
        <SourceBadge sourceType={camera.source_type} />
        <NoDataBadge count={camera.detection_count ?? 0} />
      </div>

      <div className="aspect-video bg-ink-900">
        <HlsPlayer url={url} lastKnownStatus={camera.status} cameraRef={camera.camera_ref} />
      </div>

      <div className="px-2.5 py-1.5 text-[10px] text-muted flex items-center gap-2 flex-wrap">
        <span>{camera.location_text || "location not recorded"}</span>
        <div className="flex-1" />
        {(camera.detection_count ?? 0) > 0 && (
          <span className="tabular-nums">{camera.detection_count} recorded detections</span>
        )}
      </div>
    </div>
  );
}
