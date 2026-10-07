// Human-readable status for each file on the processing screen (U2).
// Status is always conveyed in words; colour (tone) only reinforces it.

import type { BatchFile, Job } from "./api";

export type Tone = "neutral" | "progress" | "success" | "warning" | "error";
export type FileStatus = { label: string; detail: string | null; tone: Tone };

const REJECT_LABEL = "Not processed";

function jobRunning(job: Job | null): boolean {
  return !!job && ["QUEUED", "RUNNING", "RETRY_WAIT"].includes(job.state);
}

export function pagesLine(f: BatchFile): string | null {
  const total = f.pages.total;
  if (!total) return null;
  return `${f.pages.processed} of ${total} page${total === 1 ? "" : "s"} processed`;
}

export function fileStatus(f: BatchFile): FileStatus {
  switch (f.state) {
    case "UPLOADING":
      return { label: "Waiting for upload", detail: null, tone: "neutral" };
    case "EXPIRED":
      return { label: "Upload expired", detail: "Upload the file again.", tone: "warning" };
    case "REJECTED":
      return { label: REJECT_LABEL, detail: f.reject?.message ?? null, tone: "error" };
    case "QUARANTINED": {
      const scan = f.jobs.scan;
      if (scan?.state === "CANCELLED") return { label: "Cancelled before scanning", detail: null, tone: "neutral" };
      if (scan?.state === "RETRY_WAIT")
        return { label: "Scanning delayed", detail: scan.error?.message ?? "Retrying automatically.", tone: "warning" };
      if (scan?.state === "FAILED")
        return { label: "Scan failed", detail: scan.error?.message ?? null, tone: "error" };
      return { label: "Checking file safety", detail: null, tone: "progress" };
    }
    case "READY": {
      const parse = f.jobs.parse;
      if (!parse || jobRunning(parse)) {
        const delayed = parse?.state === "RETRY_WAIT";
        return {
          label: delayed ? "Reading delayed" : "Reading pages",
          detail: delayed ? (parse?.error?.message ?? "Retrying automatically.") : pagesLine(f),
          tone: delayed ? "warning" : "progress",
        };
      }
      const extract = f.jobs.extract;
      if ((parse.state === "SUCCEEDED" || parse.state === "PARTIAL") && extract) {
        if (jobRunning(extract)) return { label: "Finding entries", detail: pagesLine(f), tone: "progress" };
        if (extract.state === "FAILED")
          return { label: "Enter manually", detail: extract.error?.message ?? null, tone: "warning" };
        if (f.to_review > 0)
          return {
            label: "Ready for review",
            detail: `${f.to_review} entr${f.to_review === 1 ? "y" : "ies"} to check`,
            tone: parse.state === "PARTIAL" ? "warning" : "success",
          };
      }
      if (parse.state === "SUCCEEDED") return { label: "Text extracted", detail: pagesLine(f), tone: "success" };
      if (parse.state === "PARTIAL")
        return {
          label: "Partly read",
          detail: `Pages ${f.pages.failed.join(", ")} could not be read. ${parse.error?.message ?? ""}`.trim(),
          tone: "warning",
        };
      if (parse.state === "CANCELLED") return { label: "Reading cancelled", detail: pagesLine(f), tone: "neutral" };
      return { label: "Could not be read", detail: parse.error?.message ?? null, tone: "error" };
    }
  }
}

export function scanLabel(f: BatchFile): string | null {
  switch (f.scan_status) {
    case "CLEAN":
      return `Scanned clean${f.scanner ? ` (${f.scanner})` : ""}`;
    case "SKIPPED_DEV":
      return "Not scanned: malware scanning is disabled in this development environment";
    case "INFECTED":
      return "Malware scanner flagged this file";
    default:
      return null;
  }
}
