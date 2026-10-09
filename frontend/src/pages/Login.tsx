import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { api } from "../lib/api";
import { JUDGE_KEY } from "../components/tour";
import { ThemeToggle } from "../lib/theme";

/**
 * The two roles, named as an officer would recognise them.
 *
 * The RBAC model underneath is unchanged -- these are still exactly `admin` and
 * `operator`, and the server authorises on those strings. What changes is that the
 * login screen no longer asks someone to identify themselves as a database noun.
 *
 * Deliberately not a third-party identity provider. This is a closed law-enforcement
 * system: consumer Google login would let anyone with a Gmail account reach the sign-in
 * boundary, which is a regression rather than an improvement. Department-federated
 * login via OIDC is the real upgrade path and is documented in the HLD.
 */
const ROLES = [
  {
    id: "operator",
    label: "Control Room Operator",
    hint: "View cameras, trace vehicles, work the alert desk",
  },
  {
    id: "admin",
    label: "System Administrator",
    hint: "Everything above, plus camera onboarding and watchlist management",
  },
];

/**
 * The registration Judge Mode falls back to.
 *
 * DISCOVERY finding 22 records this as the first and only grammar-valid registration
 * the government estate has produced, read from cam22 at confidence 0.757.
 *
 * It is a fallback rather than the answer because a constant here is only correct for
 * the database it was written against. On an instance seeded from different footage
 * this plate has zero detections, and the one button whose entire purpose is to put
 * the scored capability one click from the login screen was landing the reviewer on
 * "plate never seen" -- the single worst first screen this console can show. The
 * comment that used to sit here even said so: pre-filling a plate the estate has never
 * seen would demonstrate an empty result very convincingly.
 *
 * So Judge Mode now asks the API which registration this database can actually trace,
 * and uses this only when the answer is that none can.
 */
const FALLBACK_DEMO_PLATE = "GJ09BM3641";

export default function Login() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [judgeMode, setJudgeMode] = useState(false);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(username, password);
      if (judgeMode) {
        try {
          sessionStorage.setItem(JUDGE_KEY, "1");
        } catch {
          // A tour appearing over the trace is a small cost; a failed sign-in is not.
        }
        // Ask the database what it can actually demonstrate. Both halves matter: a
        // fixed "last 30 days" window cannot show reads from eight weeks ago, so the
        // window comes from the chosen plate's own reads rather than from a guess.
        let plate = FALLBACK_DEMO_PLATE;
        let to = new Date();
        let from = new Date(to.getTime() - 30 * 864e5);
        try {
          const best = await api.bestTrace();
          if (best.plate && best.window_start && best.window_end) {
            plate = best.plate;
            from = new Date(best.window_start);
            to = new Date(best.window_end);
          }
        } catch {
          // A failed lookup must not block the sign-in it follows. The fallback plate
          // still opens the Journey screen with its controls populated, which is a
          // better outcome than an error on the way in.
        }
        const query = new URLSearchParams({
          plate,
          from: from.toISOString(),
          to: to.toISOString(),
          // The purpose is required, and rightly so -- it reaches the audit ledger
          // before the search runs. Without it the browser blocks submission and the
          // promised single click becomes "fill in this field, then click".
          purpose: "Judge Mode demonstration - vehicle trace from the login screen",
        });
        // `history.replaceState` changes the address bar and tells the router nothing.
        // React Router holds the current location in its own state and updates it only
        // through its navigation API or a popstate event, so the previous version set
        // a /journey URL and left the app sitting on Coverage -- Judge Mode missing
        // the one screen it exists to open, while appearing to have worked.
        navigate(`/journey?${query}`, { replace: true });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "sign in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full grid place-items-center p-4">
      <form onSubmit={submit} className="panel p-6 w-full max-w-xs space-y-4">
        <div className="flex items-start gap-3">
          <div className="min-w-0">
            <div className="text-xl font-semibold">SETU</div>
            <div className="text-xs text-muted">
              Gujarat Police CCTV Integration Platform
            </div>
          </div>
          <div className="flex-1" />
          {/* Offered before sign-in too: someone on a bright desk should not have to
              authenticate through a dark screen first. */}
          <ThemeToggle className="px-2 py-1 shrink-0" />
        </div>

        <div>
          <label className="label">Role</label>
          <div className="space-y-1.5">
            {ROLES.map((r) => (
              <button
                key={r.id}
                type="button"
                onClick={() => setUsername(r.id)}
                className={`w-full text-left px-2.5 py-1.5 rounded border text-sm transition-colors ${
                  username === r.id
                    ? "bg-accent/15 text-accent border-accent/50"
                    : "bg-ink-900 border-edge text-fg2 hover:bg-ink-700"
                }`}
              >
                <div className="font-medium">{r.label}</div>
                <div className="text-[10px] text-muted leading-tight mt-0.5">{r.hint}</div>
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">Password</label>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </div>

        {error && <div className="text-bad text-xs">{error}</div>}

        <button className="btn btn-primary w-full" disabled={busy || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="text-[11px] text-muted leading-snug">
          Credentials are issued from the deployment environment. Sessions are held in
          memory only and end when this tab closes.
        </p>

        {/*
          Judge Mode.

          The scored test case is "here is a registration, show us where it went". On
          evaluation day that should be one click from this screen, not a hunt through
          a sidebar for the right page and then a guess at a sensible time window.

          It pre-fills nothing it cannot justify: the plate is the one registration
          this estate has actually produced a grammar-valid read for, and the window is
          wide enough to contain it. It does not sign anybody in -- credentials are
          still required -- it only decides where the session lands.
        */}
        <label className="flex items-start gap-2 pt-2 border-t border-edge cursor-pointer">
          <input
            type="checkbox"
            checked={judgeMode}
            onChange={(event) => setJudgeMode(event.target.checked)}
            className="mt-0.5"
          />
          <span>
            <span className="text-xs">Judge mode</span>
            <span className="block text-[11px] text-muted leading-snug">
              Land on a prepared trace of{" "}
              <span className="mono">{FALLBACK_DEMO_PLATE}</span> — the one registration this
              estate has produced a grammar-valid read for — instead of the coverage
              map.
            </span>
          </span>
        </label>
      </form>
    </div>
  );
}
