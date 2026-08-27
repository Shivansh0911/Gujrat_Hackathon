/**
 * Capture every console screen against real API data.
 *
 * These images go straight into the submission, so they must show the running
 * platform with real detections, real alerts and real coordinates -- never a mock.
 * The script fails loudly if a screen renders empty, because an empty screenshot in
 * a submission is worse than no screenshot: it looks like the feature does not work.
 */
import { chromium } from "playwright";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");
const OUT = join(ROOT, "docs", "screenshots", process.env.SETU_SHOT_DIR ?? "");
const BASE = process.env.SETU_CONSOLE_URL ?? "http://127.0.0.1:5173";

function env(key) {
  const envFile = process.env.SETU_ENV_FILE ?? ".env";
  const line = readFileSync(join(ROOT, envFile), "utf8")
    .split(/\r?\n/)
    .find((l) => l.startsWith(`${key}=`));
  if (!line) throw new Error(`${key} missing from .env`);
  return line.slice(key.length + 1).trim();
}

const failures = [];

async function shot(page, name, { min = 1 } = {}) {
  await page.waitForTimeout(1200);
  const path = join(OUT, `${name}.png`);
  await page.screenshot({ path, fullPage: false });
  // A screen that rendered nothing is a failed capture, not a successful one.
  const text = (await page.locator("body").innerText()).trim();
  if (text.length < min) failures.push(`${name}: page rendered ${text.length} chars`);
  console.log(`  captured ${name}.png (${text.length} chars of text)`);
  return text;
}

const run = async () => {
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  page.on("console", (m) => m.type() === "error" && console.log("  [browser error]", m.text()));

  console.log(`capturing from ${BASE}`);

  // 1 — login
  await page.goto(BASE, { waitUntil: "networkidle" });
  await shot(page, "01-login", { min: 40 });

  // sign in
  await page.fill('input[autocomplete="username"]', "admin");
  await page.fill('input[autocomplete="current-password"]', env("SETU_ADMIN_PASSWORD"));
  await page.click('button:has-text("Sign in")');
  await page.waitForSelector("text=Camera registry", { timeout: 20000 });

  // 2 — GIS map
  await page.waitForTimeout(3500); // basemap tiles
  await shot(page, "02-gis-map", { min: 200 });

  // 2b — camera detail with provenance
  const firstCamera = page.locator("aside ~ main button.w-full").first();
  if (await firstCamera.count()) {
    await firstCamera.click();
    await page.waitForTimeout(2500);
    await shot(page, "03-camera-detail", { min: 200 });
  }

  // 3 — journey view, the scored screen
  await page.click('a:has-text("Journey")');
  await page.waitForSelector('input[placeholder="GJ01AB1234"]', { timeout: 15000 });

  const plate = process.env.SETU_DEMO_PLATE ?? "KA25AB144";
  await page.fill('input[placeholder="GJ01AB1234"]', plate);
  await page.fill(
    'input[placeholder^="FIR"]',
    "FIR 123/2026 - vehicle trace requested by Investigating Officer",
  );
  await page.click('button:has-text("Trace vehicle")');
  await page.waitForTimeout(4000);
  const journeyText = await shot(page, "04-journey", { min: 200 });
  if (!journeyText.includes(plate)) failures.push(`journey: ${plate} not shown in result`);

  // 4 — alert desk
  await page.click('a:has-text("Alert Desk")');
  await page.waitForSelector("text=Alert desk", { timeout: 15000 });
  await page.waitForTimeout(2500);
  const alertText = await shot(page, "05-alert-desk", { min: 200 });
  if (alertText.includes("No alerts")) failures.push("alert desk: no alerts present");

  // 5 — coverage gap analysis
  await page.click('a:has-text("Coverage")');
  await page.waitForSelector("text=Coverage gap analysis", { timeout: 15000 });
  await page.waitForTimeout(3500);
  const gapText = await shot(page, "08-gap-analysis", { min: 200 });
  if (!gapText.includes("Coverage confidence")) failures.push("gap analysis: no district breakdown");

  // 6 — health
  await page.click('a:has-text("Health")');
  await page.waitForSelector("text=Feed health", { timeout: 15000 });
  await page.waitForTimeout(2000);
  await shot(page, "06-health", { min: 200 });

  // 5b — fault report dialog
  const faultBtn = page.locator('button:has-text("Fault report")').first();
  if (await faultBtn.count()) {
    await faultBtn.click();
    await page.waitForTimeout(900);
    await shot(page, "07-fault-report", { min: 200 });
  }

  // The fault-report dialog is modal and swallows clicks on the nav behind it.
  const closeBtn = page.locator('button:has-text("Close")').first();
  if (await closeBtn.count()) {
    await closeBtn.click();
    await page.waitForTimeout(400);
  }

  // 7 — watchlist, the input to every alert
  await page.locator('a[href="/watchlist"]').click();
  await page.waitForSelector("text=Every alert on the desk begins here", { timeout: 15000 });
  await page.waitForTimeout(1500);
  const watchText = await shot(page, "09-watchlist", { min: 200 });
  if (!/expires/i.test(watchText)) failures.push("watchlist: no expiry column");

  // 7b — the add form, which is where the expiry requirement is visible
  const addBtn = page.locator('button:has-text("Add vehicle")').first();
  if (await addBtn.count()) {
    await addBtn.click();
    await page.waitForTimeout(700);
    await shot(page, "10-watchlist-add", { min: 200 });
  }

  // 8 — system: audit chain verification
  await page.locator('a[href="/system"]').click();
  await page.waitForSelector("text=Audit chain", { timeout: 15000 });
  await page.waitForTimeout(2500);
  const sysText = await shot(page, "11-system-audit", { min: 200 });
  if (!/entries checked/i.test(sysText)) failures.push("system: audit chain not verified");

  await browser.close();

  if (failures.length) {
    console.error("\nCAPTURE PROBLEMS:");
    failures.forEach((f) => console.error(`  - ${f}`));
    process.exit(1);
  }
  console.log(`\nall screens captured to ${OUT}`);
};

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
