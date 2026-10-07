# Runbook: integrations (Google Sheets, Power BI, Microsoft 365 email, ERP)

Administrators manage connections at **Integrations** (`/settings/integrations`). Only approved records leave the system, and nothing is sent until a connection test passes.

## Before you start (operator)

- `INTEGRATION_KEYS` must be set for the API and the worker (the same value for both). Generate one per environment:
  `python -c "from app.core.crypto import new_key; print(new_key())"`. In production, load it from the secret store.
- **Rotation:** put the new key first and keep the old one after it (`new:...,old:...`). New and edited credentials use the new key, and old ones still decrypt. Remove the old key only after every connection has been re-saved.
- If a key is lost, stored credentials cannot be recovered. The connection shows "Stored credentials cannot be read": enter them again.

## Google Sheets

1. In Google Cloud, create a service account and a JSON key, then enable the Google Sheets API.
2. Share the spreadsheet with the service account's email as **Editor**. Create a tab (default name `Production_Data`), empty or holding the exact header the system writes.
3. Connect: spreadsheet ID (from the URL), tab, and paste the key file. The test reads metadata and the header only.
4. Treat the sheet as read-only. People must not edit, sort or insert rows in the tab: row identity is `record_id`, and edits are overwritten by the next change to that record.

| Symptom | Meaning | Action |
|---|---|---|
| Stopped: destination conflict, `SCHEMA_MISMATCH` | Header row changed | Restore the header (or empty the tab), then **Test connection** |
| Stopped: destination conflict, `DUPLICATE_KEY` | A record_id appears twice | Delete the duplicate row, then **Test connection** |
| Reconnect required, `AUTH_FAILED` / `DESTINATION_NOT_FOUND` | Key revoked, sheet unshared or deleted | Fix access or replace the key, then test |
| Records "Conflict" (`DESTINATION_NEWER`) | The sheet holds a newer revision than this system | Investigate. The row was deliberately not overwritten |
| Some records failed | Five transient failures in a row | **Retry failed records** |

A passed test, or **Resend all records**, re-checks every approved record. It is safe to repeat: rows are updated in place, never duplicated.

## Power BI

1. In Microsoft Entra, register an application with a client secret. Allow service principals to use Power BI APIs (tenant setting) and add the application to the workspace as Member or Contributor.
2. Create the read-only database login Power BI will use (run as operator; the password comes from the environment):
   ```
   set BI_ROLE_PASSWORD=<at least 16 characters>
   python -m app.cli bi-access --role bi_demo --tenant "Demo Manufacturing (illustrative data)"
   ```
   The login can read only `approved_production_v` and `production_watermark_v`, and only for that company. `--revoke` removes the mapping.
3. In Power BI Desktop, import both views through the PostgreSQL connector (via a gateway if the database is private), publish, and configure the credentials on the semantic model.
4. Connect here: tenant, workspace ID, semantic model ID, client ID and secret. Optionally set the minimum minutes between refreshes (default 30) and when to show "out of date" (default 180).

Refreshes are requested automatically after approved data changes. Requests inside the minimum interval become one later refresh. The Overview shows "Power BI: Up to date / Out of date / Refreshing / Refresh failed". The native dashboard is always current regardless.

## Microsoft 365 email

Register an application with the **Mail.Send** application permission and admin consent. Then restrict it to the sender mailbox with an Exchange application access policy (strongly recommended; the test cannot check this). The M5 test confirms Mail.Send is granted and sends nothing. Sending arrives with reports (M6).

## ERP

No ERP vendor is configured. Turning on `feature_flags.erp_integration` in company settings enables the section. Only the development **mock** adapter exists, and it is refused in staging and production. Adding a real ERP means implementing `ErpAdapter` in `app/integrations/erp.py` and agreeing field mappings (`mapping_version`).

## Disconnecting

**Disconnect** erases the stored credentials, cancels queued jobs and stops all sending. History (sync ledger, refreshes, audit) is kept. Data already in the destination is not deleted.

## Where to look

- Jobs: `GET /api/v1/sync-jobs?connection_id=...` (administrators), or the `job` table (`sheets.sync`, `erp.sync`, `powerbi.refresh`, `integration.test`, `integrations.fanout`).
- Per-record state: `record_sync`. ERP attempts: `erp_sync_attempt`. Refreshes: `powerbi_refresh`.
- Audit actions: `INTEGRATION_CREATED`, `INTEGRATION_UPDATED`, `INTEGRATION_STATE_CHANGED`, `INTEGRATION_DISCONNECTED`, `INTEGRATION_TEST/RECONCILE/RETRY_FAILED`, `POWERBI_REFRESH_REQUESTED`.
