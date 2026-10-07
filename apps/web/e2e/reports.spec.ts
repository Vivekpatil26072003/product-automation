import { createHash } from "node:crypto";

import { expect, test } from "@playwright/test";

// U6/U7 on the seeded demo company (F1 on 27 Sept 2026: 4,830 m of 6,000 m, 80.5%, 5 records).
// The email channel follows the API's EMAIL_PROVIDER. With "graph" and no Microsoft mailbox connected, the send
// is shown as blocked with its reason; with "emailjs" it is ready to confirm (never confirmed here, so no real
// email leaves). Send paths: tests/integration/test_reports_email.py (imitation Graph) and test_emailjs.py.

const F1 = "date_from=2026-09-27&date_to=2026-09-27&unit=m";

async function createF1Report(page: import("@playwright/test").Page) {
  await page.goto(`/reports/new?${F1}`);
  await page.getByRole("button", { name: "Create report" }).click();
  await expect(page).toHaveURL(/\/reports\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("status").filter({ hasText: /^Ready$/ })).toBeVisible({ timeout: 45_000 });
  return page.url().split("/").pop() as string;
}

test("reviewer creates the F1 report; figures, summary and PDF bytes agree", async ({ page }, info) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Reviewer (all departments)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  if (info.project.name === "mobile") await page.getByRole("button", { name: "Menu" }).click(); // phones: nav in header
  await page.getByRole("link", { name: "Reports" }).first().click();
  await expect(page.getByRole("heading", { name: "Reports", level: 1 })).toBeVisible();

  const id = await createF1Report(page);
  const figures = page.getByRole("table", { name: "Production against target per unit" });
  await expect(figures).toContainText("4,830");
  await expect(figures).toContainText("6,000");
  await expect(figures).toContainText("80.5%");
  await expect(page.getByText(/4,830 m against a target of 6,000 m, achieving 80\.5%/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Download PDF" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Compose email" })).toHaveCount(0); // reviewers do not send

  const file = await (await page.request.get(`/api/v1/reports/${id}/file`)).json();
  const preview = Buffer.from(await (await page.request.get(file.data.url)).body());
  const download = Buffer.from(await (await page.request.get(file.data.download_url)).body());
  const sha = (b: Buffer) => createHash("sha256").update(b).digest("hex");
  expect(sha(preview)).toBe(file.data.sha256);
  expect(sha(download)).toBe(file.data.sha256);
  expect(file.data.name).toMatch(/^Production_2026-09-27_P[0-9A-F]{6}_v1\.pdf$/);
});

test("sender drafts an email; invalid input is refused and sending explains what is missing", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Sender" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await createF1Report(page);

  await page.getByRole("button", { name: "Compose email" }).click();
  await expect(page.getByRole("heading", { name: "Email report" })).toBeVisible();
  await expect(page.getByLabel("Message (plain text)")).toHaveValue(/4,830 m against a target of 6,000 m/);
  const emailJs = page.getByText("Sent with EmailJS: the report figures are in the message; the PDF is not attached.");
  const attachment = page.getByText(/Attachment: Production_2026-09-27_P[0-9A-F]{6}_v1\.pdf/);
  await expect(emailJs.or(attachment)).toBeVisible();
  const viaEmailJs = await emailJs.isVisible();

  await page.getByLabel("To", { exact: true }).fill("not-an-address");
  await expect(page.getByText("not-an-address is not a valid email address.")).toBeVisible();
  await page.getByLabel("To", { exact: true }).fill("plant.manager@example.com");
  await expect(page.getByText(/Saved · version \d+/)).toBeVisible();

  await page.getByRole("button", { name: "Preview and send" }).click();
  const dialog = page.getByRole("dialog", { name: "Confirm send" });
  await expect(dialog).toContainText("plant.manager@example.com");
  await expect(dialog).toContainText("Outside your company: example.com");
  if (viaEmailJs) {
    await expect(dialog).toContainText("From EmailJS (your connected Gmail service)");
    await expect(dialog).toContainText("figures in the message (no attachment)");
    await expect(dialog.getByRole("button", { name: "Confirm send" })).toBeEnabled();
  } else {
    await expect(dialog).toContainText("Email sending is not connected");
    await expect(dialog.getByRole("button", { name: "Confirm send" })).toBeDisabled();
  }
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
});

test("viewers cannot open reports", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Viewer (Tapeline, Warping)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("link", { name: "Reports" })).toHaveCount(0);
  await page.goto("/reports");
  await expect(page.getByText("Reports are available to Reviewers and Senders.")).toBeVisible();
  expect((await page.request.get("/api/v1/reports")).status()).toBe(403);
});
