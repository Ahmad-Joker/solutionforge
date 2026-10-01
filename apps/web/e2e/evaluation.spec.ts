import { expect, test } from "@playwright/test";

import { bff, createOrg, signUp, uniqueEmail } from "./helpers";

const tierSchema = { type: "object", required: ["tier"], properties: { tier: { enum: ["standard", "gold", "platinum"] } } };
const v1 = {
  start: "lookup",
  inputs: { customer: { type: "string" } },
  steps: [{ id: "lookup", type: "tool", config: { tool: "crm.get_customer", args: { customer_ref: "$.input.customer" } } }],
  output: { tier: "$.steps.lookup.result.tier" },
};
const v2 = {
  start: "guess",
  inputs: { customer: { type: "string" } },
  steps: [{ id: "guess", type: "transform", config: { set: { tier: "unknown" } } }],
  output: { tier: "$.state.tier" },
};

test("evaluate two versions, compare them, and the gate blocks the regression", async ({ page }) => {
  await signUp(page, uniqueEmail("eval"));
  const org = await createOrg(page, "E2E Evaluation");
  await bff(page, "POST", `/orgs/${org}/demo-data`);
  const wf = await bff<{ id: string }>(page, "POST", `/orgs/${org}/workflows`, { name: "tier-lookup" });
  await bff(page, "POST", `/orgs/${org}/workflows/${wf.id}/versions`, { definition: v1 });
  await bff(page, "POST", `/orgs/${org}/workflows/${wf.id}/versions`, { definition: v2 });
  await bff(page, "POST", `/orgs/${org}/workflows/${wf.id}/deployments`, { version: 1 });

  await page.goto(`/o/${org}/evaluation`);
  await page.locator('input[name="dataset-name"]').fill("regression");
  await page.getByTestId("create-dataset").click();
  await expect(page.getByText("Dataset: regression")).toBeVisible();

  for (const [name, customer] of [["first", "C-1001"], ["second", "C-1002"]] as const) {
    await page.locator('textarea[name="case"]').fill(
      JSON.stringify({ name, input: { customer }, expectations: { output_schema: tierSchema, expected_tools: ["crm.get_customer"] } }),
    );
    await page.getByTestId("add-case").click();
    await expect(page.getByRole("cell", { name, exact: true })).toBeVisible();
  }

  for (const v of ["1", "2"]) {
    await page.locator('input[name="eval-version"]').fill(v);
    await page.getByTestId("run-eval").click();
    await expect(page.getByTestId(`v${v}-pass_rate`)).toHaveText(v === "1" ? "100%" : "0%", { timeout: 30_000 });
  }

  await page.getByLabel("compare v1").check();
  await page.getByLabel("compare v2").check();
  await expect(page.getByText("Per-case comparison")).toBeVisible();

  await page.getByTestId("save-gate").click();
  await expect(page.getByRole("button", { name: "Remove gate" })).toBeVisible();
  const res = await page.request.post(`/api/sf/orgs/${org}/workflows/${wf.id}/deployments`, {
    headers: { "x-sf-csrf": "1" },
    data: { version: 2 },
  });
  expect(res.status()).toBe(409);
  expect((await res.json()).error.code).toBe("deployment_blocked");
  await expect(page.getByTestId("decision-v2")).toContainText("no_pass_rate_regression", { timeout: 10_000 });
});
