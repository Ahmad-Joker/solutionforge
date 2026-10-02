// Capture README screenshots from a running dashboard populated by api/scripts/demo_setup.py.
//   node scripts/screenshots.mjs <web-base-url> <demo.json> <out-dir>
import { readFileSync, mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const [base, demoPath, outDir] = process.argv.slice(2);
const demo = JSON.parse(readFileSync(demoPath, "utf-8"));
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1360, height: 860 }, deviceScaleFactor: 1 });

await page.goto(`${base}/login`);
await page.locator('input[name="email"]').fill(demo.email);
await page.locator('input[name="password"]').fill(demo.password);
await page.getByTestId("submit").click();
await page.getByRole("heading", { name: "Organizations", exact: true }).waitFor();

const o = `${base}/o/${demo.org}`;
const shots = [
  ["overview", `${o}`],
  ["workflow", `${o}/workflows/${demo.cases.support.workflow_id}`],
  ["execution", `${o}/executions/${demo.executions[0]}`],
  ["approvals", `${o}/approvals`],
  ["evaluation", `${o}/evaluation`],
  ["knowledge", `${o}/knowledge`],
  ["audit", `${o}/audit`],
];
for (const [name, url] of shots) {
  await page.goto(url);
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${outDir}/${name}.png`, fullPage: false });
  console.log("captured", name);
}
await browser.close();
