import { Suspense, lazy, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth";
import { ThemeToggle } from "./lib/theme";
import { Spinner } from "./components/ui";
import Login from "./pages/Login";

/**
 * Screens load when they are opened, not when the console does.
 *
 * Importing all ten eagerly put MapLibre and hls.js into the first bundle, so signing
 * in meant downloading 1.5 MB of JavaScript — around 4.6 seconds on a normal
 * connection — including a map renderer and a video player for a login form that needs
 * neither. Splitting them leaves the shell small and charges each library to the screen
 * that actually uses it.
 *
 * `Login` stays eager on purpose: it is the first thing every visitor sees, and a
 * loading state on the way to a password box is a worse trade than the few kilobytes.
 */
const MapPage = lazy(() => import("./pages/MapPage"));
const JourneyPage = lazy(() => import("./pages/JourneyPage"));
const AlertsPage = lazy(() => import("./pages/AlertsPage"));
const HealthPage = lazy(() => import("./pages/HealthPage"));
const GapsPage = lazy(() => import("./pages/GapsPage"));
const WatchlistPage = lazy(() => import("./pages/WatchlistPage"));
const SystemPage = lazy(() => import("./pages/SystemPage"));
const ControlRoomPage = lazy(() => import("./pages/ControlRoomPage"));
const DemoPage = lazy(() => import("./pages/DemoPage"));
const ZonesPage = lazy(() => import("./pages/ZonesPage"));

/**
 * Fetch a screen's code before anyone asks for it, using time already being spent.
 *
 * Splitting the bundle moved the cost rather than removing it: signing in now lands on
 * the map, which pulls MapLibre's 785 kB at exactly the moment the operator is waiting
 * to see something. But they spend seconds on the login form first, and that time is
 * free. Warming the map there, and the video player once the console is up, spends idle
 * time instead of the operator's.
 *
 * Deliberately failure-tolerant and unawaited. A warm-up that cannot complete must
 * never be visible: the lazy route will simply fetch the chunk itself, exactly as it
 * did before this existed.
 */
export function warmScreens(which: "map" | "video"): void {
  const jobs =
    which === "map"
      ? [() => import("./pages/MapPage")]
      : [
          () => import("./pages/ControlRoomPage"),
          () => import("./pages/ZonesPage"),
          () => import("./pages/DemoPage"),
        ];
  const run = () => {
    for (const job of jobs) job().catch(() => {});
  };
  const idle = (window as { requestIdleCallback?: (cb: () => void) => void }).requestIdleCallback;
  if (idle) idle(run);
  else window.setTimeout(run, 1200);
}

const NAV = [
  { to: "/map", label: "GIS Map", hint: "Camera registry" },
  { to: "/journey", label: "Journey", hint: "Route reconstruction" },
  { to: "/alerts", label: "Alert Desk", hint: "Live watchlist matches" },
  { to: "/control-room", label: "Control Room", hint: "Multi-camera video wall" },
  { to: "/health", label: "Health", hint: "Feed diagnostics" },
  { to: "/gaps", label: "Coverage", hint: "Gap analysis" },
  { to: "/watchlist", label: "Watchlist", hint: "Vehicles being watched" },
  { to: "/zones", label: "Zones", hint: "Intrusion detection areas" },
  { to: "/system", label: "System", hint: "Audit chain, catalogue" },
  { to: "/demo", label: "Demo", hint: "Footage and the reads from it" },
];

function Shell() {
  const { authenticated, username, role, signOut } = useAuth();
  // Drawer state lives here rather than in the sidebar so the backdrop, the toggle and
  // the route change can all close it.
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();

  // Close on navigation. A drawer left open over the page the operator just chose is
  // the single most irritating thing a mobile menu can do.
  useEffect(() => setNavOpen(false), [location.pathname]);

  // Three screens play video and share one 388 kB player. Fetch it once the console is
  // up, so the first tile does not wait for the library and the frame at the same time.
  useEffect(() => {
    if (authenticated) warmScreens("video");
  }, [authenticated]);

  if (!authenticated) return <Login />;

  return (
    <div className="h-full flex flex-col md:flex-row">
      {/*
        Below md the sidebar is 208px of a 375px screen -- 55% of the viewport spent on
        navigation, with the map squeezed out of what remains entirely. `main` carries
        overflow-hidden, so the content did not overflow, it was simply *clipped*, which
        is why a scrollWidth check reported the layout as fine while half of it was
        off-screen. It becomes a drawer instead.
      */}
      <header className="md:hidden flex items-center gap-2 px-3 py-2 bg-ink-800 border-b border-edge shrink-0">
        <button
          aria-label="Open navigation"
          aria-expanded={navOpen}
          className="btn px-2.5 py-1.5"
          onClick={() => setNavOpen((v) => !v)}
        >
          <span aria-hidden="true">{navOpen ? "\u2715" : "\u2630"}</span>
        </button>
        <div className="font-semibold tracking-tight">SETU</div>
        <div className="text-[10px] text-muted truncate">Gujarat CCTV Integration</div>
        <div className="flex-1" />
        <ThemeToggle className="px-2 py-1" />
        <div className="text-[10px] text-muted truncate max-w-[9rem]">{username}</div>
      </header>

      {navOpen && (
        <button
          aria-label="Close navigation"
          className="md:hidden fixed inset-0 z-30 bg-black/60"
          onClick={() => setNavOpen(false)}
        />
      )}

      <aside
        className={`bg-ink-800 border-edge flex flex-col shrink-0
          md:w-52 md:border-r md:static md:translate-x-0
          fixed inset-y-0 left-0 z-40 w-64 border-r
          transition-transform duration-200 ease-out
          motion-reduce:transition-none
          ${navOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"}`}
      >
        <div className="px-4 py-4 border-b border-edge">
          <div className="text-lg font-semibold tracking-tight">SETU</div>
          <div className="text-[11px] text-muted leading-tight mt-0.5">
            Gujarat CCTV Integration
          </div>
        </div>
        <nav className="flex-1 p-2 space-y-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `block px-3 py-2.5 rounded text-sm transition-colors min-h-[44px] ${
                  isActive
                    ? "bg-accent/15 text-accent border border-accent/40"
                    : "text-fg2 hover:bg-ink-700 border border-transparent"
                }`
              }
            >
              <div>{item.label}</div>
              <div className="text-[10px] text-muted">{item.hint}</div>
            </NavLink>
          ))}
        </nav>
        <div className="p-3 border-t border-edge text-xs">
          <div className="text-fg2">{username}</div>
          <div className="text-muted mb-2">{role}</div>
          <ThemeToggle className="w-full justify-center mb-2" />
          <button className="btn w-full" onClick={signOut}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="flex-1 min-w-0 overflow-hidden">
        <Suspense
          fallback={
            <div className="p-6">
              <Spinner />
            </div>
          }
        >
        <Routes>
          <Route path="/map" element={<MapPage />} />
          <Route path="/journey" element={<JourneyPage />} />
          <Route path="/alerts" element={<AlertsPage />} />
          <Route path="/control-room" element={<ControlRoomPage />} />
          <Route path="/health" element={<HealthPage />} />
          <Route path="/gaps" element={<GapsPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/zones" element={<ZonesPage />} />
          <Route path="/system" element={<SystemPage />} />
          <Route path="/demo" element={<DemoPage />} />
          <Route path="*" element={<Navigate to="/map" replace />} />
        </Routes>
        </Suspense>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}
