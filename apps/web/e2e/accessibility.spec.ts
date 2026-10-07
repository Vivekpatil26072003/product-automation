import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

// FR26 / TC50: automated WCAG 2.2 A/AA scan of the primary screens per role (no critical or serious
// violations), no sideways scrolling at 360 px and at 200% zoom, and keyboard use of the send confirmation.
// Automated scans do not replace the manual screen-reader pass required before release.

const ROUTES: Record<string, string[]> = {
  "Reviewer (all departments)": ["/dashboard", "/records?date_from=2026-09-27&date_to=2026-09-27", "/uploads/new",
    "/batches", "/reports", "/reports/new", "/history", "/exceptions", "/control-tower", "/notifications", "/orders",
    "/history?kind=orders", "/diary", "/history?kind=owner_reports", "/sheets", "/registers"],
  Sender: ["/automation", "/history?kind=emails"],
  Administrator: ["/settings/integrations", "/settings/automation", "/settings/operations", "/settings/owner-report"],
};

async function signIn(page: Page, who: string) {
  await page.goto("/login");
  await page.getByRole("button", { name: who }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
}

async function settle(page: Page) {
  // Pages poll in the background, so "network idle" never arrives: wait for the heading and loaded content.
  await expect(page.locator("main h1").first()).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0, { timeout: 20_000 });
}

test("primary screens have no serious WCAG 2.2 A/AA violations", async ({ page }) => {
  test.setTimeout(240_000);
  const found: string[] = [];
  await page.goto("/login");
  const login = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  found.push(...login.violations.filter((v) => ["critical", "serious"].includes(v.impact ?? "")).map((v) => `/login ${v.id}`));
  for (const [who, routes] of Object.entries(ROUTES)) {
    await signIn(page, who);
    for (const route of routes) {
      await page.goto(route);
      await settle(page);
      const result = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
        .exclude("nextjs-portal") // development-only build indicator
        .analyze();
      for (const v of result.violations.filter((x) => ["critical", "serious"].includes(x.impact ?? ""))) {
        found.push(`${route} ${v.id}: ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
      }
    }
  }
  expect(found, found.join("\n")).toEqual([]);
});

for (const [label, viewport] of [["360 px", { width: 360, height: 780 }], ["200% zoom", { width: 720, height: 450 }]] as const) {
  test(`no sideways scrolling at ${label}`, async ({ page }) => {
    test.setTimeout(180_000);
    await page.setViewportSize(viewport);
    await signIn(page, "Reviewer (all departments)");
    for (const route of ["/dashboard", "/records", "/reports/new", "/exceptions", "/control-tower", "/history"]) {
      await page.goto(route);
      await settle(page);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `${route} scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(1);
    }
  });
}

test("the send confirmation is keyboard operable and returns focus", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "keyboard flow checked once");
  await signIn(page, "Sender");
  await page.goto("/reports/new?date_from=2026-09-27&date_to=2026-09-27&unit=m");
  await page.getByRole("button", { name: "Create report" }).click();
  await expect(page.getByRole("status").filter({ hasText: /^Ready$/ })).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Compose email" }).click();
  await page.getByLabel("To", { exact: true }).fill("plant.manager@example.com");
  await expect(page.getByText(/Saved · version/)).toBeVisible();
  const trigger = page.getByRole("button", { name: "Preview and send" });
  await trigger.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Confirm send" });
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true); // focus moved into the dialog
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused(); // and back to where the user was
});
