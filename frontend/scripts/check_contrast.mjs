/**
 * WCAG contrast audit of the theme tokens, computed rather than eyeballed.
 *
 * Reads the `--token: r g b` declarations out of index.css for each theme and reports
 * the contrast ratio of every foreground role against the surfaces it is actually used
 * on. AA is 4.5:1 for body text and 3:1 for large text and non-text indicators.
 *
 * Computed rather than eyeballed because judging a 4.3:1 against a 4.5:1 by eye is not
 * a thing anyone can do, and because a colour nobody looks at twice is exactly where
 * this regresses. The first run found two real failures in the light theme: `muted` at
 * 4.31:1 on raised rows, and `grade-unknown` at 2.32:1 -- which is the badge worn by
 * every camera the estate has not yet graded, so the least legible colour on the
 * palette was also the most frequently shown. The dark theme passed outright.
 *
 * Exit code 1 on any failure, so this can gate a build rather than inform one.
 */
import { readFileSync } from "node:fs";

const css = readFileSync(process.argv[2] ?? new URL("../src/index.css", import.meta.url), "utf8");

/** Pull every `--name: r g b;` inside the given selector block. */
function tokensIn(selectorPattern) {
  const match = css.match(selectorPattern);
  if (!match) return null;
  const out = {};
  for (const m of match[0].matchAll(/--([a-z0-9-]+)\s*:\s*([0-9]{1,3})\s+([0-9]{1,3})\s+([0-9]{1,3})\s*;/gi)) {
    out[m[1]] = [Number(m[2]), Number(m[3]), Number(m[4])];
  }
  return out;
}

const light = tokensIn(/:root\s*\{[\s\S]*?\n\}/);
const darkBlock =
  tokensIn(/:root\[data-theme="dark"\]\s*\{[\s\S]*?\n\}/) ??
  tokensIn(/prefers-color-scheme:\s*dark[\s\S]*?\{[\s\S]*?\{[\s\S]*?\n\s*\}/);

if (!light) {
  console.log("could not find a :root token block");
  process.exit(1);
}
const dark = { ...light, ...(darkBlock ?? {}) };

function luminance([r, g, b]) {
  const f = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function ratio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

// Foreground role -> the minimum it must meet. Text roles 4.5; roles that only ever
// carry a badge, a dot or a chart segment are non-text indicators at 3.0.
const ROLES = {
  fg: 4.5,
  fg2: 4.5,
  fg3: 4.5,
  muted: 4.5,
  accent: 4.5,
  ok: 3.0,
  warn: 3.0,
  bad: 3.0,
  "tier-confirmed": 3.0,
  "tier-probable": 3.0,
  "tier-candidate": 3.0,
  "grade-capable": 3.0,
  "grade-marginal": 3.0,
  "grade-unviable": 3.0,
  "grade-unknown": 3.0,
};

const SURFACES = ["ink-900", "ink-800", "ink-700"];

let failures = 0;
for (const [themeName, tokens] of [
  ["light", light],
  ["dark", dark],
]) {
  console.log(`\n=== ${themeName} ===`);
  for (const [role, minimum] of Object.entries(ROLES)) {
    const fg = tokens[role];
    if (!fg) {
      console.log(`  ${role.padEnd(16)} MISSING in ${themeName}`);
      continue;
    }
    for (const surfaceName of SURFACES) {
      const bg = tokens[surfaceName];
      if (!bg) continue;
      const r = ratio(fg, bg);
      const ok = r >= minimum;
      if (!ok) failures += 1;
      if (!ok) {
        console.log(
          `  FAIL ${role.padEnd(16)} on ${surfaceName.padEnd(8)} ${r.toFixed(2)}:1 (needs ${minimum})`,
        );
      }
    }
  }
}

console.log(`\n${failures === 0 ? "PASS" : `${failures} contrast failures`}`);
