/**
 * Measure every console screen at four widths, and fail on horizontal overflow.
 *
 * Responsiveness kept getting cut for time, and "it looked fine when I resized the
 * window" is not a measurement. This one is: for each page at each viewport it
 * compares `document.documentElement.scrollWidth` against the viewport width, and any
 * page whose body scrolls sideways is a failure with the offending element named.
 *
 * A page body that scrolls horizontally on a phone is the specific defect worth
 * catching. Wide content -- tables, the map -- is allowed to scroll *inside its own
 * container*; what must never happen is the whole layout sliding, because then the
 * navigation and half the controls are simply off-screen.
 *
 * A warning about the browser this uses. Playwright's bundled Chromium is the
 * open-source build, which ships **without H.264**. It reports "format not supported"
 * on perfectly good video, so it cannot be used to judge whether media plays -- doing
 * exactly that produced a confident and completely wrong diagnosis in this project:
 * the console was declared broken while it was working in Edge and Chrome. Launch with
 * `{ channel: "msedge" }` or `{ channel: "chrome" }` before believing anything about
 * playback. The broken-video check below is still worth having for a *missing* source,
 * which is codec-independent.
 *
 *   node scripts/responsive_audit.mjs
 *   SETU_CONSOLE_URL=https://setu-gujrat.netlify.app node scripts/responsive_audit.mjs
 */
import { chromium } from "playwright";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");
const OUT = join(ROOT, "docs", "screenshots", "responsive");
const BASE = process.env.SETU_CONSOLE_URL ?? "http://127.0.0.1:5173";

/**
 * Which theme to audit. The console has a light mode as well as a dark one, and a
 * layout can be sound in one and unreadable in the other: a colour that only exists
 * in the dark palette leaves light-mode text sitting on its own background. Set
 * SETU_THEME=light to check that side, or `both` to run the whole sweep twice.
 *
 * Injected as an init script so the preference is in place before the app's own
 * pre-paint stamp reads it -- setting it after load would audit a repaint.
 */
const THEME = process.env.SETU_THEME ?? "dark";
const SUFFIX = THEME === "light" ? "-light" : "";

/** iPhone SE, iPhone 14, iPad portrait, small laptop. */
const VIEWPORTS = [
  { name: "375", width: 375, height: 812 },
  { name: "390", width: 390, height: 844 },
  { name: "768", width: 768, height: 1024 },
  { name: "1024", width: 1024, height: 768 },
];

const PAGES = [
  { name: "map", nav: "GIS Map" },
  { name: "journey", nav: "Journey" },
  { name: "alerts", nav: "Alert Desk" },
  { name: "health", nav: "Health" },
  { name: "coverage", nav: "Coverage" },
  { name: "watchlist", nav: "Watchlist" },
  { name: "system", nav: "System" },
  { name: "controlroom", nav: "Control Room", optional: true },
  { name: "demo", nav: "Demo", optional: true },
  { name: "zones", nav: "Zones", optional: true },
];

function secret(key) {
  const envFile = process.env.SETU_ENV_FILE ?? "deploy-secrets.env";
  try {
    const line = readFileSync(join(ROOT, envFile), "utf8")
      .split(/\r?\n/)
      .find((l) => l.startsWith(`${key}=`));
    if (line) return line.slice(key.length + 1).trim();
  } catch {
    /* fall through to .env */
  }
  const line = readFileSync(join(ROOT, ".env"), "utf8")
    .split(/\r?\n/)
    .find((l) => l.startsWith(`${key}=`));
  if (!line) throw new Error(`${key} not found in ${envFile} or .env`);
  return line.slice(key.length + 1).trim();
}

/**
 * Sign in.
 *
 * The role is chosen with a button, not typed into a field -- the login screen names
 * the two roles operationally rather than asking an officer to type a database noun.
 * Scripts that still filled `input[autocomplete="username"]` broke silently when that
 * changed, which is why this is one helper rather than three copies.
 */
