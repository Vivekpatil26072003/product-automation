import { expect, test } from "@playwright/test";

// U9 Automation on the demo company: an administrator turns scheduled reports on, a Sender creates a schedule,
// sees the next three runs, and runs it now for 27 Sept 2026 (F1). Runs are draft-only unless approved, and no
// email provider is connected locally, so nothing is sent.

async function signIn(page: import("@playwright/test").Page, who: string) {
  await page.goto("/login");
  await page.getByRole("button", { name: who }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
}

test("sender schedules a report and runs it now as a draft", async ({ page }, info) => {
  await signIn(page, "Administrator");
  await page.goto("/settings/automation");
  const flag = page.getByLabel("Scheduled reports");
  if (!(await flag.isChecked())) {
    await flag.check();
    await page.getByRole("button", { name: "Save settings" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
  }

  await signIn(page, "Sender");
  await page.goto("/automation");
  await page.getByRole("button", { name: "New schedule" }).click();
  await page.getByLabel("Name").fill(`E2E daily ${info.project.name} ${Date.now()}`);
  await page.getByLabel("Unit").selectOption("m");
  await page.getByLabel("To", { exact: true }).fill("plant.manager@example.com");
  await expect(page.getByRole("list", { name: "Next three runs" }).getByRole("listitem")).toHaveCount(3);
  await page.getByRole("button", { name: "Create schedule" }).click();
  await expect(page).toHaveURL(/\/automation\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("status").filter({ hasText: /^Active$/ })).toBeVisible();

  await page.getByLabel("From").fill("2026-09-27");
  await page.getByLabel("To", { exact: true }).fill("2026-09-27");
  await page.getByRole("button", { name: "Run now" }).click();
  const runs = page.getByRole("table", { name: "Run history, newest first" });
  await expect(runs.getByRole("row").filter({ hasText: "2026-09-27" })).toContainText("Draft ready", { timeout: 60_000 });
  await expect(runs.getByRole("link", { name: "Draft" })).toBeVisible();
});

test("exceptions and notifications are reachable by role", async ({ page }) => {
  await signIn(page, "Reviewer (all departments)");
  await page.goto("/exceptions");
  await expect(page.getByRole("heading", { name: "Exceptions", level: 1 })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Open" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("link", { name: /^Notifications/ }).click();
  await expect(page.getByRole("heading", { name: "Notifications", level: 1 })).toBeVisible();

  await signIn(page, "Viewer (Tapeline, Warping)");
  await page.goto("/automation");
  await expect(page.getByText("Scheduled reports are managed by Senders.")).toBeVisible();
  expect((await page.request.get("/api/v1/schedules")).status()).toBe(403);
});
