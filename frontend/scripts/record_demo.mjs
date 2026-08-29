/**
 * FIRST DRAFT of a demonstration recording. NOT the submission deliverable.
 * =========================================================================
 *
 * This produces an unnarrated screen recording that follows `docs/DEMO_RUNBOOK.md`
 * §2 beat by beat. A human must watch the output before it goes anywhere near the
 * submission, and it needs voice-over: the runbook's "say this" lines carry the
 * argument, and silent footage of a map is not a demonstration of anything. The
 * checklist item "record both demonstration videos" is NOT satisfied by running this.
 *
 * What it does that a screenshot run does not
 * -------------------------------------------
 * Screenshots prove a screen renders. A recording has to show the platform being
 * *used*, so this deliberately:
 *
 *   * dwells on each screen for the runbook's stated timing rather than advancing as
 *     soon as the DOM settles -- a reviewer needs time to read what is on screen;
 *   * types the plate character by character instead of filling the field, because a
 *     value that appears instantly reads as a canned result;
 *   * scrolls through long results rather than capturing the first viewport;
 *   * performs the real interactions the runbook calls for -- acknowledging an alert,
 *     opening a fault report, verifying the audit chain -- against the live API.
 *
 * Login and navigation deliberately mirror `capture_screenshots.mjs`. That script is
 * the tested path through the console; forking it would mean two things to keep
 * working when a selector changes.
 *
 * Usage
 * -----
 *   # own-feed demonstration (after `make demo`)
 *   node scripts/record_demo.mjs
 *
 *   # government-feed demonstration (after `make gateway-ingest`)
 *   SETU_DEMO_VARIANT=gateway SETU_DEMO_PLATE=GJ32AG1111 node scripts/record_demo.mjs
 *
 * Output lands in docs/demo-recordings/<variant>/ as WebM. Playwright names the file
 * itself; the run prints the final path.
 */
