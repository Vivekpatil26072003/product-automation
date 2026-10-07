"""Google Sheets projection (FR13, spec §11 "Google Sheets").

A one-way, read-only-for-the-business copy of approved records in a company-owned spreadsheet tab.
Rules implemented here:
- Row identity is record_id (never row position). The adapter loads the header and every key first.
- Header must equal COLUMNS exactly (schema version 1); an empty tab gets the header written.
  A changed header or a duplicated record_id is a CONFLICT: nothing more is written until repaired.
- Existing rows are updated in place; new rows are appended. Because every run reads the keys before
  writing, a timed-out append is never repeated blindly: the next run sees the row if it landed.
- All values are written with valueInputOption=RAW, so text such as "=SUM(A1)" stays text.
- A row is only overwritten by an equal or newer revision.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.auth import google_token
from app.integrations.base import IntegrationError, Token, client, request

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCHEMA_VERSION = 1
COLUMNS = [
    "record_id",
    "revision",
    "schema_version",
    "date",
    "department",
    "operator",
    "machine",
    "production_qty",
    "target_qty",
    "unit",
    "status",
    "stop_minutes",
    "remarks",
    "record_state",
    "approved_at",
    "updated_at",
    "department_id",
    "machine_id",
]
LAST_COL = "R"  # 18 columns
MAX_BATCH_BYTES = 1_500_000  # stay below the documented 2 MB request guidance


def row_values(rec: dict[str, Any]) -> list[Any]:
    """One record as sheet cells. Quantities are numbers only when exactly representable."""

    def qty(value: str) -> Any:
        d = Decimal(value)
        return float(d) if Decimal(repr(float(d))) == d else format(d, "f")

    return [
        rec["record_id"],
        rec["revision"],
        SCHEMA_VERSION,
        rec["production_date"],
        rec["department_name"],
        rec["operator_name"],
        rec["machine_code"],
        qty(rec["production_qty"]),
        qty(rec["target_qty"]),
        rec["unit"],
        rec["status"],
        rec["stop_minutes"],
        rec["remarks"],
        rec["state"],
        rec["approved_at"],
        rec["updated_at"],
        rec["department_id"],
        rec["machine_id"],
    ]


@dataclass
class SheetState:
    rows: dict[str, tuple[int, int]]  # record_id -> (1-based row number, revision)
    next_row: int
    header_written: bool = False


class SheetsAdapter:
    def __init__(self, config: dict[str, Any], secret: dict[str, Any], http: httpx.Client | None = None):
        self.spreadsheet_id = config["spreadsheet_id"]
        self.tab = config.get("tab") or "Production_Data"
        self._account = secret.get("service_account") or {}
        self.http = client(http)
        self._token: Token | None = None

    def _auth(self) -> dict[str, str]:
        if self._token is None or not self._token.fresh:
            self._token = google_token(self.http, self._account)
        return {"Authorization": f"Bearer {self._token.value}"}

    def _range(self, a1: str) -> str:
        return quote(f"'{self.tab}'!{a1}", safe="")

    def test(self) -> dict[str, Any]:
        """Metadata and header check only; never writes production data (spec §11 connection test)."""
        meta = request(
            self.http,
            "GET",
            f"{API}/{self.spreadsheet_id}",
            "Read spreadsheet",
            headers=self._auth(),
            params={"fields": "properties.title,sheets.properties.title"},
        ).json()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if self.tab not in tabs:
            raise IntegrationError("TAB_NOT_FOUND", f'The spreadsheet has no tab named "{self.tab}".', reconnect=True)
        header = self._read(f"A1:{LAST_COL}1")
        if header and header[0] != COLUMNS:
            raise IntegrationError(
                "SCHEMA_MISMATCH", "The tab's header row does not match the expected columns.", conflict=True
            )
        return {"title": meta.get("properties", {}).get("title"), "tab": self.tab, "header_present": bool(header)}

    def _read(self, a1: str) -> list[list[Any]]:
        resp = request(
            self.http,
            "GET",
            f"{API}/{self.spreadsheet_id}/values/{self._range(a1)}",
            "Read rows",
            headers=self._auth(),
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
        )
        return resp.json().get("values", [])

    def load(self) -> SheetState:
        values = self._read(f"A1:{LAST_COL}")
        if not values:
            self._write_rows({1: COLUMNS})
            return SheetState({}, 2, header_written=True)
        if values[0] != COLUMNS:
            raise IntegrationError(
                "SCHEMA_MISMATCH",
                "The tab's header row was changed. Restore it or approve a new mapping before syncing continues.",
                conflict=True,
            )
        rows: dict[str, tuple[int, int]] = {}
        for n, row in enumerate(values[1:], start=2):
            if not row or not row[0]:
                continue
            key = str(row[0])
            if key in rows:
                raise IntegrationError(
                    "DUPLICATE_KEY", f"record_id {key} appears more than once in the sheet.", conflict=True
                )
            try:
                revision = int(row[1])
            except (IndexError, TypeError, ValueError) as exc:
                raise IntegrationError("SCHEMA_MISMATCH", f"Row {n} has no valid revision.", conflict=True) from exc
            rows[key] = (n, revision)
        return SheetState(rows, len(values) + 1)

    def _write_rows(self, rows: dict[int, list[Any]]) -> None:
        batch, size = [], 0
        for n, values in sorted(rows.items()):
            item = {"range": f"'{self.tab}'!A{n}:{LAST_COL}{n}", "values": [values]}
            item_size = len(str(item))
            if batch and size + item_size > MAX_BATCH_BYTES:
                self._batch_update(batch)
                batch, size = [], 0
            batch.append(item)
            size += item_size
        if batch:
            self._batch_update(batch)

    def _batch_update(self, data: list[dict[str, Any]]) -> None:
        request(
            self.http,
            "POST",
            f"{API}/{self.spreadsheet_id}/values:batchUpdate",
            "Update rows",
            headers=self._auth(),
            json={"valueInputOption": "RAW", "data": data},
        )

    def append(self, rows: list[list[Any]]) -> None:
        if rows:
            request(
                self.http,
                "POST",
                f"{API}/{self.spreadsheet_id}/values/{self._range(f'A1:{LAST_COL}')}:append",
                "Append rows",
                headers=self._auth(),
                params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                json={"values": rows},
            )

    def upsert(self, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Write the given records. Returns record_id -> {row, revision, action} for each one written or kept."""
        state = self.load()
        updates: dict[int, list[Any]] = {}
        appends: list[tuple[str, list[Any]]] = []
        result: dict[str, dict[str, Any]] = {}
        for rec in records:
            existing = state.rows.get(rec["record_id"])
            if existing and existing[1] > rec["revision"]:
                result[rec["record_id"]] = {"row": existing[0], "revision": existing[1], "action": "NEWER_IN_SHEET"}
            elif existing:
                updates[existing[0]] = row_values(rec)
                result[rec["record_id"]] = {"row": existing[0], "revision": rec["revision"], "action": "UPDATED"}
            else:
                appends.append((rec["record_id"], row_values(rec)))
        self._write_rows(updates)
        self.append([values for _, values in appends])
        for i, (record_id, _) in enumerate(appends):
            result[record_id] = {
                "row": state.next_row + i,
                "revision": next(r["revision"] for r in records if r["record_id"] == record_id),
                "action": "APPENDED",
            }
        return result