async function signIn(page, password) {
  await page.addInitScript((theme) => {
    try {
      localStorage.setItem("setu.theme", theme);
    } catch {
      /* the app falls back to its own default */
    }
  }, THEME === "both" ? "dark" : THEME);
  await page.goto(BASE, { waitUntil: "networkidle" });
  const roleButton = page.locator('button:has-text("System Administrator")');
  if (await roleButton.count()) await roleButton.first().click();
  await page.fill('input[autocomplete="current-password"]', password);
  await page.click('button:has-text("Sign in")');
  await page.waitForSelector("nav", { timeout: 25000 });
}

/** Open the page, coping with the nav being behind a menu button on narrow screens. */
async function navigateTo(page, label) {
  const burger = page.locator('button[aria-label="Open navigation"]');
  if (await burger.count()) {
    const visible = await burger.first().isVisible();
    if (visible) await burger.first().click();
  }
  const link = page.locator(`a:has-text("${label}")`).first();
  if (!(await link.count())) return false;
  await link.click();
  await page.waitForTimeout(1400);
  return true;
}

const problems = [];

async function measure(page, pageName, vp) {
  const metrics = await page.evaluate(() => {
    const doc = document.documentElement;
    const overflowing = [];
    // Clipped, not merely overflowing. `overflow-hidden` on a container means content
    // wider than the viewport is silently cut off instead of scrolling, so scrollWidth
    // never grows and a naive check reports the layout as fine while half of it is
    // off-screen. That exact false pass is why this looks at element geometry too.
    const clipped = [];

    // Content extending past the viewport is only a defect if it cannot be reached.
    // A ten-column table inside `overflow-x-auto` is *supposed* to be wider than the
    // screen -- that is the fix, not the bug -- so anything with a horizontally
    // scrollable ancestor is reachable and therefore fine. Without this the audit
    // flags the remedy as the fault, which it did on the first two runs.
    const reachableByScrolling = (el) => {
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX;
        if (ox === "auto" || ox === "scroll") return true;
      }
      return false;
    };

    for (const el of document.querySelectorAll("main *")) {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.right > window.innerWidth + 2) {
        const cls = typeof el.className === "string" ? el.className.slice(0, 50) : "";
        const desc = `${el.tagName.toLowerCase()}.${cls}`.slice(0, 70);
        overflowing.push(desc);
        // More than a quarter of the element beyond the right edge, and no way to
        // scroll to it, is content the operator simply cannot read.
        if (r.right - window.innerWidth > r.width * 0.25 && !reachableByScrolling(el)) {
          clipped.push(desc);
        }
      }
    }
    // How much of the width the navigation is consuming.
    const aside = document.querySelector("aside");
    const asideRect = aside ? aside.getBoundingClientRect() : null;
    const navShare =
      asideRect && asideRect.width > 0 && asideRect.left >= -1
        ? asideRect.width / window.innerWidth
        : 0;
    // A map squeezed to nothing is the defect this audit twice failed to catch, and
    // it is invisible to every metric above: the offending sibling panel hangs only
    // ~15% past the right edge, under the clipping threshold, while the map beside it
    // resolves to zero width and simply is not there. Measure the map directly.
    // `null` means the page has no map and the check does not apply.
    // Measured against `main`, not the viewport: a healthy desktop layout legitimately
    // gives the map only half the screen once the nav rail and a results panel have
    // taken their share, so a viewport-relative threshold would flag the good layout.
    // Against `main` the broken case is unmistakable -- the map is at zero.
    const canvas = document.querySelector(".maplibregl-map");
    const mainEl = document.querySelector("main");
    const mainWidth = mainEl ? mainEl.getBoundingClientRect().width : window.innerWidth;
    const mapShare =
      canvas && mainWidth > 0 ? canvas.getBoundingClientRect().width / mainWidth : null;

    // Images that loaded nothing. An evidence photograph is a scored capability, and
    // a broken one is invisible to every geometry metric here -- the element has a
    // size, it just has no picture in it. It happened on the deployed console for
    // days: crop URLs come back root-relative, so the browser resolved them against
    // the *console* origin, where Netlify's SPA catch-all returns index.html with
    // HTTP 200. Nothing 404s, so nothing looking for failures could find it.
    // `naturalWidth === 0` on a complete image is the one signal that does not care
    // why the bytes were not an image.
    const brokenImages = [...document.querySelectorAll("main img")]
      .filter((img) => img.complete && img.naturalWidth === 0)
      .map((img) => (img.getAttribute("src") ?? "(no src)").slice(0, 80));

    // Videos that failed to load their source. Same defect family as the broken
    // images, and equally invisible to geometry: the element is present and correctly
    // sized, it just has no media behind it. This is how a stream URL resolved against
    // the wrong origin presented -- "clip could not be opened" on every tile, reading
    // as a broken camera rather than a wrong host.
    const brokenVideos = [...document.querySelectorAll("main video")]
      .filter((v) => v.error !== null || (v.currentSrc === "" && v.getAttribute("src")))
      .map((v) => (v.getAttribute("src") ?? "(no src)").slice(0, 80));

    // Text the viewer cannot read. This is the failure mode a second theme
    // introduces and that no geometry check can see: a colour defined only for the
    // dark palette leaves light-mode text sitting on its own background, perfectly
    // laid out and perfectly invisible. WCAG AA wants 4.5:1 for body text; 3:1 is
    // used here as the threshold for "broken" rather than "could be better", so the
    // check fails on genuinely unreadable text and stays quiet about taste.
    const luminance = (rgb) => {
      const [r, g, b] = rgb.map((v) => {
        const c = v / 255;
        return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const parse = (str) => {
      const m = String(str).match(/rgba?\(([^)]+)\)/);
      if (!m) return null;
      const parts = m[1].split(",").map((x) => parseFloat(x));
      if (parts.length >= 4 && parts[3] === 0) return null; // fully transparent
      return parts.slice(0, 3);
    };
    // Walk up for the first ancestor that actually paints a background.
    const backgroundOf = (el) => {
      let node = el;
      while (node && node !== document.documentElement) {
        const bg = parse(getComputedStyle(node).backgroundColor);
        if (bg) return bg;
        node = node.parentElement;
      }
      return parse(getComputedStyle(document.body).backgroundColor) ?? [0, 0, 0];
    };
    const lowContrast = [];
    const textNodes = [...document.querySelectorAll("main *, nav *, header *")].filter(
      (el) => {
        if (el.children.length > 0) return false; // leaf elements carry the text
        const t = (el.textContent ?? "").trim();
        if (t.length < 2) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      },
    );
    for (const el of textNodes.slice(0, 400)) {
      const fg = parse(getComputedStyle(el).color);
      if (!fg) continue;
      const bg = backgroundOf(el);
      const l1 = luminance(fg);
      const l2 = luminance(bg);
      const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      if (ratio < 3) {
        lowContrast.push(
          `${(el.textContent ?? "").trim().slice(0, 24)} @${ratio.toFixed(1)}:1`,
        );
      }
    }

    return {
      lowContrast: [...new Set(lowContrast)].slice(0, 6),
      brokenVideos: [...new Set(brokenVideos)].slice(0, 4),
      scrollWidth: doc.scrollWidth,
      innerWidth: window.innerWidth,
      overflowing: [...new Set(overflowing)].slice(0, 4),
      clipped: [...new Set(clipped)].slice(0, 4),
      navShare,
      mapShare,
      brokenImages: [...new Set(brokenImages)].slice(0, 4),
    };
  });

  // Two pixels of slack: sub-pixel rounding on scaled viewports is not a defect.
  const overflows = metrics.scrollWidth > metrics.innerWidth + 2;
  const clipped = metrics.clipped.length > 0;
  // Navigation eating more than a third of a phone screen is a layout failure even
  // when nothing overflows: it is what pushed the map off the GIS page entirely.
  const navTooWide = vp.width < 768 && metrics.navShare > 0.34;
  // A map given less than a third of the content area has been crushed by a sibling
  // panel. It is a failure at every width, not only on phones -- the GIS, Coverage and
  // Journey pages all put a fixed-width panel beside a flex-1 map, and each collapsed
  // the same way once the panel alone exceeded the screen.
  const mapCrushed = metrics.mapShare !== null && metrics.mapShare < 0.35;
  const tag = `${pageName} @ ${vp.name}px`;

  if (overflows) {
    problems.push(
      `${tag}: body scrolls horizontally ` +
        `(${metrics.scrollWidth}px content in ${metrics.innerWidth}px viewport)`,
    );
  }
  if (clipped) {
    problems.push(`${tag}: content clipped off-screen — ${metrics.clipped.join(", ")}`);
  }
  if (navTooWide) {
    problems.push(
      `${tag}: navigation occupies ${(metrics.navShare * 100).toFixed(0)}% of the viewport`,
    );
  }

  const unreadable = metrics.lowContrast.length > 0;
  if (unreadable) {
    problems.push(
      `${tag}: ${metrics.lowContrast.length} text run(s) below 3:1 contrast — ` +
        metrics.lowContrast.join("; "),
    );
  }

  const imagesBroken = metrics.brokenImages.length > 0;
  const videosBroken = metrics.brokenVideos.length > 0;
  if (videosBroken) {
    problems.push(
      `${tag}: ${metrics.brokenVideos.length} video(s) failed to load — ` +
        metrics.brokenVideos.join(", "),
    );
  }
  if (imagesBroken) {
    problems.push(
      `${tag}: ${metrics.brokenImages.length} image(s) loaded nothing — ` +
        metrics.brokenImages.join(", "),
    );
  }
  if (mapCrushed) {
    problems.push(
      `${tag}: the map occupies ${(metrics.mapShare * 100).toFixed(0)}% of the content area ` +
        `— a side panel has squeezed it out`,
    );
  }

  const ok =
    !overflows && !clipped && !navTooWide && !mapCrushed && !imagesBroken && !videosBroken;
  console.log(
    ok
      ? `  ok   ${tag}`
      : `  FAIL ${tag}` +
          (overflows ? " overflow" : "") +
          (clipped ? ` clipped:${metrics.clipped.length}` : "") +
          (navTooWide ? ` nav:${(metrics.navShare * 100).toFixed(0)}%` : "") +
          (mapCrushed ? ` map:${(metrics.mapShare * 100).toFixed(0)}%` : "") +
          (imagesBroken ? ` brokenimg:${metrics.brokenImages.length}` : "") +
          (videosBroken ? ` brokenvid:${metrics.brokenVideos.length}` : ""),
  );
  return ok;
}

