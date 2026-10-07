"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiGet } from "@/lib/api";
import { dayLabel } from "@/lib/sheets";

// The daily sheets and pick registers that pages of one upload were read into, with how many values still need a
// person's check.

type Item = {
  id: string; kind?: "sheet" | "register"; report_date: string; state: "DRAFT" | "APPROVED"; values_read: number; uncertain: number;
};

export function SheetLinks({ batchId, refresh = "" }: { batchId: string; refresh?: string }) {
  const [items, setItems] = useState<Item[]>([]);

  useEffect(() => {
    let active = true;
    apiGet<{ data: Item[] }>(`/batches/${batchId}/sheets`).then(
      (r) => active && setItems(r.data),
      () => active && setItems([]),
    );
    return () => {
      active = false;
    };
  }, [batchId, refresh]);

  if (items.length === 0) return null;
  return (
    <ul className="filelist" aria-label="Daily sheets from this upload">
      {items.map((s) => {
        const reg = s.kind === "register";
        return (
        <li key={s.id}>
          <div>
            <strong>{reg ? "Pick register" : "Daily sheet"} · {dayLabel(s.report_date)}</strong>
            <div className="meta">
              {s.values_read} values read · {s.state === "APPROVED" ? "approved" : s.uncertain ? `${s.uncertain} to check` : "ready to approve"}
            </div>
          </div>
          <Link className={`button${s.state === "DRAFT" ? " primary" : ""}`} href={reg ? `/registers/${s.id}` : `/sheets/${s.id}`}>
            {s.state === "DRAFT" ? (reg ? "Check register" : "Check sheet") : (reg ? "Open register" : "Open sheet")}
          </Link>
        </li>
        );
      })}
    </ul>
  );
}
