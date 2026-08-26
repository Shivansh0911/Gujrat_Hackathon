import { FormEvent, useState } from "react";
import { useAuth } from "../lib/auth";

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
    <div className="h-full grid place-items-center">
      <form onSubmit={submit} className="panel p-6 w-80 space-y-4">
        <div>
          <div className="text-xl font-semibold">SETU</div>
          <div className="text-xs text-muted">
            Gujarat Police CCTV Integration Platform
          </div>
        </div>

        <div>
          <label className="label">Username</label>
          <input
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
          />
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
