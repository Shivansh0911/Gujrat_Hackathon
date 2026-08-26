/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Control-room palette: dark, high contrast, readable at three metres.
        ink: { 900: "#0b0f14", 800: "#111823", 700: "#1a2432", 600: "#26344a" },
        edge: "#2b3a52",
        accent: "#4da3ff",
        ok: "#31c48d",
        warn: "#f0a13a",
        bad: "#f05252",
        muted: "#8ba0bd",
      },
      fontFamily: { mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"] },
    },
  },
  plugins: [],
};
