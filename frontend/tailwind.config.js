/** @type {import('tailwindcss').Config} */

// Colours resolve to CSS custom properties so one class works in both themes; the
// `<alpha-value>` form is what keeps `bg-accent/20` and friends working. The values
// themselves live in src/index.css.
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
      },
      fontFamily: { mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"] },
    },
  },
  plugins: [],
};
