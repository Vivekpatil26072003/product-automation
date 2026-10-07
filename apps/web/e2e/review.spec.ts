import { expect, type Page, test } from "@playwright/test";

// U3/U4 end to end: upload a note with a missing target -> review shows the problem -> the reviewer
// supplies it (autosave) -> checks the evidence -> approves -> opens the record -> corrects it through a
// revision that is approved separately.

async function signInAsReviewer(page: Page) {
  await page.goto("/login");
  await page.getByRole("button", { name: "Reviewer (all departments)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/); // sign-in lands on Overview since M4
  await page.goto("/uploads/new");
}

test("review, approve and correct an entry", async ({ page, isMobile }) => {
  await signInAsReviewer(page);
  await page.getByLabel("Department").selectOption({ label: "Tapeline" });
  const run = Date.now().toString().slice(-6);
  const noteText = [
    "Date 25/09/2026", "Department Tapeline", `Operator Tester ${run}`, `Machine ${isMobile ? "T-02" : "T-03"}`,
    "Production 1250 m", "Status Running", "Stop time 30 min", "Remarks e2e run",
  ].join("\n");
  await page.getByLabel("Add files").setInputFiles([
    { name: `review-${run}.txt`, mimeType: "text/plain", buffer: Buffer.from(noteText) },
  ]);
  await page.getByRole("button", { name: "Process notes" }).click();
  await expect(page.getByText("Ready for review", { exact: true })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("link", { name: /Review entries/ }).click();

  // The missing target blocks approval and is linked from the problem summary.
  await expect(page.getByRole("group", { name: "Problems to fix" })).toContainText("Target quantity");
  await expect(page.getByRole("checkbox", { name: /Select .* for approval/ })).toBeDisabled();

  if (isMobile) await page.getByRole("tab", { name: "Fields" }).click();
  await page.getByLabel(/^Target quantity/).fill("1500");
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await expect(page.locator("#field-target_qty")).not.toHaveAttribute("aria-invalid", "true");

  // Evidence: the production value is highlighted in the source.
  await page.locator("#field-production_qty").locator("xpath=..").getByRole("button", { name: "Show in source" }).click();
  await expect(page.locator("mark.marked")).toHaveText("Production 1250 m");
  if (isMobile) await page.getByRole("tab", { name: "Fields" }).click();

  // Earlier runs may have approved an entry for the same date and machine: decide explicitly.
  const dup = page.getByRole("group", { name: "Possible duplicate" });
  if (await dup.isVisible()) {
    await page.getByLabel("Why is this a separate event?").fill(`Separate e2e run ${run}`);
    await page.getByRole("button", { name: "Keep as a separate event" }).click();
    await expect(page.getByText(/Decision: kept/)).toBeVisible();
  }
  await expect(page.getByRole("group", { name: "Problems to fix" })).toHaveCount(0);

  const select = page.getByRole("checkbox", { name: /Select .* for approval/ });
  await expect(select).toBeEnabled();
  await select.check();
  await page.getByRole("button", { name: "Approve selected (1)" }).click();
  await expect(page.getByText("1 record(s) approved.")).toBeVisible();
  await page.getByRole("link", { name: "Open record", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Production record" })).toBeVisible();
  await expect(page.getByText("Revision 1 · Active")).toBeVisible();
  await expect(page.getByText("1250.000")).toBeVisible();

  await page.getByRole("button", { name: "Correct values" }).click();
  await page.getByLabel("Production", { exact: true }).fill("1300");
  await page.getByLabel(/^Reason/).fill("Recount at shift end");
  await page.getByRole("button", { name: "Save correction" }).click();
  await expect(page.getByText("Correction waiting for approval")).toBeVisible();
  await expect(page.getByText("Production: 1250.000 → 1300.000").first()).toBeVisible();
  await page.getByRole("button", { name: "Approve correction" }).click();
  await expect(page.getByText("Revision 2 · Active")).toBeVisible();
  await expect(page.getByText("1300.000").first()).toBeVisible();
});
