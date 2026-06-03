# CLAUDE.md — Yale ClusterMonitor + Azure Usage dashboard

Orientation for Claude Code working in this repo. The **full deploy runbook** is in
`DEPLOY_AZURE.md` ("Update 2026-06" section); this file is the map + the hard-won gotchas.

## What this is
Two things ship from this repo:
1. **Misha Monitor** — Flask app (`app.py`) showing Yale HPC cluster GPU stats at `/`
   (template `index.html`). Cluster data is pushed in from the HPC side (`misha-side/`,
   `deploy/monitor-receive.sh`).
2. **Azure Usage dashboard** — `/azure` (login-required). Azure OpenAI / Cognitive Services
   spend for the `image-text-medical` resource group: Year/Month navigation, per-resource
   billing, a token-based estimate, and a "Tracked resources" roster including idle/brand-new
   resources. Code: `azure_dashboard.py` + `templates/azure.html`.

**Data pipeline** that feeds `/azure` (version-controlled here, but on the droplet it RUNS
from a separate dir so the Azure secret stays out of the web app):
- `usage_monitor.py` — polls Azure Monitor metrics + Cost Management, writes `usage.db` (SQLite).
- `dashboard.py` — standalone offline HTML generator (same design as `/azure`) → `dashboard.html`.
- `rates.json` — USD-per-1M-token price table for the estimate.

## Repo layout
- `app.py` — routes `/`, `/login`, `/logout`, `/azure`, `/healthz`. `/azure` lazy-imports `azure_dashboard`.
- `azure_dashboard.py` — `build_context()` reads `usage.db`, returns per-month Plotly figure JSON
  + roster for `azure.html`. **No Azure calls** — it only reads the DB.
- `templates/azure.html` — client-side month switching via `Plotly.react` (Plotly from CDN);
  defaults to the current month in **US Eastern**, auto-rolls on the 1st; `#YYYY-MM` deep-links.
- `usage_monitor.py` / `dashboard.py` / `rates.json` — the pipeline (above).
- `deploy/` — Caddyfile + systemd unit *templates* (REPLACE_ME placeholders; live units differ).
- **Runtime-only, NOT in git** (`.gitignore`): `.env`, `usage.db`, `users.json`, `.flask_secret`, `.venv/`.

## Local dev on a fresh machine
1. venv + deps:
   `pip install Flask gunicorn plotly azure-identity "azure-monitor-query<2.0" azure-mgmt-resource python-dotenv requests`
   — the **`<2.0` pin matters**: 2.x relocated `MetricAggregationType` and breaks the import.
2. Pipeline needs a `.env` (NOT committed) with the service-principal creds:
   `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, optional `MONTHLY_BUDGET_USD`.
   Subscription ID + resource group are constants in `usage_monitor.py`.
3. Bootstrap data: `python usage_monitor.py` → creates/refreshes `usage.db`.
   Then `python dashboard.py` → `dashboard.html` for local preview.
4. Web app needs its own `.env` (Flask secret), `users.json` (see `users.json.example`), and
   `AZURE_USAGE_DB` pointing at the `usage.db`.

## Droplet (production)
Host `cluster-monitor` = `root@159.223.173.141`, served at `https://mishamonitor.duckdns.org`.
- `/home/monitor/ClusterMonitor/` — this repo. Run by `misha-monitor.service`
  (gunicorn on `127.0.0.1:5111`, `User=monitor`); Caddy reverse-proxies with TLS. A `systemctl`
  drop-in sets `AZURE_USAGE_DB=/home/monitor/azure-usage-monitor/usage.db`.
- `/home/monitor/azure-usage-monitor/` — the pipeline at runtime: `usage_monitor.py`, `rates.json`,
  `.env` (0600), `usage.db`. Run by `azure-usage-monitor.timer` **every 4h** — the ONLY thing that
  calls Cost Management.
