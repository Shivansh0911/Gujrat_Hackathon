import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { getToken, login as apiLogin, setToken, setUnauthorisedHandler } from "./api";

type AuthState = {
  role: string | null;
  username: string | null;
  authenticated: boolean;
  signIn: (u: string, p: string) => Promise<void>;
  signOut: () => void;
};

const Ctx = createContext<AuthState | null>(null);

/** The claims we rely on. The server verifies the signature; this only reads it. */
type Claims = { sub?: string; role?: string; exp?: number };

/**
 * Read a token's payload without verifying it.
 *
 * Deliberately not a trust decision. The signature is checked by the API on every
 * request, and a forged token would be refused there. This exists so a restored
 * session can show the right name and role immediately, and so an obviously expired
 * token is dropped before it causes a sign-in screen to flash past.
 */
function readClaims(token: string): Claims | null {
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json) as Claims;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // Restored from the tab's own storage, so a refresh keeps you signed in and closing
  // the tab does not. Role and username are read back out of the token rather than
  // stored beside it: two copies of the same fact can disagree, and the one the server
  // will actually act on is the token.
  const restored = (() => {
    const token = getToken();
    if (!token) return null;
    const claims = readClaims(token);
    if (!claims?.role) return null;
    // A token past its expiry is not worth restoring: every call would 401 and sign
    // the user straight back out, which looks like the bug this replaced.
    if (claims.exp && claims.exp * 1000 <= Date.now()) {
      setToken(null);
      return null;
    }
    return { role: claims.role, username: claims.sub ?? null };
  })();

  const [role, setRole] = useState<string | null>(restored?.role ?? null);
  const [username, setUsername] = useState<string | null>(restored?.username ?? null);

  const signOut = useCallback(() => {
    setToken(null);
    setRole(null);
    setUsername(null);
  }, []);

  useEffect(() => setUnauthorisedHandler(signOut), [signOut]);

  const signIn = useCallback(async (u: string, p: string) => {
    const res = await apiLogin(u, p);
    setToken(res.access_token);
    setRole(res.role);
    setUsername(u);
  }, []);

  const value = useMemo(
    () => ({ role, username, authenticated: role !== null, signIn, signOut }),
    [role, username, signIn, signOut],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
