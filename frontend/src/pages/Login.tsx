import { FormEvent, useState } from "react";
import { useAuth } from "../lib/auth";
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

export default function Login() {
  const { signIn } = useAuth();
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
      </form>
    </div>
  );
}
