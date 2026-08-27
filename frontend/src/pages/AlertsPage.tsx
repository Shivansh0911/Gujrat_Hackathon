import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, getToken, type Alert, websocketUrl } from "../lib/api";
import { Badge, Empty, ErrorBox, Spinner } from "../components/ui";

export default function AlertsPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [stateFilter, setStateFilter] = useState("");
  const [live, setLive] = useState<"connecting" | "live" | "offline">("connecting");
  const wsRef = useRef<WebSocket | null>(null);

  const { data: alerts, isLoading, error } = useQuery({
    queryKey: ["alerts", stateFilter],
    queryFn: () => api.alerts(stateFilter ? { state: stateFilter } : undefined),
    refetchInterval: 15_000,
  });

  // Live feed. The token goes in the query string because a browser cannot set
  // headers on a WebSocket handshake; the server verifies it before accepting.
  useEffect(() => {
    const token = getToken();
    if (!token) return;
    // websocketUrl() follows VITE_API_ORIGIN when the console is served from a
    // different host to the API, which is the case on Netlify: its redirects proxy
    // HTTP but not wss://, so a same-origin socket URL would fail there with no
    // visible error beyond a status dot that never turns green.
    const ws = new WebSocket(
      `${websocketUrl("/ws/alerts")}?token=${encodeURIComponent(token)}`,
    );
    wsRef.current = ws;

    ws.onopen = () => setLive("live");
    ws.onclose = () => setLive("offline");
    ws.onerror = () => setLive("offline");
    ws.onmessage = () => {
      // The payload is a nudge, not the source of truth: refetching keeps the list
      // consistent with scoping and filters rather than trusting a pushed object.
      qc.invalidateQueries({ queryKey: ["alerts"] });
    };
    return () => ws.close();
  }, [qc]);

  const ack = useMutation({
    mutationFn: (id: string) => api.ackAlert(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });
  const resolve = useMutation({
    mutationFn: ({ id, disposition }: { id: string; disposition: string }) =>
      api.resolveAlert(id, disposition),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });

  if (isLoading) return <Spinner label="Loading alerts…" />;
  if (error) return <div className="p-4"><ErrorBox error={error} /></div>;

  return (
    <div className="h-full flex flex-col">
      <div className="p-3 border-b border-edge bg-ink-800 flex items-center gap-3">
        <div className="font-medium">Alert desk</div>
        <Badge tone={live === "live" ? "ok" : live === "connecting" ? "muted" : "bad"}>
          {live === "live" ? "live feed connected" : live === "connecting" ? "connecting…" : "live feed offline"}
        </Badge>
        <div className="flex-1" />
        <select className="input w-44" value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
          <option value="">All states</option>
          {["RAISED", "ACKNOWLEDGED", "RESOLVED"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <Badge tone="muted">{alerts?.length ?? 0} shown</Badge>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {!alerts?.length && (
          <Empty
            title="No alerts"
            detail="No watchlisted vehicle has been seen in this scope. Alerts appear here within seconds of a match."
          />
        )}
        {alerts?.map((a) => (
          <AlertCard
            key={a.id}
            alert={a}
            onAck={() => ack.mutate(a.id)}
            onResolve={(d) => resolve.mutate({ id: a.id, disposition: d })}
            onTrace={() => navigate(`/journey?plate=${encodeURIComponent(a.matched_value)}`)}
            busy={ack.isPending || resolve.isPending}
          />
        ))}
      </div>
    </div>
  );
}

function AlertCard({
  alert,
  onAck,
  onResolve,
  onTrace,
  busy,
}: {
  alert: Alert;
  onAck: () => void;
  onResolve: (disposition: string) => void;
  onTrace: () => void;
  busy: boolean;
}) {
  const [showResolve, setShowResolve] = useState(false);
  const corrections = (alert.corrections ?? []) as Array<Record<string, unknown>>;
  const priorityTone = alert.priority >= 0.8 ? "bad" : alert.priority >= 0.6 ? "warn" : "muted";

  return (
    <div className={`panel p-3 space-y-3 ${alert.state === "RESOLVED" ? "opacity-60" : ""}`}>
      <div className="flex items-start gap-3">
        {alert.crop_url ? (
          <img
            src={alert.crop_url}
            alt={`Evidence crop for ${alert.matched_value}`}
            className="w-32 rounded border border-edge bg-black object-contain shrink-0"
          />
        ) : (
          <div className="w-32 h-16 rounded border border-edge bg-ink-900 grid place-items-center text-[10px] text-muted shrink-0">
            no crop
          </div>
        )}

        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="mono text-lg text-slate-100">{alert.matched_value}</span>
            <Badge tone={priorityTone}>priority {alert.priority.toFixed(2)}</Badge>
            <Badge tone={alert.match_type === "exact" ? "ok" : "warn"}>
              {alert.match_type} · {alert.match_score.toFixed(2)}
            </Badge>
            {alert.is_movement && (
              <Badge tone="accent" title="Sightings at more than one camera, grouped as one developing event">
                movement · {alert.observation_count} sightings
              </Badge>
            )}
            {alert.state !== "RAISED" && <Badge tone="muted">{alert.state}</Badge>}
            {alert.disposition && <Badge tone="muted">{alert.disposition}</Badge>}
          </div>

          <div className="text-xs text-muted">
            {alert.camera_name}
            {alert.camera_lat != null && (
              <span className="mono ml-2">
                {alert.camera_lat.toFixed(4)}, {alert.camera_lon!.toFixed(4)}
              </span>
            )}
          </div>

          <div className="grid grid-cols-3 gap-2 text-[11px]">
            <div>
              <div className="text-muted text-[10px] uppercase">Observed (UTC)</div>
              <div className="mono text-slate-300">
                {new Date(alert.observed_at_utc).toISOString().replace("T", " ").slice(0, 19)}
              </div>
            </div>
            <div>
              <div className="text-muted text-[10px] uppercase">Stream PTS</div>
              <div className="mono text-slate-300">
                {alert.detection_pts_ms != null ? `${(alert.detection_pts_ms / 1000).toFixed(1)}s` : "—"}
              </div>
            </div>
            <div>
              <div className="text-muted text-[10px] uppercase">Read confidence</div>
              <div className="mono text-slate-300">
                {alert.detection_confidence != null ? alert.detection_confidence.toFixed(2) : "—"}
              </div>
            </div>
          </div>

          {/* Corrections are shown, never hidden: an operator acting on a corrected
              plate must be able to see exactly what was rewritten. */}
          {corrections.length > 0 && (
            <div className="text-[11px] bg-warn/10 border border-warn/30 rounded px-2 py-1.5">
              <div className="text-warn font-medium mb-0.5">
                {corrections.length} character correction{corrections.length > 1 ? "s" : ""} applied
              </div>
              {corrections.map((c, i) => (
                <div key={i} className="text-muted mono">
                  position {String(c.position)}: {String(c.raw)} → {String(c.corrected)} (confidence{" "}
                  {String(c.confidence)})
                </div>
              ))}
            </div>
          )}

          <div className="text-[11px] text-muted">
            <span className="text-slate-300">{alert.watchlist_name}</span>
            {alert.watchlist_authority && <> · {alert.watchlist_authority}</>}
            {alert.watchlist_case_ref && <> · case <span className="mono">{alert.watchlist_case_ref}</span></>}
          </div>

          {alert.acknowledged_by && (
            <div className="text-[11px] text-muted">
              acknowledged by <span className="text-slate-300">{alert.acknowledged_by}</span>
            </div>
          )}
        </div>
      </div>

      {alert.is_movement && alert.sightings.length > 1 && (
        <div className="border-t border-edge pt-2">
          <div className="text-[11px] text-muted mb-1">Sighting sequence</div>
          <div className="flex gap-2 overflow-x-auto">
            {(alert.sightings as Array<Record<string, unknown>>).map((s, i) => (
              <div key={i} className="text-[10px] bg-ink-900 rounded px-2 py-1 shrink-0">
                <div className="text-slate-300">{String(s.camera_name ?? "—")}</div>
                <div className="mono text-muted">
                  {String(s.observed_at_utc).slice(11, 19)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {alert.state !== "RESOLVED" && (
        <div className="flex gap-2 border-t border-edge pt-2">
          {alert.state === "RAISED" && (
            <button className="btn" onClick={onAck} disabled={busy}>Acknowledge</button>
          )}
          <button className="btn" onClick={() => setShowResolve((v) => !v)} disabled={busy}>
            Resolve…
          </button>
          <button className="btn btn-primary ml-auto" onClick={onTrace}>
            Trace this vehicle
          </button>
        </div>
      )}

      {showResolve && (
        <div className="border-t border-edge pt-2 space-y-2">
          <div className="text-[11px] text-muted">
            The disposition feeds the per-camera false-positive rate on the Health
            screen — it is how the platform measures its own precision.
          </div>
          <div className="flex gap-2">
            {[
              ["true_positive", "True positive"],
              ["false_positive", "False positive"],
              ["unable_to_verify", "Unable to verify"],
            ].map(([value, label]) => (
              <button key={value} className="btn text-xs" onClick={() => onResolve(value)} disabled={busy}>
                {label}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
