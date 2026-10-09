/** @type {import('tailwindcss').Config} */

// Colours resolve to CSS custom properties so one class works in both themes; the
// `<alpha-value>` form is what keeps `bg-accent/20` and friends working. The values
// themselves live in src/index.css, where each theme is chosen rather than inverted.
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          900: token("ink-900"),
          800: token("ink-800"),
          700: token("ink-700"),
          600: token("ink-600"),
        },
        edge: token("edge"),
        accent: token("accent"),
        ok: token("ok"),
        warn: token("warn"),
        bad: token("bad"),
        muted: token("muted"),
        // Text tiers: primary, secondary, tertiary.
        fg: token("fg"),
        fg2: token("fg2"),
        fg3: token("fg3"),
        // Meaning, not decoration. An evidence tier and a readiness grade each get a
        // named role so no component invents its own colour for them.
        tier: {
          confirmed: token("tier-confirmed"),
          probable: token("tier-probable"),
          candidate: token("tier-candidate"),
        },
        grade: {
          capable: token("grade-capable"),
          marginal: token("grade-marginal"),
          unviable: token("grade-unviable"),
          unknown: token("grade-unknown"),
        },
      },
      fontFamily: {
        // IBM Plex Sans for interface text and IBM Plex Mono for technical strings --
        // a matched pair drawn for technical products, with unambiguous figures at
        // 11px. Noto Sans Gujarati and Devanagari follow so Indic camera names render
        // rather than showing tofu.
        sans: [
          "IBM Plex Sans",
          "Noto Sans Gujarati",
          "Noto Sans Devanagari",
          "system-ui",
          "sans-serif",
        ],
        mono: ["IBM Plex Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
