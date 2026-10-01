import { expect, type Page } from "@playwright/test";

export const PASSWORD = "e2e-password-long-enough";

export function uniqueEmail(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
}

/** Register + sign in through the real UI (exercises the BFF cookie flow). */
export async function signUp(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByTestId("toggle-mode").click();
  await page.locator('input[name="name"]').fill("E2E User");
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(PASSWORD);
  await page.getByTestId("submit").click();
  await expect(page.getByRole("heading", { name: "Organizations", exact: true })).toBeVisible();
}

export async function createOrg(page: Page, name: string): Promise<string> {
  await page.locator('input[name="org-name"]').fill(name);
  await page.getByTestId("create-org").click();
  await expect(page).toHaveURL(/\/o\/[0-9a-f-]{36}$/);
  return page.url().split("/o/")[1]!;
}

/** Call the API through the BFF proxy with the page's session cookies (+ CSRF header). */
export async function bff<T>(page: Page, method: string, path: string, body?: unknown): Promise<T> {
  const res = await page.request.fetch(`/api/sf${path}`, {
    method,
    headers: method === "GET" ? {} : { "x-sf-csrf": "1", "content-type": "application/json" },
    data: body === undefined ? undefined : JSON.stringify(body),
  });
  expect(res.ok(), `${method} ${path} → ${res.status()} ${await res.text()}`).toBeTruthy();
  return (res.status() === 204 ? undefined : await res.json()) as T;
}
