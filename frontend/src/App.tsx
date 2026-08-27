import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./lib/auth";
import Login from "./pages/Login";
import MapPage from "./pages/MapPage";
import JourneyPage from "./pages/JourneyPage";
import AlertsPage from "./pages/AlertsPage";
import HealthPage from "./pages/HealthPage";
import GapsPage from "./pages/GapsPage";
import WatchlistPage from "./pages/WatchlistPage";
import SystemPage from "./pages/SystemPage";

const NAV = [
  { to: "/map", label: "GIS Map", hint: "Camera registry" },
  { to: "/journey", label: "Journey", hint: "Route reconstruction" },
  { to: "/alerts", label: "Alert Desk", hint: "Live watchlist matches" },
  { to: "/health", label: "Health", hint: "Feed diagnostics" },
  { to: "/gaps", label: "Coverage", hint: "Gap analysis" },
  { to: "/watchlist", label: "Watchlist", hint: "Vehicles being watched" },
  { to: "/system", label: "System", hint: "Audit chain, catalogue" },
];

function Shell() {
  const { authenticated, username, role, signOut } = useAuth();
  if (!authenticated) return <Login />;

  return (
    <div className="h-full flex">
      <aside className="w-52 shrink-0 bg-ink-800 border-r border-edge flex flex-col">
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
                `block px-3 py-2 rounded text-sm transition-colors ${
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
          <Route path="/health" element={<HealthPage />} />
          <Route path="/gaps" element={<GapsPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/system" element={<SystemPage />} />
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
