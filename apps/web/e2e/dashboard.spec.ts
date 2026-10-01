import { expect, test } from "@playwright/test";

import { bff, createOrg, signUp, uniqueEmail } from "./helpers";

test("unauthenticated users are sent to login and tokens are never readable by JS", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await signUp(page, uniqueEmail("cookie"));
  const jsCookies = await page.evaluate(() => document.cookie);
  expect(jsCookies).not.toContain("sf_access");
  expect(jsCookies).not.toContain("sf_refresh");
  const cookies = await page.context().cookies();
  const access = cookies.find((c) => c.name === "sf_access");
  expect(access?.httpOnly).toBe(true);
  expect(access?.sameSite).toBe("Lax");
});

test("mutating proxy calls without the CSRF header are rejected", async ({ page }) => {
  await signUp(page, uniqueEmail("csrf"));
  const res = await page.request.post("/api/sf/orgs", { data: { name: "No CSRF" } });
  expect(res.status()).toBe(403);
});

test("create, version, deploy and run a workflow from the UI", async ({ page }) => {
  await signUp(page, uniqueEmail("wf"));
  await createOrg(page, "E2E Workflows");
  await page.getByTestId("seed-demo").click();
  await expect(page.getByText(/Seeded 24 customers/)).toBeVisible();

  await page.getByRole("link", { name: "Workflows" }).click();
  await page.locator('input[name="workflow-name"]').fill("customer-greeting");
  await page.getByTestId("create-workflow").click();
  await expect(page.getByRole("heading", { name: "customer-greeting" })).toBeVisible();

  const definition = {
    start: "lookup",
    inputs: { customer: { type: "string" } },
    steps: [
      { id: "lookup", type: "tool", config: { tool: "crm.get_customer", args: { customer_ref: "$.input.customer" } }, next: "greet" },
      { id: "greet", type: "transform", config: { set: { message: "Hello {{ $.steps.lookup.result.name }} ({{ $.steps.lookup.result.tier }})" } } },
    ],
    output: { message: "$.state.message" },
  };
  await page.locator('textarea[name="definition"]').fill(JSON.stringify(definition, null, 2));
  await page.getByTestId("create-version").click();
  await page.getByTestId("deploy-v1").click();
  await expect(page.getByText("Production: v1")).toBeVisible();

  await page.locator('textarea[name="input"]').fill('{"customer": "C-1001"}');
  await page.getByTestId("run").click();
  await expect(page).toHaveURL(/\/executions\/[0-9a-f-]{36}$/);
  await expect(page.getByTestId("execution-status")).toHaveText("succeeded", { timeout: 20_000 });
  await expect(page.getByTestId("step-lookup")).toBeVisible();
  await expect(page.getByText(/"message": "Hello .+ \((standard|gold|platinum)\)"/).first()).toBeVisible();
});

test("an external action waits for approval, is approved in the inbox, and runs once", async ({ page }) => {
  await signUp(page, uniqueEmail("approver"));
  const org = await createOrg(page, "E2E Approvals");
  await bff(page, "POST", `/orgs/${org}/demo-data`);
  await bff(page, "PUT", `/orgs/${org}/tools/email.send_message`, {
    config: { from_address: "support@acme.example" },
    credentials: { api_key: "e2e-test-key" },
  });
  const wf = await bff<{ id: string }>(page, "POST", `/orgs/${org}/workflows`, { name: "notify-customer" });
  await bff(page, "POST", `/orgs/${org}/workflows/${wf.id}/versions`, {
    definition: {
      start: "draft",
      steps: [
        { id: "draft", type: "tool", config: { tool: "email.draft_message", args: { to: "ada@example.com", subject: "Your order", body: "It is late, sorry." } }, next: "send" },
        { id: "send", type: "tool", config: { tool: "email.send_message", args: { message_id: "$.steps.draft.result.message_id" } } },
      ],
      output: { status: "$.steps.send.result.status" },
    },
  });
  await bff(page, "POST", `/orgs/${org}/workflows/${wf.id}/deployments`, { version: 1 });
  const ex = await bff<{ id: string }>(page, "POST", `/orgs/${org}/workflows/${wf.id}/executions`, { input: {} });

  await page.goto(`/o/${org}/executions/${ex.id}`);
  await expect(page.getByTestId("execution-status")).toHaveText("waiting", { timeout: 20_000 });
  await expect(page.getByText(/Waiting for tool_approval/)).toBeVisible();

  await page.getByRole("link", { name: /Approvals/ }).first().click();
  await expect(page.getByText("email.send_message").first()).toBeVisible();
  await page.locator('[data-testid^="approve-"]').first().click();
  await expect(page.getByText("No pending approvals.")).toBeVisible();

  await page.goto(`/o/${org}/executions/${ex.id}`);
  await expect(page.getByTestId("execution-status")).toHaveText("succeeded", { timeout: 20_000 });
  const activity = await bff<{ messages: { status: string }[] }>(page, "GET", `/orgs/${org}/simulated/activity`);
  expect(activity.messages.map((m) => m.status)).toEqual(["sent"]);

  await page.getByRole("link", { name: "Audit log" }).click();
  await expect(page.getByText("approval.approved")).toBeVisible();
  await expect(page.getByText("tool.executed").first()).toBeVisible();
});
