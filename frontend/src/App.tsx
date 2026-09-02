import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth";
import Login from "./pages/Login";
import MapPage from "./pages/MapPage";
import JourneyPage from "./pages/JourneyPage";
import AlertsPage from "./pages/AlertsPage";
import HealthPage from "./pages/HealthPage";
import GapsPage from "./pages/GapsPage";
import WatchlistPage from "./pages/WatchlistPage";
import SystemPage from "./pages/SystemPage";
import ControlRoomPage from "./pages/ControlRoomPage";
import DemoPage from "./pages/DemoPage";
import ZonesPage from "./pages/ZonesPage";

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
                    : "text-slate-300 hover:bg-ink-700 border border-transparent"
                }`
              }
            >
              <div>{item.label}</div>
              <div className="text-[10px] text-muted">{item.hint}</div>
            </NavLink>
          ))}
        </nav>
        <div className="p-3 border-t border-edge text-xs">
          <div className="text-slate-300">{username}</div>
          <div className="text-muted mb-2">{role}</div>
          <button className="btn w-full" onClick={signOut}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="flex-1 min-w-0 overflow-hidden">
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
