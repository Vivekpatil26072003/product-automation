import { expect, test } from "@playwright/test";

// U5/U4/control tower on the seeded demo company. 27 Sept 2026 holds the spec's F1 records
// (4,830 m against 6,000 m, 80.5%, 5 records); no E2E test approves anything on that date.

const F1 = "date_from=2026-09-27&date_to=2026-09-27";

test("overview reconciles, drills down and exports", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Reviewer (all departments)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);

  await page.goto(`/dashboard?${F1}`);
  const tiles = page.getByLabel("Totals in m");
  await expect(tiles).toContainText("4,830");
  await expect(tiles).toContainText("6,000");
  await expect(tiles).toContainText("80.5%");
  await expect(tiles).toContainText("-1,170");

  // Every chart has a table view with the same numbers.
  await page.getByRole("button", { name: "Show as table" }).click();
  await expect(page.getByRole("row", { name: /Tapeline/ })).toContainText("83.3%");
  await page.getByRole("button", { name: "Show chart" }).click();

  // Clicking a department opens exactly its records, with the filters carried over in the URL.
  await page.getByRole("button", { name: /^Tapeline: 1,250 m of 1,500 m/ }).click();
  await expect(page).toHaveURL(/\/records\?.*department_id=.*unit=m/);
  await expect(page.getByRole("heading", { name: "Production records" })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: /^1 record$/ })).toBeVisible();
  await expect(page.getByRole("cell", { name: "1,250 m" })).toBeVisible();

  // Back restores the dashboard filters from the URL.
  await page.goBack();
  await expect(page.getByLabel("Totals in m")).toContainText("4,830");

  // Excel export of the same selection.
  await page.goto(`/records?${F1}`);
  await page.getByRole("button", { name: "Export to Excel" }).click();
  const download = page.getByRole("link", { name: "Download (5 records)" });
  await expect(download).toBeVisible({ timeout: 30_000 });
  await expect(download).toHaveAttribute("href", /exports\//);
});

test("control tower shows who submitted", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Reviewer (all departments)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await page.goto("/control-tower?date=2026-09-27");
  const table = page.getByRole("table");
  await expect(table.getByRole("row", { name: /Tapeline/ })).toContainText("Submitted");
  await expect(table.getByRole("row", { name: /Purchase/ })).toContainText("Not expected"); // a Sunday
  await expect(page.getByText("Google Sheets sync")).toBeVisible();
});