import { chromium } from "playwright";
import { mkdirSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");

const VARIANT = process.env.SETU_DEMO_VARIANT ?? "own-feed";
const OUT = join(ROOT, "docs", "demo-recordings", VARIANT);
const BASE = process.env.SETU_CONSOLE_URL ?? "http://127.0.0.1:5173";
const PLATE = process.env.SETU_DEMO_PLATE ?? "KA25AB1542";

/** Runbook timings, in milliseconds. Named so a change here is a deliberate one. */
const BEAT = {
  read: 3500, // long enough to read a panel
  dwell: 5000, // the runbook's "spend time here" screens
  settle: 1500, // after a click, before the next action
  tiles: 4000, // basemap tiles
};

function env(key) {
  const envFile = process.env.SETU_ENV_FILE ?? ".env";
  const line = readFileSync(join(ROOT, envFile), "utf8")
    .split(/\r?\n/)
    .find((l) => l.startsWith(`${key}=`));
  if (!line) throw new Error(`${key} missing from ${envFile}`);
  return line.slice(key.length + 1).trim();
}

const problems = [];

/** Announce the beat in the console so a reviewer can follow along while it records. */
function beat(n, what) {
  console.log(`  [${String(n).padStart(2, "0")}] ${what}`);
}

/** Type at human speed. An instantly-filled field reads as a canned result. */
async function typeSlowly(page, selector, value) {
  await page.click(selector);
  await page.type(selector, value, { delay: 110 });
}

/** Scroll a long result so the recording shows all of it, not just the fold. */
async function scrollThrough(page, steps = 3, pause = 1300) {
  for (let i = 0; i < steps; i += 1) {
    await page.mouse.wheel(0, 420);
    await page.waitForTimeout(pause);
  }
  await page.mouse.wheel(0, -420 * steps);
  await page.waitForTimeout(600);
}

const run = async () => {
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1600, height: 950 },
    recordVideo: { dir: OUT, size: { width: 1600, height: 950 } },
  });
  const page = await context.newPage();
  page.on("console", (m) => m.type() === "error" && console.log("  [browser error]", m.text()));

  console.log(`recording "${VARIANT}" from ${BASE}`);
  console.log(`plate: ${PLATE}\n`);

  // ---------------------------------------------------------------- 0. login
  beat(0, "login");
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForTimeout(BEAT.read);
  // Filled, not typed. Two reasons, and neither is cosmetic: `page.type` does not
  // reliably drive these controlled inputs (it left the form rejecting a password
  // that `fill` accepts), and a credential typed character by character is a
  // credential rendered legibly into a video that goes to a submission portal.
  await page.fill('input[autocomplete="username"]', "admin");
  await page.fill('input[autocomplete="current-password"]', env("SETU_ADMIN_PASSWORD"));
  await page.waitForTimeout(700);
  await page.click('button:has-text("Sign in")');
  try {
    await page.waitForSelector("text=Camera registry", { timeout: 20000 });
  } catch {
    // Report what the screen says rather than guessing. Two causes are common and
    // they look identical from out here: the credentials do not match the deployment
    // (SETU_ENV_FILE pointing at the wrong file is the usual one), or login's
    // 60-second rate limiter was consumed by something that authenticated just
    // before -- verify_deployment.py, a screenshot run, a manual login.
    const onScreen = await page.locator("body").innerText();
    throw new Error(
      "login did not complete. Check SETU_ENV_FILE points at the same environment " +
        "as SETU_CONSOLE_URL; if the credentials are right, wait a minute for the " +
        "login rate limiter to clear. Screen said: " +
        onScreen.replace(/\s+/g, " ").slice(0, 200),
    );
  }

  // ------------------------------------------------- 1. GIS map — 40 seconds
  beat(1, "GIS map: coordinate provenance and the pin-drop");
  await page.waitForTimeout(BEAT.tiles);
  await page.waitForTimeout(BEAT.dwell);
  await scrollThrough(page, 2);

  const firstCamera = page.locator("aside ~ main button.w-full").first();
  if (await firstCamera.count()) {
    await firstCamera.click();
    await page.waitForTimeout(BEAT.dwell); // declared vs measured fps, provenance
  } else {
    problems.push("map: no camera rows to open");
  }

  // -------------------------------------- 2. Journey — the scored capability
  beat(2, `journey: tracing ${PLATE}`);
  await page.click('a:has-text("Journey")');
  await page.waitForSelector('input[placeholder="GJ01AB1234"]', { timeout: 15000 });
  await page.waitForTimeout(BEAT.settle);

  await typeSlowly(page, 'input[placeholder="GJ01AB1234"]', PLATE);
  await typeSlowly(
    page,
    'input[placeholder^="FIR"]',
    "FIR 123/2026 - vehicle trace requested by Investigating Officer",
  );
  await page.waitForTimeout(900); // let the mandatory-purpose field be seen filled
  await page.click('button:has-text("Trace vehicle")');
  await page.waitForTimeout(BEAT.tiles);

  const journeyText = await page.locator("body").innerText();
  if (!journeyText.includes(PLATE)) {
    problems.push(`journey: ${PLATE} not present in the result`);
  }
  // The runbook spends two minutes here: hops, evidence crops, provenance badges,
  // implied speed, and the dashed no-detection footer.
  await page.waitForTimeout(BEAT.dwell);
  await scrollThrough(page, 4, 1600);
  await page.waitForTimeout(BEAT.dwell);

  // ------------------------------------------------------- 3. Alert desk
  beat(3, "alert desk: a fuzzy match, then acknowledge");
  await page.click('a:has-text("Alert Desk")');
  await page.waitForSelector("text=Alert desk", { timeout: 15000 });
  await page.waitForTimeout(BEAT.dwell);

  const alertText = await page.locator("body").innerText();
  if (alertText.includes("No alerts")) problems.push("alert desk: no alerts present");
  await scrollThrough(page, 2);

  // A real interaction, not a hover: the disposition is what feeds the measured
  // false-positive rate on the Health screen.
  const ackBtn = page.locator('button:has-text("Acknowledge")').first();
  if (await ackBtn.count()) {
    await ackBtn.click();
    await page.waitForTimeout(BEAT.read);
  }

  // ------------------------------------------------------- 4. Coverage
  beat(4, "coverage: district confidence and investigation-derived gaps");
  await page.click('a:has-text("Coverage")');
  await page.waitForSelector("text=Coverage gap analysis", { timeout: 15000 });
  await page.waitForTimeout(BEAT.tiles);
  await page.waitForTimeout(BEAT.read);
  await scrollThrough(page, 3);

  // ------------------------------------------------------- 5. Health
  beat(5, "health: declared vs measured fps, vehicle counts, fault report");
  await page.click('a:has-text("Health")');
  await page.waitForSelector("text=Feed health", { timeout: 15000 });
  await page.waitForTimeout(BEAT.dwell); // vehicle-count card loads here
  await scrollThrough(page, 3);

  const faultBtn = page.locator('button:has-text("Fault report")').first();
  if (await faultBtn.count()) {
    await faultBtn.click();
    await page.waitForTimeout(BEAT.dwell); // the §2.5 payload is the point
    const closeBtn = page.locator('button:has-text("Close")').first();
    if (await closeBtn.count()) {
      await closeBtn.click();
      await page.waitForTimeout(BEAT.settle);
    }
  }

  // ------------------------------------------------------- 6. Watchlist
  beat(6, "watchlist: the input to every alert, and mandatory expiry");
  await page.locator('a[href="/watchlist"]').click();
  await page.waitForSelector("text=Every alert on the desk begins here", { timeout: 15000 });
  await page.waitForTimeout(BEAT.read);

  const addBtn = page.locator('button:has-text("Add vehicle")').first();
  if (await addBtn.count()) {
    await addBtn.click();
    await page.waitForTimeout(BEAT.dwell); // the expiry field defaulted to 30 days
  }

  // ------------------------------------------------------- 7. System
  beat(7, "system: audit chain verification");
  await page.locator('a[href="/system"]').click();
  await page.waitForSelector("text=Audit chain", { timeout: 15000 });
  await page.waitForTimeout(BEAT.dwell);

  const verifyBtn = page.locator('button:has-text("Verify now")').first();
  if (await verifyBtn.count()) {
    await verifyBtn.click();
    await page.waitForTimeout(BEAT.dwell);
  }
  const sysText = await page.locator("body").innerText();
  if (!/entries checked/i.test(sysText)) problems.push("system: audit chain not verified");
  await scrollThrough(page, 2);

  // Playwright finalises the video on context close, not on page close.
  await page.close();
  await context.close();
  await browser.close();

  const written = readdirSync(OUT).filter((f) => f.endsWith(".webm"));
  console.log(`\nrecorded ${written.length} file(s) into ${OUT}`);
  written.forEach((f) => console.log(`  ${join(OUT, f)}`));

  if (problems.length) {
    console.error("\nPROBLEMS DURING RECORDING (the video is still written):");
    problems.forEach((p) => console.error(`  - ${p}`));
    console.error("\nFix these and re-record before showing anyone.");
    process.exit(1);
  }

  console.log("\nFIRST DRAFT ONLY. Watch it, then add narration from");
  console.log("docs/DEMO_RUNBOOK.md section 2 before treating it as submission-ready.");
};

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