- Shared venv: `/home/monitor/ClusterMonitor/.venv` (used by both the app and the pipeline).

### Deploy (push → pull; full version in DEPLOY_AZURE.md)
```bash
# laptop
git push origin main
# droplet
ssh root@159.223.173.141
sudo -u monitor bash -lc 'cd ~/ClusterMonitor && git pull --ff-only'
systemctl restart misha-monitor                                   # REQUIRED to load app/template changes
sudo -u monitor cp /home/monitor/ClusterMonitor/usage_monitor.py \
                   /home/monitor/ClusterMonitor/rates.json \
                   /home/monitor/azure-usage-monitor/              # sync pipeline (absolute paths!)
```
`build_context()` re-reads the DB per request, so dashboard data refreshes without a restart; but
restart IS needed for code/template changes (gunicorn caches Python modules + Jinja templates).

### SSH gotcha (this bit us)
`sudo -u monitor cp ~/...` expands `~` in **root's** shell (→ `/root`) *before* sudo switches user.
Use **absolute paths** (`/home/monitor/...`) or wrap: `sudo -u monitor bash -lc '... ~/...'`.
Non-interactive ssh: `ssh -o BatchMode=yes -o ConnectTimeout=12 root@159.223.173.141 '<cmd>'`.

## Hard-won gotchas (Azure)
- **Cost Management is aggressively rate-limited (429, QPU-based).** Only the 4h timer should query
  it (1 call/run). DON'T run repeated `usage_monitor.py --backfill`. If a backfill 429s, load history
  a different way: build a portable SQLite of `billed_costs` rows on a machine that already has them,
  scp it, and merge — `ATTACH '/tmp/hist.db' AS h; INSERT OR REPLACE INTO billed_costs SELECT * FROM h.billed_costs;`
- **8–24h billing lag.** The current month reads ~$0 for the first day(s); the token estimate (from
  metrics) updates immediately while billed `$` (from Cost Management) trails. Not a bug.
- **Custom timeframe capped at 1 year** by Azure → `query_cost_management` clamps the span to <365 days.
  Routine runs use a 2-month rolling window (`BILLING_LOOKBACK_MONTHS`); `--backfill [N]` for a deep fill.
- **Two metric vocabularies.** Legacy OpenAI accounts: `ProcessedPromptTokens`/`GeneratedTokens`/`TotalCalls`.
  AIServices accounts + `accounts/projects` children: `InputTokens`/`OutputTokens`/`ModelRequests` (they
  reject the legacy names). `METRIC_BUCKETS` normalizes both into canonical buckets. Projects are
  discovered too, labelled `<child> (project)`.
- **Resource-name casing.** Cost Management lowercases names (`belo2-yhf`); RM/metrics keep the created
  casing (`BELO2-YHF`). ALL resource-name matching must be **case-insensitive**; display prefers the
  created casing. Do not reintroduce case-sensitive `==` on resource names.
- **SQLite schema is stable** — `usage.db` is forward-compatible; backfills only add `billed_costs` rows.
  Tables: `metric_points` (per-deployment token/call timeseries, canonical bucket names), `billed_costs`
  (daily $ per resource+meter, names as Cost Management returns them = lowercase), `snapshots` (latest
  rollup the dashboard's roster comes from).

## Validating dashboard changes (no node/playwright on these boxes)
- `/azure` requires login, so `curl` returns 302. Test the data layer directly:
  `AZURE_USAGE_DB=<db> python -c "from azure_dashboard import build_context; print(build_context().keys())"`.
- To eyeball the rendered page: render `azure.html` offline (stub Flask `url_for`, swap the CDN Plotly
  `<script>` for `plotly.offline.get_plotlyjs()`), then headless Chrome
  `chrome --headless --dump-dom <file>` or `--screenshot=out.png` with `--virtual-time-budget=8000`.
  (Edge/Chrome are on the Windows box; `node`/`playwright` are not.)
