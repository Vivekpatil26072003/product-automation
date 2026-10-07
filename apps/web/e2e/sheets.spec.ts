import { expect, type Page, test } from "@playwright/test";

// Daily production sheet in the browser: a notebook page (typed here; photos go through OCR / AI the same way)
// is read into the day's sheet, the unclear values are checked, the sheet is approved, listed, downloaded as
// Excel / PDF / CSV, and the Send Email dialog validates the address. No real email is sent by this test.

async function signIn(page: Page, who: string) {
  await page.goto("/login");
  await page.getByRole("button", { name: who }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
}

test("a notebook page becomes the day's sheet, is checked, approved, downloaded and ready to email", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "one run is enough: the sheet table is wide by nature");
  const run = Date.now();
  // A day within the last two years nobody else uses, so repeated runs never merge into the same sheet.
  const when = new Date(Date.now() - (30 + (run % 700)) * 86_400_000);
  const day = when.getUTCDate(), month = when.getUTCMonth() + 1, year = when.getUTCFullYear();
  const iso = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const note = [
    `Date : ${day}/${month}/${year}`, `Shift I supervisor : Tester ${run % 1000}`, "SULZER PROD",
    "No. of running looms 67.91 68.46 68.44", "Picks 7026 7087 7071", "Production in Meters 52104 52949 52919",
    "Production in Kg 8919 8981 8955", "Avg width 3.33 3.33 3.33", "Downtime", "a) Mechanical 169.54 123.30 110.97",
    "c) Weft cut 36.99 33.91",
  ].join("\n");

  await signIn(page, "Reviewer (all departments)");
  await page.goto("/uploads/new");
  await page.getByLabel("Department").selectOption({ label: "Tapeline" });
  await page.getByLabel("Add files").setInputFiles([{ name: `sheet-${run}.txt`, mimeType: "text/plain", buffer: Buffer.from(note) }]);
  await page.getByRole("button", { name: "Process notes" }).click();
  await expect(page.getByRole("link", { name: "Check sheet" })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("link", { name: "Check sheet" }).click();

  // The values are in the sheet; calculated rows are filled; the two-value line is highlighted.
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Daily sheet");
  await expect(page.locator("#s-date")).toHaveValue(iso);
  await expect(page.getByLabel("Sulzer production: Picks, shift II", { exact: true })).toHaveValue("7087");
  const eff = page.getByRole("region", { name: "Sulzer production" }).getByRole("row", { name: /Total efficiency/ });
  await expect(eff).toContainText("64.56");
  await expect(page.getByText("2 values were hard to read")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve and save sheet" })).toBeDisabled();

  // Check them: confirm two, type the missing shift, save, approve.
  await page.getByRole("tab", { name: "Downtime" }).click();
  for (let i = 0; i < 2; i++) await page.getByRole("button", { name: "OK" }).first().click();
  await page.getByLabel("Downtime (hours): c) Weft cut, shift III", { exact: true }).fill("43.16");
  await page.getByRole("button", { name: /Save changes \(\d+\)/ }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  await expect(page.getByRole("row", { name: /c\) Weft cut/ })).toContainText("114.06");
  await page.getByRole("button", { name: "Approve and save sheet" }).click();
  await expect(page.getByText("Sheet approved and saved.")).toBeVisible();

  // Downloads of the saved sheet.
  for (const [fmt, type] of [["xlsx", "spreadsheetml"], ["pdf", "application/pdf"], ["csv", "text/csv"]] as const) {
    const href = await page.getByRole("link", { name: new RegExp(`Download ${fmt === "xlsx" ? "Excel" : fmt.toUpperCase()}`) }).getAttribute("href");
    const res = await page.request.get(href!);
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toContain(type);
  }

  // The list ("SQL sheet") shows the day with its figures and actions.
  await page.getByRole("link", { name: "All daily sheets" }).click();
  await page.getByLabel("From", { exact: true }).fill(iso);
  await page.getByLabel("To", { exact: true }).fill(iso);
  await page.getByRole("button", { name: "Show" }).click();
  await expect(page.getByRole("status").filter({ hasText: "1 sheet" })).toBeVisible();
  const row = page.getByRole("region", { name: "Daily sheets table" }).getByRole("row").nth(1);
  await expect(row).toContainText("Approved");
  await expect(row).toContainText("1,57,972"); // production in metres, Indian grouping
  await expect(row.getByRole("link", { name: "Excel" })).toBeVisible();

  // Email: an invalid address is refused before anything is requested.
  const sends: string[] = [];
  page.on("request", (r) => { if (/\/sheets\/.+\/emails$/.test(r.url())) sends.push(r.url()); });
  await row.getByRole("button", { name: "Send Email" }).click();
  const dialog = page.getByRole("dialog", { name: "Send daily sheet" });
  await dialog.getByLabel("Email address").fill("not-an-email");
  await dialog.getByRole("button", { name: "Send Email" }).click();
  await expect(dialog.getByText("Enter one valid email address")).toBeVisible();
  expect(sends).toEqual([]);
});
