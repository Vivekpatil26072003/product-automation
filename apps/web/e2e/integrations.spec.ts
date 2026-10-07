import { expect, test } from "@playwright/test";

// Integrations settings (FR22). Runs without provider credentials: it checks the page, role gating and that
// invalid credentials are refused with field messages that never echo the submitted secret. Nothing is saved,
// so no request ever reaches Google or Microsoft.

test("administrator sees every destination; invalid credentials are refused without echo", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Administrator" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);

  await page.goto("/settings/integrations");
  await expect(page.getByRole("heading", { name: "Integrations", level: 1 })).toBeVisible();
  for (const name of ["Google Sheets", "Power BI", "Microsoft 365 email", "ERP"]) {
    await expect(page.getByRole("heading", { name, level: 2 })).toBeVisible();
  }

  await page.getByRole("button", { name: "Connect Google Sheets" }).click();
  await page.getByLabel("Spreadsheet ID").fill("short");
  const secret = "not-a-key-but-a-secret-value-123";
  await page.getByLabel("Service account key (JSON)").fill(secret);
  await page.getByRole("button", { name: "Save and test" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /Review \d+ fields?/ })).toBeVisible();
  await expect(page.getByLabel("Spreadsheet ID")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("paste the service account key file (JSON)")).toBeVisible();
  const messages = await page.locator('[role="alert"], .reason').allInnerTexts();
  expect(messages.join(" ")).not.toContain(secret);
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Connect Google Sheets" })).toBeVisible();
});

test("integrations are hidden from and refused to non-administrators", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Reviewer (all departments)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("link", { name: "Integrations" })).toHaveCount(0);
  await page.goto("/settings/integrations");
  await expect(page.getByText("Only administrators can manage integrations.")).toBeVisible();
  const res = await page.request.get("/api/v1/integrations");
  expect(res.status()).toBe(403);
});