const run = async () => {
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const password = secret("SETU_ADMIN_PASSWORD");

  console.log(`auditing ${BASE}\n`);

  for (const vp of VIEWPORTS) {
    console.log(`viewport ${vp.width}x${vp.height}`);
    const context = await browser.newContext({
      viewport: { width: vp.width, height: vp.height },
      hasTouch: vp.width < 900,
      isMobile: vp.width < 900,
      deviceScaleFactor: 2,
    });
    const page = await context.newPage();

    await signIn(page, password);
    await page.waitForTimeout(2500);
    await measure(page, "login+map", vp);
    await page.screenshot({ path: join(OUT, `map-${vp.name}${SUFFIX}.png`) });

    for (const p of PAGES.slice(1)) {
      const went = await navigateTo(page, p.nav);
      if (!went) {
        if (!p.optional) problems.push(`${p.name} @ ${vp.name}px: nav link not found`);
        continue;
      }
      await measure(page, p.name, vp);
      // Keep evidence for the two narrowest and the tablet width only; four full
      // sets of eight is noise in a submission.
      if (vp.name !== "1024") {
        await page.screenshot({ path: join(OUT, `${p.name}-${vp.name}${SUFFIX}.png`) });
      }
    }

    await context.close();
    console.log("");
  }

  await browser.close();

  console.log("=".repeat(64));
  if (problems.length === 0) {
    console.log("PASS: no page overflows, clips content, or lets navigation dominate");
  } else {
    console.log(`${problems.length} problem(s):\n`);
    for (const p of problems) console.log(`  - ${p}`);
  }
  console.log("=".repeat(64));
  console.log(`screenshots: ${OUT}`);
  process.exit(problems.length ? 1 : 0);
};

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
