# Deployment

How to keep Web Canvas running on a machine you control. Installing it is in
the [README](../README.md); this covers running it as a service, upgrading,
HTTPS, logs and troubleshooting.

## Two ways to run it

| | Production (a release, or `npm run build`) | Development (`npm run dev`) |
|---|---|---|
| Processes | One: `api_server.py --serve-dist` | Two: the API on `127.0.0.1:3000`, Vite on `:5173` proxying `/api` |
| Needs | Python | Python and Node |
| Frontend changes | Rebuild (`npm run build`) | Hot reload |
| Backend changes | Restart the API | Restart the API (it does not reload itself) |

On Windows the installer (`WebCanvas-Setup-<version>.exe`, README "On
Windows") is the production shape: `WebCanvas.exe` is the frozen API server
(`packaging/windows/web_canvas_launcher.py`) serving the bundled app on
`127.0.0.1:3000` or the next free port, with its data in
`%LOCALAPPDATA%\WebCanvas` (`--storage-dir …\storage`, `WEB_CANVAS_DB_DIR=…\db`).
It is built and smoke-tested by `.github/workflows/windows-installer.yml` for
every published release and attached to it.

## As a systemd user service (production)

`~/.config/systemd/user/web-canvas.service`:

```ini
[Unit]
Description=Web Canvas
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/web_canvas
ExecStart=/opt/web_canvas/.venv/bin/python3 scripts/api_server.py --serve-dist --host 0.0.0.0 --port 8443 \
  --storage-dir /var/lib/web_canvas --ssl-certfile certs/server-cert.pem --ssl-keyfile certs/server-key.pem
Restart=always
RestartSec=2
StandardOutput=append:/opt/web_canvas/api-server.log
StandardError=append:/opt/web_canvas/api-server.log

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now web-canvas
loginctl enable-linger "$USER"       # keep it running when you are logged out
```

A development deployment uses two units instead: one running
`api_server.py --host 127.0.0.1 --port 3000`, one running `npm run dev`'s Vite
(`npx vite --host`) in the repository.

## Upgrading

Extract the new release next to the old one (or `git pull` and
`npm run build`), keep the same `--storage-dir` and `.local_db/` (copy
`.local_db/` into the new directory, or upgrade in place), reinstall
`requirements.txt`, and restart. Database migrations run at startup; back up
`.local_db/metadata.db` before upgrading.

Restart when no turn is running: a server-side turn survives a restart (it
resumes from its last step), but the step in flight is redone.

## HTTPS

Sessions use `Secure` cookies, which browsers keep only over HTTPS or on
`localhost`. For access from other devices either terminate TLS in a reverse
proxy (nginx, Caddy) in front of `--host 127.0.0.1`, or let the server do it
with `--ssl-certfile` / `--ssl-keyfile`.

A self-signed certificate must list, as `IP Address` entries (not `DNS`),
every IP you browse to: Chrome lets a mismatch through after a click, but
Firefox then fails same-origin requests with "NetworkError when attempting to
fetch resource". Edit `[alt]` in `certs/san.cnf`, then:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/server-key.pem \
  -out certs/server-cert.pem -days 825 -config certs/san.cnf
openssl x509 -in certs/server-cert.pem -noout -ext subjectAltName   # check
```

Each browser accepts the certificate once ("Advanced" → proceed), and again
after it is regenerated. The development server uses `certs/dev-cert.pem` /
`certs/dev-key.pem` the same way.

## Logs

- `api-server.log` (or wherever the unit sends it): the API's own lines —
  job starts, time to first token, per-job usage with prompt-cache hits, and a
  `prefix:` line saying where a request departs from the conversation's
  previous one.
- `.local_db/step-journal/<date>.jsonl` (kept 7 days): every finished step's
  reasoning, visible text and tool calls — why a turn did what it did.
- `app.log`: the development orchestrator and Vite.

## Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| Login works on the server's own browser but not from another device | Is it plain HTTP? | Serve HTTPS (above) |
| "NetworkError" in Firefox only | The certificate's SAN lacks the IP you browse to | Add it to `certs/san.cnf`, regenerate, re-accept |
| Every request fails | `systemctl --user status web-canvas`, the log's tail | Restart; check the Python dependencies and that the storage directory is mounted |
| Port in use | `fuser 8443/tcp` | Stop the other process |
| A backend fix "did not work" | `ps -o lstart= -p $(pgrep -f api_server.py)` | The process predates the change: restart it |
| `--serve-dist: no index.html` | | `npm run build`, or install a release |
| The model has no web tools | `python -c "import playwright"` | Install Playwright and Chromium (README) |
