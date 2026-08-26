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
export type JourneyResult = components["schemas"]["JourneyResult"];
export type JourneyHop = components["schemas"]["JourneyHop"];
export type CoverageGap = components["schemas"]["CoverageGap"];
export type RejectedHop = components["schemas"]["RejectedHop"];
export type SyncResult = components["schemas"]["SyncResult"];
export type AuditVerify = components["schemas"]["AuditVerifyOut"];
export type StreamUrl = components["schemas"]["StreamUrlOut"];

const BASE = "/api";

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
  if (init.body && !headers.has("Content-Type"))
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
  health: () => request<CameraHealth[]>("/health/cameras"),
  alerts: (params?: Record<string, string>) =>
    request<Alert[]>(`/alerts${params ? `?${new URLSearchParams(params)}` : ""}`),
  ackAlert: (id: string) => request<Alert>(`/alerts/${id}/ack`, { method: "POST" }),
  resolveAlert: (id: string, disposition: string, note?: string) =>
    request<Alert>(`/alerts/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ disposition, note }),
    }),
  watchlist: () => request<WatchlistEntry[]>("/watchlist"),
  journey: (plate: string, from: string, to: string, purpose: string) =>
    request<JourneyResult>(
      `/journey?${new URLSearchParams({ plate, from, to, purpose })}`,
    ),
  auditVerify: () => request<AuditVerify>("/audit/verify"),
};
