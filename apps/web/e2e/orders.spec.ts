import { expect, type Page, test } from "@playwright/test";

// Customer orders in the browser: a diary note is read into the review table and form, corrected, approved,
// listed in Customer orders, shown as a PDF, and the Send Email dialog validates and queues the email.
// Emails are sent by the worker through EmailJS; these tests never cause a real email: when the development
// database has EmailJS configured, the send step is skipped (server sending is covered by
// tests/integration/test_orders.py and test_owner_reports.py against a recorded EmailJS transport).

async function signIn(page: Page, who: string) {
  await page.goto("/login");
  await page.getByRole("button", { name: who }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
}

async function upload(page: Page, file: { name: string; mimeType: string; buffer: Buffer }) {
  await page.goto("/uploads/new");
  await page.getByLabel("Department").selectOption({ label: "Tapeline" });
  await page.getByLabel("Add files").setInputFiles([file]);
  await page.getByRole("button", { name: "Process notes" }).click();
  await expect(page.getByRole("link", { name: /Review entries/ })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("link", { name: /Review entries/ }).click();
}

async function emailConfigured(page: Page): Promise<boolean> {
  const r = await page.request.get("/api/v1/settings/owner-report");
  return r.ok() && (await r.json()).data.email_ready;
}

test("a diary note fills the review table; the corrected order is saved, listed and its PDF ready to email", async ({ page, isMobile }) => {
  const run = Date.now().toString().slice(-7);
  const note = [
    "Customer : E2E Traders", `Mobile : 98${run}1`, "Order Date : 26/09/2026", "Delivery Date : 25/10/2026",
    "Package : Corrugated Box", "Quantity : 500", "Rate : 25", "Total : 12500", "Employee : Tester",
  ].join("\n");
  await signIn(page, "Administrator");
  const configured = await emailConfigured(page);
  await signIn(page, "Reviewer (all departments)");
  await upload(page, { name: `order-${run}.txt`, mimeType: "text/plain", buffer: Buffer.from(note) });

  const batchUrl = page.url().replace(/\/review.*$/, "");
  // Right after reading: the diary data table with a PDF download, before any review.
  await page.goto(batchUrl);
  const preview = page.getByRole("region", { name: /Diary data table/ });
  await expect(preview).toContainText("E2E Traders");
  await expect(preview).toContainText("To review");
  const draftPdf = await page.request.get((await page.getByRole("link", { name: "Download PDF (draft)" }).getAttribute("href"))!);
  expect((await draftPdf.body()).subarray(0, 5).toString()).toBe("%PDF-");
  await page.goto(`${batchUrl}/review`);

  // TEST 1: the read values are in the review table and the form.
  await page.getByRole("link", { name: "Review orders" }).click();
  await expect(page.getByRole("heading", { name: "Review customer orders" })).toBeVisible();
  const table = page.getByRole("region", { name: /Orders read from this batch/ });
  await expect(table.getByRole("textbox", { name: /^Customer name/ })).toHaveValue("E2E Traders");
  if (isMobile) await expect(page.getByRole("tab", { name: "Order form" })).toBeVisible();
  await expect(page.locator("#of-quantity")).toHaveValue("500");
  await expect(page.locator("#of-order_date")).toHaveValue("2026-09-26");
  await expect(page.getByText('Read from the page: "Customer : E2E Traders"')).toBeVisible();
  await expect(page.locator("#of-customer_email")).toHaveValue(""); // not on the note: left for the reviewer
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1); // the wide table scrolls inside its card, never the page

  // TEST 2: correct, approve, and find the corrected values in Customer orders.
  await page.locator("#of-customer_name").fill(`E2E Traders ${run}`);
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Approve and save order" }).click();
  await expect(page.getByText(/Order ORD-[0-9A-F]{8} saved/)).toBeVisible();
  await page.goto(batchUrl); // the same table now shows the saved values
  await expect(page.getByRole("region", { name: /Diary data table/ })).toContainText(`E2E Traders ${run}`);
  await expect(page.getByRole("link", { name: "Download PDF", exact: true })).toBeVisible();
  await page.goto("/orders");
  const row = page.getByRole("row", { name: new RegExp(`E2E Traders ${run}`) });
  await expect(row).toContainText("12,500.00");

  // TEST 3: the PDF of this order is served for it.
  const pdfHref = await row.getByRole("link", { name: "View PDF" }).getAttribute("href");
  const pdf = await page.request.get(pdfHref!);
  expect(pdf.headers()["content-type"]).toBe("application/pdf");
  expect((await pdf.body()).subarray(0, 5).toString()).toBe("%PDF-");

  // TEST 6: an invalid address is refused before anything is requested.
  const sends: string[] = [];
  page.on("request", (r) => { if (/\/orders\/.+\/emails$/.test(r.url())) sends.push(r.url()); });
  await row.getByRole("button", { name: "Send Email" }).click();
  const dialog = page.getByRole("dialog", { name: "Send Report" });
  await expect(dialog).toContainText(`Customer: E2E Traders ${run}`);
  await expect(dialog).toContainText(/Attachment: ✓ Order_ORD-[0-9A-F]{8}_r1\.pdf/);
  await dialog.getByLabel("Email Address").fill("not-an-email");
  await dialog.getByRole("button", { name: "Send Email" }).click();
  await expect(dialog.getByText("Enter one valid email address, for example name@company.com.")).toBeVisible();
  expect(sends).toEqual([]);
  if (configured) return; // never send a real email from a test

  // Not set up: the server refuses and says why; nothing is queued, the order is unchanged.
  await dialog.getByLabel("Email Address").fill("buyer@example.com");
  await dialog.getByRole("button", { name: "Send Email" }).click();
  await expect(dialog.getByText(/Email is not set up yet/)).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(row).toContainText("None"); // no email recorded
});

test("a photo nobody can read gets an empty order form beside it", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "checked once");
  await signIn(page, "Reviewer (all departments)");
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
  await upload(page, { name: `diary-${Date.now()}.png`, mimeType: "image/png", buffer: png });
  const unread = page.getByRole("heading", { name: "Files without entries" }).locator("..");
  await expect(unread).toContainText(/no OCR reader is set up|could not be read/);
  await unread.getByRole("button", { name: "Enter customer order" }).last().click();
  await expect(page.getByRole("heading", { name: "Review customer orders" })).toBeVisible();
  await expect(page.getByText("Entered by hand: nothing could be read automatically.")).toBeVisible();
  await expect(page.locator("#of-customer_name")).toHaveValue("");
  await expect(page.getByRole("button", { name: "Approve and save order" })).toBeDisabled();
  await expect(page.locator("img.source-image")).toBeVisible(); // the photo is shown beside the form
});

test("the worker screen uploads a diary photo and shows its steps", async ({ page }) => {
  await signIn(page, "Reviewer (all departments)");
  await page.goto("/diary");
  await expect(page.getByRole("button", { name: /Take photo/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Upload diary image/ })).toBeVisible();
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
  await page.getByLabel("Upload diary image", { exact: true }).setInputFiles([{ name: `page-${Date.now()}.png`, mimeType: "image/png", buffer: png }]);
  await expect(page.getByText(/Uploaded\. The pages are being read now/)).toBeVisible({ timeout: 30_000 });
  const latest = page.locator(".diary-list > li").first();
  await expect(latest.getByRole("list", { name: "Processing steps" })).toContainText("Uploaded");
  await expect(latest.getByRole("link", { name: /Enter what could not be read|Review/ })).toBeVisible({ timeout: 60_000 });
});
