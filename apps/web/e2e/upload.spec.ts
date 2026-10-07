import { expect, type Page, test } from "@playwright/test";

// U1/U2 end to end in a real browser: sign in -> select files -> explicit removal of an unsupported
// file -> signed upload to storage -> quarantine scan (ClamAV) -> text extraction -> status in U2.

async function signInAsReviewer(page: Page) {
  await page.goto("/login");
  await page.getByRole("button", { name: "Reviewer (all departments)" }).click();
  await expect(page).toHaveURL(/\/dashboard$/); // sign-in lands on Overview since M4
  await page.goto("/uploads/new");
}

test("upload a note, see it scanned and read", async ({ page }) => {
  await signInAsReviewer(page);
  await expect(page.getByText(/Up to 20 files, 20 MiB each and 100 MiB in total/)).toBeVisible();

  await page.getByLabel("Department").selectOption({ label: "Tapeline" });
  const note = `Date 27/09/2026\nDepartment Tapeline\nMachine T-04\nProduction 1250 m\nTarget 1500\nRun ${Date.now()}\n`;
  await page.getByLabel("Add files").setInputFiles([
    { name: "shift-note.txt", mimeType: "text/plain", buffer: Buffer.from(note) },
    { name: "legacy.doc", mimeType: "application/msword", buffer: Buffer.from("old") },
  ]);

  // An unsupported file blocks processing until it is explicitly removed.
  await expect(page.getByText(".doc is not accepted")).toBeVisible();
  const processButton = page.getByRole("button", { name: "Process notes" });
  await expect(processButton).toBeDisabled();
  await page.getByRole("button", { name: "Remove legacy.doc" }).click();
  await expect(processButton).toBeEnabled();
  await processButton.click();

  await expect(page).toHaveURL(/\/batches\/[0-9a-f-]{36}$/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Processing" })).toBeVisible();
  // M3: a readable note continues from text extraction to entries waiting for review.
  await expect(page.getByText("Ready for review", { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(/Scanned clean \(clamav/)).toBeVisible();
  await expect(page.getByText("1 of 1 page processed", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Finished", { exact: true })).toBeVisible();
});

test("blocked camera explains itself and offers the file picker", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: () => Promise.reject(new DOMException("denied", "NotAllowedError")) },
    });
  });
  await signInAsReviewer(page);
  await page.getByRole("button", { name: "Capture photo" }).click();
  const dialog = page.getByRole("dialog", { name: "Capture a production note" });
  await expect(dialog.getByText("Camera access is blocked. Allow access or choose a file.")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Choose a file" })).toBeVisible();
  await dialog.getByRole("button", { name: "Close camera" }).click();
  await expect(dialog).toBeHidden();
});

test("a viewer reads the overview but cannot upload", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("button", { name: "Viewer (Tapeline, Warping)" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Upload notes" })).toHaveCount(0);
  await page.goto("/uploads/new");
  await expect(page.getByText("Uploading notes needs the Uploader or Reviewer role.")).toBeVisible();
});
