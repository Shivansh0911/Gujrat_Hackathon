import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { login as apiLogin, setToken, setUnauthorisedHandler } from "./api";

type AuthState = {
  role: string | null;
  username: string | null;
  authenticated: boolean;
  signIn: (u: string, p: string) => Promise<void>;
  signOut: () => void;
};

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // Token state lives here and in the api module, never in localStorage: a single
  // XSS on the origin would otherwise yield a session that outlives the tab.
  const [role, setRole] = useState<string | null>(null);
  const [username, setUsername] = useState<string | null>(null);

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
