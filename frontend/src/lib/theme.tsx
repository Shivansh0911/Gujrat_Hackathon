import { createContext, useCallback, useContext, useEffect, useState } from "react";

export type Theme = "dark" | "light";

const KEY = "setu.theme";

/**
 * Which theme to start in.
 *
 * An explicit choice wins, then the operating system, then dark. Dark is the fallback
 * rather than light because this is a control-room console first: the default should
 * be the one that suits a dim room at three metres, and someone on a bright desk has
 * an OS preference that says so.
 *
 * Reading localStorage is wrapped because a browser with site data blocked throws on
 * access rather than returning null, and a theme preference is not worth a blank page.
 */
export function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "dark" || saved === "light") return saved;
  } catch {
    /* private mode, or site data blocked */
  }
  try {
    if (window.matchMedia("(prefers-color-scheme: light)").matches) return "light";
  } catch {
    /* matchMedia is absent in some embedded webviews */
  }
  return "dark";
}

type Ctx = { theme: Theme; setTheme: (t: Theme) => void; toggle: () => void };

const ThemeContext = createContext<Ctx>({
  theme: "dark",
  setTheme: () => {},
  toggle: () => {},
});

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      /* the choice simply will not persist; the session still honours it */
    }
  }, [theme]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const toggle = useCallback(() => setThemeState((t) => (t === "dark" ? "light" : "dark")), []);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggle }}>{children}</ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}

/** Sun and moon as inline SVG, so the control needs no icon font or network fetch. */
export function ThemeToggle({ className = "" }: { className?: string }) {
  const { theme, toggle } = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      onClick={toggle}
      className={`btn inline-flex items-center gap-1.5 ${className}`}
      // The label names the destination, not the current state: a control that says
      // "Dark" while the screen is dark reads as a status, and people click it twice.
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
    >
      <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none"
           stroke="currentColor" strokeWidth="2" strokeLinecap="round">
        {theme === "dark" ? (
          <>
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4" />
          </>
        ) : (
          <path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" />
        )}
      </svg>
      <span className="text-xs">{next === "light" ? "Light" : "Dark"}</span>
    </button>
  );
}
