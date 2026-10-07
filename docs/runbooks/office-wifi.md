# Runbook: run the app on an office PC for phones on the same Wi-Fi

The app runs on one Windows PC. Workers open it from phones or computers on the same network. Nothing runs in the
cloud except the services the app calls: Gemini to read photos, EmailJS to send email.

**Static hosts such as Netlify cannot run this app.** They serve only the website. The API, the worker,
PostgreSQL, Redis, the file storage and the virus scanner must keep running somewhere: here, on the office PC.

## Start / stop

| What | How |
|---|---|
| Start (also runs automatically at Windows login) | `powershell -ExecutionPolicy Bypass -File infra\local\start-office.ps1` |
| Stop (data is kept) | `powershell -ExecutionPolicy Bypass -File infra\local\stop-office.ps1` |
| Logs | `infra\local\logs\*.log` |

The script:
1. Finds the PC's network address.
2. Starts Docker Desktop and the services.
3. Migrates the database and allows uploads from that address.
4. Builds the website, but only when the code or the address changed (about a minute).
5. Starts the API (this PC only), the worker and the website (port 3000 on the whole network).
6. Prints the link, e.g. `http://10.225.215.169:3000`.

## Setup

- **Auto-start:** `Production automation app.cmd` in the Windows Startup folder
  (`shell:startup`) runs the script at login.
- **Firewall:** "Node.js JavaScript Runtime" (website, port 3000) and "Docker Desktop Backend" (storage, port 9000)
  must be allowed inbound. Phones upload photos straight to storage on port 9000.
- **Keep the PC on:** Settings → System → Power → Sleep: Never (when plugged in).
- **Same address every day:** in the Wi-Fi router, reserve the PC's address (DHCP reservation), so the link
  workers saved keeps working. If the address changes anyway, run the script again (or restart the PC) and share
  the new link it prints.
- **Do not run `npm run dev` at the same time.** Port 3000 is already used by the app ("EADDRINUSE"). Stop the app
  first with `stop-office.ps1` to develop.

## Limits

- **Plain http on the local network.** "Take photo" opens the phone's camera app and works. The in-page live camera
  needs https and falls back to choosing a file.
- **Quick sign-in buttons.** Anyone on the Wi-Fi can open the app with them (`APP_ENV=development`). Use only on a
  trusted office network with a Wi-Fi password, or add a real login before wider use.
