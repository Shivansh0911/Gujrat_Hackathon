import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { ThemeProvider } from "./lib/theme";
import "./index.css";

/**
 * Fetch the Indic faces up front instead of waiting for Indic text to appear.
 *
 * The stylesheet link and the font stacks were both already correct, and both were
 * insufficient: a browser does not download a webfont until something on screen needs
 * a glyph from it, and nothing on the login screen is in Gujarati. Measured in the
 * browser, `document.fonts.check('16px "Noto Sans Gujarati"')` returned false on a
 * fully loaded page, and the Gujarati sample text was being rendered by a Windows
 * system font that happened to be installed. On a workstation without one -- a kiosk,
 * a locked-down build -- the stack falls through to system-ui and the camera name
 * renders as tofu. That failure would appear on exactly the machines nobody tests on.
 *
 * Asking for the faces by name costs two requests on a warm CDN and makes the
 * behaviour the same everywhere. It is deliberately not awaited: Indic text is not on
 * the first screen, and blocking the render on a font request would trade a rare
 * failure for a universal delay.
 */
if (typeof document !== "undefined" && "fonts" in document) {
  for (const face of ["Noto Sans Gujarati", "Noto Sans Devanagari"]) {
    // A rejected load is not an error worth surfacing: the stack still has
    // system-ui behind it, and a font that failed to fetch must not break the console.
    void document.fonts.load(`400 16px "${face}"`).catch(() => undefined);
    void document.fonts.load(`600 16px "${face}"`).catch(() => undefined);
  }
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      // A 401 is terminal until the operator logs in again; retrying it just delays
      // the redirect and burns requests.
      retry: (count, err) =>
        (err as { status?: number })?.status === 401 ? false : count < 2,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  </React.StrictMode>,
);
