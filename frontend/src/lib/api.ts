/**
 * Typed API client.
 *
 * Request and response shapes come from `api-types.ts`, which is generated from the
 * live OpenAPI schema by `npm run gen:api`. Nothing here is hand-typed: a
 * hand-maintained client is how a console silently drifts from its API, and the
 * drift is only discovered in a demo.
 */
import type { components } from "./api-types";

export type Camera = components["schemas"]["CameraOut"];
export type CameraHealth = components["schemas"]["CameraHealthOut"];
export type Alert = components["schemas"]["AlertOut"];
export type WatchlistEntry = components["schemas"]["WatchlistOut"];
export type WatchlistCreate = components["schemas"]["WatchlistCreate"];
export type JourneyResult = components["schemas"]["JourneyResult"];
export type JourneyHop = components["schemas"]["JourneyHop"];
export type CoverageGap = components["schemas"]["CoverageGap"];
export type RejectedHop = components["schemas"]["RejectedHop"];
export type SyncResult = components["schemas"]["SyncResult"];
export type BulkImportResult = components["schemas"]["BulkImportResult"];
export type VehicleCounts = components["schemas"]["VehicleCountResult"];
export type AuditVerify = components["schemas"]["AuditVerifyOut"];
export type StreamUrl = components["schemas"]["StreamUrlOut"];
export type GapAnalysis = components["schemas"]["GapAnalysis"];
export type DistrictCoverage = components["schemas"]["DistrictCoverage"];
export type CameraGap = components["schemas"]["CameraGap"];
export type JourneyGap = components["schemas"]["JourneyGap"];

/**
 * Where the API lives.
 *
 * **Same-origin (default).** nginx proxies `/api` to the backend in production and
 * the Vite dev server proxies it in development, so the browser never makes a
 * cross-origin credentialed request and the WebSocket rides the same host.
 *
 * **Split-origin.** Set `VITE_API_ORIGIN` to the backend's own origin, e.g.
 * `https://setu-api.up.railway.app`. Needed when the console is hosted somewhere
 * that cannot proxy a WebSocket -- Netlify's redirects proxy HTTP but not `wss://`,
 * so a console served there with a same-origin socket URL loses the live alert feed
 * silently: the list still polls, and the only symptom is the status dot never
 * turning green. The backend must then name this console in SETU_CORS_ORIGINS.
 *
 * Behind nginx the backend's routes sit at the root (`/auth/login`), because the
 * proxy strips the `/api` prefix. Addressed directly, they are at the root too, so
 * the split-origin base carries no prefix.
 */
const API_ORIGIN = (import.meta.env.VITE_API_ORIGIN ?? "").replace(/\/+$/, "");
const BASE = API_ORIGIN || import.meta.env.VITE_API_BASE_URL || "/api";

/** Absolute `ws(s)://` URL for a backend WebSocket path such as `/ws/alerts`. */
export function websocketUrl(path: string): string {
  if (API_ORIGIN) {
    return API_ORIGIN.replace(/^http/, "ws") + path;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${path}`;
}

/**
 * The access token lives in a module variable, never in localStorage.
 * localStorage is readable by any script on the origin, so a single XSS turns into
 * a stolen session that outlives the tab. In memory it dies with the page.
 */
let accessToken: string | null = null;
let onUnauthorised: (() => void) | null = null;

export function setToken(token: string | null) {
  accessToken = token;
}
export function getToken() {
  return accessToken;
}
export function setUnauthorisedHandler(fn: () => void) {
  onUnauthorised = fn;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  // FormData must set its own Content-Type: the browser appends a multipart
  // boundary to it, and overwriting the header with application/json makes the
  // server unable to parse a body that is otherwise perfectly well formed.
  const isFormData = init.body instanceof FormData;
  if (init.body && !isFormData && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");

  const res = await fetch(`${BASE}${path}`, { ...init, headers });

  if (res.status === 401) {
    accessToken = null;
    onUnauthorised?.();
    throw new ApiError(401, "session expired");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* a non-JSON error body is still an error; keep the status text */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function login(username: string, password: string) {
  // The token endpoint is OAuth2 password flow, which is form-encoded, not JSON.
  const body = new URLSearchParams({ username, password });
  const res = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, detail.detail ?? "login failed");
  }
  return (await res.json()) as {
    access_token: string;
    role: string;
    expires_in_s: number;
  };
}

export const api = {
  cameras: (params?: Record<string, string>) =>
    request<Camera[]>(`/cameras${params ? `?${new URLSearchParams(params)}` : ""}`),
  camera: (id: string) => request<Camera>(`/cameras/${id}`),
  patchGeom: (id: string, lat: number, lon: number, note?: string) =>
    request<Camera>(`/cameras/${id}/geom`, {
      method: "PATCH",
      body: JSON.stringify({ lat, lon, confidence_radius_m: 25, note }),
    }),
  streamUrl: (id: string) => request<StreamUrl>(`/cameras/${id}/stream-url`),
  syncCatalogue: () => request<SyncResult>("/cameras/sync-catalogue", { method: "POST" }),
  bulkImportCameras: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<BulkImportResult>("/cameras/bulk-import", { method: "POST", body });
  },
  health: () => request<CameraHealth[]>("/health/cameras"),
  vehicleCounts: (hours = 24, bucket: "minute" | "hour" | "day" = "hour") =>
    request<VehicleCounts>(
      `/analytics/vehicle-counts?${new URLSearchParams({ hours: String(hours), bucket })}`,
    ),
  alerts: (params?: Record<string, string>) =>
    request<Alert[]>(`/alerts${params ? `?${new URLSearchParams(params)}` : ""}`),
  ackAlert: (id: string) => request<Alert>(`/alerts/${id}/ack`, { method: "POST" }),
  resolveAlert: (id: string, disposition: string, note?: string) =>
    request<Alert>(`/alerts/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ disposition, note }),
    }),
  watchlist: (includeExpired = false) =>
    request<WatchlistEntry[]>(
      `/watchlist${includeExpired ? "?include_expired=true" : ""}`,
    ),
  addWatchlistEntry: (body: WatchlistCreate) =>
    request<WatchlistEntry>("/watchlist", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  journey: (plate: string, from: string, to: string, purpose: string) =>
    request<JourneyResult>(
      `/journey?${new URLSearchParams({ plate, from, to, purpose })}`,
    ),
  auditVerify: () => request<AuditVerify>("/audit/verify"),
  gapAnalysis: () => request<GapAnalysis>("/cameras/gap-analysis"),

  /**
   * Signed evidence PDF. Returned as a Blob rather than JSON, and the signature
   * headers travel with it so a recipient can verify the document without a
   * second request that might reconstruct a different route.
   */
  async exportJourney(plate: string, from: string, to: string, purpose: string) {
    const qs = new URLSearchParams({ plate, from, to, purpose });
    const headers = new Headers();
    if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
    const res = await fetch(`${BASE}/journey/export?${qs}`, { headers });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return {
      blob: await res.blob(),
      auditSeq: res.headers.get("X-SETU-Audit-Seq"),
      signature: res.headers.get("X-SETU-Signature"),
      manifestSha256: res.headers.get("X-SETU-Manifest-SHA256"),
    };
  },
};
