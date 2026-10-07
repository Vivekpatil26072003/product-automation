import { expect, type Page, test } from "@playwright/test";

// Pick reading register (WGS-02) in the browser: a register page (typed here; photos go through OCR / AI the same
// way) is read into the day's register, the cell that does not add up is highlighted, checked, the register approved,
// listed, downloaded as Excel / PDF / CSV / SQL, and the Send Email dialog validates the address. No real email.

async function signIn(page: Page, who: string) {
  await page.goto("/login");
  await page.getByRole("button", { name: who }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
}

test("a register page becomes the day's register, is checked, approved, downloaded and ready to email", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "one run is enough: the register grid is wide by nature");
  const run = Date.now();
  // A day within the last two years nobody else uses, so repeated runs never merge into the same register.
  const when = new Date(Date.now() - (40 + (run % 690)) * 86_400_000);
  const day = when.getUTCDate(), month = when.getUTCMonth() + 1, year = when.getUTCFullYear();
  const iso = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const note = [
    "HOURLY PRODUCTION READING REGISTER (WGS-02)", `DATE : ${day}/${month}/${year}   PICK - READING`,
    "M/c No./Time | 16-00 | 18-00 | 20-00 | 22-00 | 24-00 | Total",
    "26 | B.F | | | | |",
    "28 | 2169 | 2191 22 | 2210 19 | 2234 24 | 2259 25 |",
    "29 | 1206 | 1230 24 | 1256 26 | 1281 25 | 1306 25 |",
    "48 | 853 | 881 28 | 916 25 | 946 30 | 976 30 |",
    "Total | | 74 | 70 | 79 | 80 |",
  ].join("\n");

  await signIn(page, "Reviewer (all departments)");
  await page.goto("/uploads/new");
  await page.getByLabel("Department").selectOption({ label: "Tapeline" });
  await page.getByLabel("Add files").setInputFiles([{ name: `register-${run}.txt`, mimeType: "text/plain", buffer: Buffer.from(note) }]);
  await page.getByRole("button", { name: "Process notes" }).click();
  await expect(page.getByRole("link", { name: "Check register" })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("link", { name: "Check register" }).click();

  // Read into shift II of the written day; totals calculated; 916 - 881 = 35 but 25 written is highlighted.
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Pick register");
  await expect(page.locator("#r-date")).toHaveValue(iso);
  await expect(page.getByLabel("Shift II, machine 28, 18-00: reading and picks", { exact: true })).toHaveValue("2191 22");
  await expect(page.getByLabel("Shift II, machine 26, 16-00 start reading", { exact: true })).toHaveValue("B.FALL");
  const table = page.getByRole("region", { name: "Shift II · 16:00-24:00" });
  await expect(table.getByRole("row", { name: /Total \(calculated\)/ })).toContainText("70");
  await expect(page.getByText("1 cell does not add up")).toBeVisible();
  await expect(table.getByText(/916 - 881 \(18-00\) = 35, but 25 picks are written/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve and save register" })).toBeDisabled();

  // The page is right as written: OK, save, approve.
  await table.getByRole("button", { name: "OK" }).click();
  await page.getByRole("button", { name: /Save changes \(\d+\)/ }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  await page.getByRole("button", { name: "Approve and save register" }).click();
  await expect(page.getByText("Register approved and saved.")).toBeVisible();

  // Downloads of the saved register.
  for (const [label, type] of [["Excel", "spreadsheetml"], ["PDF", "application/pdf"], ["CSV", "text/csv"], ["SQL", "application/sql"]] as const) {
    const href = await page.getByRole("link", { name: `Download ${label}` }).getAttribute("href");
    const res = await page.request.get(href!);
    expect(res.status()).toBe(200);
    expect(res.headers()["content-type"]).toContain(type);
    if (label === "SQL") expect(await res.text()).toContain("INSERT INTO pick_reading");
  }

  // The list shows the day with its picks and actions.
  await page.getByRole("link", { name: "All pick registers" }).click();
  await page.getByLabel("From", { exact: true }).fill(iso);
  await page.getByLabel("To", { exact: true }).fill(iso);
  await page.getByRole("button", { name: "Show" }).click();
  await expect(page.getByRole("status").filter({ hasText: "1 register" })).toBeVisible();
  const row = page.getByRole("region", { name: "Pick registers table" }).getByRole("row").nth(1);
  await expect(row).toContainText("Approved");
  await expect(row).toContainText("303"); // 74 + 70 + 79 + 80
  await expect(row.getByRole("link", { name: "SQL" })).toBeVisible();

  // Email: an invalid address is refused before anything is requested.
  const sends: string[] = [];
  page.on("request", (r) => { if (/\/registers\/.+\/emails$/.test(r.url())) sends.push(r.url()); });
  await row.getByRole("button", { name: "Send Email" }).click();
  const dialog = page.getByRole("dialog", { name: "Send pick register" });
  await expect(dialog.getByRole("radio", { name: "SQL" })).toBeVisible();
  await dialog.getByLabel("Email address").fill("not-an-email");
  await dialog.getByRole("button", { name: "Send Email" }).click();
  await expect(dialog.getByText("Enter one valid email address")).toBeVisible();
  expect(sends).toEqual([]);
});
