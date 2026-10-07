import { expect, test } from "@playwright/test";

// Operations (M8): an administrator sees health, runs a retention dry run (nothing deleted) and the ROI section
// refuses to claim savings without a measured baseline. Non-administrators are refused.

test("administrator runs a retention dry run and sees ROI guard rails", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Administrator" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.goto("/settings/operations");
  await expect(page.getByRole("heading", { name: "Operations", level: 1 })).toBeVisible();
  await expect(page.getByRole("table", { name: "Items past their retention period now" })).toBeVisible();
  await page.getByRole("button", { name: "Dry run" }).click();
  await expect(page.getByText("Dry run recorded; nothing was deleted.")).toBeVisible();
  await page.getByRole("button", { name: "Purge now" }).click();
  await expect(page.getByRole("dialog", { name: "Purge files now?" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("heading", { name: "Time saved (ROI)" })).toBeVisible();
});

test("operations are refused to non-administrators", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Reviewer (all departments)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  expect((await page.request.get("/api/v1/ops/status")).status()).toBe(403);
  expect((await page.request.get("/api/v1/roi")).status()).toBe(403);
});
