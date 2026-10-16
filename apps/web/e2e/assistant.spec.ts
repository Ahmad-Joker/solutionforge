import { expect, test } from "@playwright/test";

import { bff, createOrg, signUp, uniqueEmail } from "./helpers";

// Browser → BFF → API → database, with the labelled fixture policy as the "model" (no API key
// in E2E). This proves the wiring, the approval flow and persistence, not model quality.
test("assistant: sourced answer, ticket lookup, approved draft, unsupported question", async ({ page }) => {
  test.setTimeout(120_000);
  await signUp(page, uniqueEmail("assistant"));
  const org = await createOrg(page, "E2E Assistant");

  await page.goto(`/o/${org}/assistant`);
  await page.getByTestId("demo-setup").click();
  await expect(page.getByText(/Demo content loaded \(20 added/)).toBeVisible();

  // Wait for the embedded worker to index the 20 documents.
  const kbs = await bff<{ id: string; name: string }[]>(page, "GET", `/orgs/${org}/knowledge-bases`);
  const kb = kbs.find((k) => k.name.startsWith("Harbor & Pine"))!;
  await expect
    .poll(async () => {
      const docs = await bff<{ status: string }[]>(page, "GET", `/orgs/${org}/knowledge-bases/${kb.id}/documents`);
      return docs.filter((d) => d.status === "ready").length;
    }, { timeout: 60_000 })
    .toBe(20);

  const ask = async (q: string) => {
    await page.getByTestId("assistant-question").fill(q);
    await page.getByTestId("assistant-ask").click();
    await expect(page.getByTestId("assistant-ask")).toHaveText("Ask", { timeout: 30_000 });
  };

  // 1. Sourced answer. (Which refund passage the rule-based fixture quotes is not under
  // test here; answer quality is measured by the evals. The wiring is: a verified source.)
  await ask("How many days do I have to ask for a refund?");
  await expect(page.getByTestId("assistant-answer").last()).toContainText(/\[HP-(POL-02|SOP-04)#\d\]/);
  await expect(page.getByTestId("assistant-sources").last()).toContainText(/HP-(POL-02|SOP-04)#\d/);
  await page.getByTestId("tool-activity").last().click();
  await expect(page.getByTestId("tool-activity").last()).toContainText("search_documents");

  // 2. Ticket lookup.
  await ask("What's the status of ticket T-1A2B3C4D?");
  await expect(page.getByTestId("assistant-answer").last()).toContainText("open");

  // 3. Draft → human approval → ticket exists in the simulated ticketing system.
  await ask("Please open a ticket: my kitchen tap has been dripping since Monday.");
  const draft = page.getByTestId("ticket-draft").last();
  await expect(draft).toContainText("pending");
  await draft.getByTestId("approve-draft").click();
  await expect(draft.getByTestId("ticket-ref")).toContainText(/T-[0-9A-F]{8}/);
  const ref = (await draft.getByTestId("ticket-ref").innerText()).replace("opened ", "").trim();
  const activity = await bff<{ tickets: { ref: string }[] }>(page, "GET", `/orgs/${org}/simulated/activity`);
  expect(activity.tickets.map((t) => t.ref)).toContain(ref);

  // 4. Unsupported question: abstains, no sources.
  await ask("Do you clean swimming pools?");
  await expect(page.getByTestId("assistant-answer").last()).toContainText("couldn't find");
  await expect(page.getByText("No verified source supports this answer.").last()).toBeVisible();
});
