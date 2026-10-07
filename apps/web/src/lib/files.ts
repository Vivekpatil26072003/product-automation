// Client-side mirror of the server's upload limits, so people see exact reasons before uploading.
// The server re-checks everything; this is guidance, never the control.

import type { Limits } from "./api";

export const MiB = 1024 * 1024;

export function extensionOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i).toLowerCase();
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < MiB) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / MiB).toFixed(1)} MiB`;
}

export type QueuedCheck = { reason: string | null };

/** Per-file reasons, in queue order. A file is sendable only when its reason is null. */
export function checkQueue(files: { name: string; size: number }[], limits: Limits): QueuedCheck[] {
  const accepted = new Set(limits.accepted_extensions);
  const total = files.reduce((sum, f) => sum + f.size, 0);
  return files.map((f, index) => {
    const ext = extensionOf(f.name);
    if (!accepted.has(ext)) {
      return { reason: `${ext || "This file type"} is not accepted. Use ${limits.accepted_extensions.join(", ")}.` };
    }
    if (f.size === 0) return { reason: "The file is empty." };
    if (f.size > limits.max_file_bytes) return { reason: `Larger than ${limits.max_file_bytes / MiB} MiB.` };
    if (index >= limits.max_files) return { reason: `Only ${limits.max_files} files can be sent at once.` };
    if (total > limits.max_batch_bytes) {
      return { reason: `All files together exceed ${limits.max_batch_bytes / MiB} MiB. Remove some files.` };
    }
    return { reason: null };
  });
}

export function limitsSentence(limits: Limits): string {
  return (
    `Up to ${limits.max_files} files, ${limits.max_file_bytes / MiB} MiB each and ` +
    `${limits.max_batch_bytes / MiB} MiB in total. PDFs up to ${limits.max_pdf_pages} pages; ` +
    `Excel/Word up to ${limits.max_office_rows.toLocaleString("en-IN")} rows. ` +
    `Accepted: ${limits.accepted_extensions.join(", ")}.`
  );
}
