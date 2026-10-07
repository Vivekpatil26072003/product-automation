"use client";

// Step-by-step status of one upload batch, from the server's real states (never "done" before it is done):
// Uploaded -> Reading -> Review -> Approved and saved -> PDF report -> Emailed to owner.

export type Stage = { key: string; label: string; state: "done" | "current" | "failed" | "waiting" | "skipped"; detail: string };

const MARK: Record<Stage["state"], { sign: string; text: string }> = {
  done: { sign: "✓", text: "done" },
  current: { sign: "…", text: "in progress" },
  failed: { sign: "!", text: "needs attention" },
  waiting: { sign: "○", text: "not yet" },
  skipped: { sign: "–", text: "off" },
};

export function BatchPipeline({ stages, compact = false }: { stages: Stage[]; compact?: boolean }) {
  return (
    <ol className={`pipeline${compact ? " compact" : ""}`} aria-label="Processing steps">
      {stages.map((s) => (
        <li key={s.key} className={`step step-${s.state}`}>
          <span className="step-mark" aria-hidden="true">{MARK[s.state].sign}</span>
          <span>
            <strong>{s.label}</strong> <span className="sr-only">({MARK[s.state].text})</span>
            {!compact && s.detail && <span className="meta step-detail">{s.detail}</span>}
          </span>
        </li>
      ))}
    </ol>
  );
}
