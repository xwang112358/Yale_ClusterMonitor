# CLAUDE.md — Yale ClusterMonitor + Azure Usage dashboard

Orientation for Claude Code working in this repo. The **full deploy runbook** is in
`DEPLOY_AZURE.md` ("Update 2026-06" section); this file is the map + the hard-won gotchas.

## What this is
Two things ship from this repo:
1. **Cluster Monitor** — Flask app (`app.py`) showing Yale HPC GPU stats, one page per
   cluster (template `index.html`): Misha at `/`, Bouchet at `/bouchet`. Cluster data is
   pushed in from the HPC side (`misha-side/pusher.sbatch` runs unchanged on every cluster,
   `deploy/monitor-receive.sh` files it by cluster). Adding a cluster: `DEPLOY_BOUCHET.md`.
2. **Azure Usage dashboard** — `/azure` (login-required). Azure OpenAI / Cognitive Services
   spend for the `image-text-medical` resource group: Year/Month navigation, per-resource
   billing, per-model token/call usage, and a "Tracked resources" roster including idle/brand-new
   resources. Code: `azure_dashboard.py` + `templates/azure.html`.

**Data pipeline** that feeds `/azure` (version-controlled here, but on the droplet it RUNS
from a separate dir so the Azure secret stays out of the web app):
- `usage_monitor.py` — polls Azure Monitor metrics + Cost Management, writes `usage.db` (SQLite).
- `dashboard.py` — standalone offline HTML generator (same design as `/azure`) → `dashboard.html`.
- `rates.json` — USD-per-1M-token price table. **Retired from the UI** (2026-09): the
  dashboard no longer shows any dollar estimate. Still read by the pipeline, which keeps
  writing `estimated_cost_usd` into the snapshot; nothing renders it.

## Repo layout
- `app.py` — routes `/` (default cluster), `/<slug>` (other clusters), `/api/cluster[/<slug>]`,
  `/login`, `/logout`, `/azure`, `/healthz`. `/azure` lazy-imports `azure_dashboard`.
  Clusters come from `CLUSTERS=misha,bouchet` in `.env` plus `<SLUG>_HOST/_PARTITIONS/
  _SNAPSHOT_FILE/_LAB_ACCOUNT/_LABEL`; Misha also reads the original single-cluster names
  (`SNAPSHOT_FILE`, `MISHA_PARTITIONS`, `LAB_ACCOUNT`). Cache + staleness are per cluster.
- `azure_dashboard.py` — `build_context()` reads `usage.db`, returns per-month Plotly figure JSON
  + roster for `azure.html`. **No Azure calls** — it only reads the DB.
- `templates/azure.html` — client-side month switching via `Plotly.react` (Plotly from CDN);
  defaults to the current month in **US Eastern**, auto-rolls on the 1st; `#YYYY-MM` deep-links.
- `usage_monitor.py` / `dashboard.py` / `rates.json` — the pipeline (above).
- `deploy/` — Caddyfile + systemd unit *templates* (REPLACE_ME placeholders; live units differ).
  `deploy/monitor-receive.sh` is the forced command behind every pusher key in
  `/home/monitor/.ssh/authorized_keys`: no argument → `/var/lib/monitor/snapshot.txt` (Misha),
  `monitor-receive.sh bouchet` → `/var/lib/monitor/bouchet/snapshot.txt`. The KEY picks the
  file, never the pushed bytes. Re-copy it to `/usr/local/bin/` when it changes (it is not
  run from the repo).
- **Auth** (`app.py` "Auth" section, templates `_auth_base.html` + `login/invite/account/admin.html`):
  `users.json` holds `{password hash|null, display, admin?, invite?}`. Accounts are made from
  `/admin` (admin-only, 404 to others) as one-time expiring **invite links** (`/invite/<token>`;
  only the token's sha256 is stored, link shown once); re-issuing a link = password reset.
  `/account` changes your own password. State-changing POSTs carry a session CSRF token.
  `manage_users.py` is bootstrap/emergency only (`invite`, `admin`, `rename`, `add`, `reset`).
  `PUBLIC_URL` in `.env` sets the link host; ProxyFix trusts Caddy's forwarded headers.
- **Analysis** (nav button "Analysis"; `recorder.py` → `/var/lib/monitor/history.db` → `history.py` → `templates/history.html`
  at `/history[/<slug>]`): `cluster-history.timer` (5 min, units in `deploy/`) runs `recorder.py`,
  which reuses `app.fetch_cluster()` and stores COUNTS per (ts, cluster, gpu_type) — never per
  user/job (policy: mirror live scheduler output, don't archive named activity). Raw rows kept
  90 days, `hourly` rollup forever. `HISTORY_DB` must be in the app `.env` AND the service unit.
  `HISTORY_GPU_TYPES` (default a100,h100,h200,b200,rtx_pro_6000_blackwell) scopes the page;
  hours are US Eastern. Charts are Plotly client-side — the 1-vCPU/458 MB droplet does no chart
  work. Demand excludes held jobs (`HELD_REASONS`), shown as "· N held".
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
Host `cluster-monitor` = `root@159.223.173.141`, served at **`https://qingyuchen-lab-monitor.org`**
(Cloudflare Registrar, DNS-only records, since 2026-09-21). `www` and the original
`mishamonitor.duckdns.org` are permanent redirects to it in the Caddyfile, so old links work, but
campus firewalls (Vanderbilt's Palo Alto, category "dynamic DNS") block the DuckDNS name — never
hand it out again.
`PUBLIC_URL` in the app `.env` is the new name, so invite links carry it.
- **Certificates come from ZeroSSL, not Let's Encrypt** (`tls { issuer zerossl { email ... } }` on every
  site block in the live Caddyfile). Vanderbilt's Palo Alto inspects connections to "new" domains and
  rejected LE's 2026 chain (leaf ← YE2 ← Root YE ← ISRG X2): it re-signed with its "VU Cybersecurity
  Untrust" CA and reset the session. With the ZeroSSL chain the same connection passes. Don't switch
  back to LE without re-testing through that VPN (`openssl s_client -servername ... 159.223.173.141:443`).
  Vanderbilt's DNS (Infoblox, `10.52.144.1` on their VPN) separately sinkholes the new name to
  `35.168.95.233` as a newly-observed domain; that is theirs to lift (ticket) or to age out.
- `/home/monitor/ClusterMonitor/` — this repo. Run by `misha-monitor.service`
  (gunicorn on `127.0.0.1:5111`, `User=monitor`); Caddy reverse-proxies with TLS. A `systemctl`
  drop-in sets `AZURE_USAGE_DB=/home/monitor/azure-usage-monitor/usage.db`.
- `/home/monitor/azure-usage-monitor/` — the pipeline at runtime: `usage_monitor.py`, `rates.json`,
  `.env` (0600), `usage.db`. **Two timers** (units in `deploy/`):
  - `azure-usage-monitor.timer` (**4h**) → full run (metrics + Cost Management). The ONLY thing that
    calls Cost Management.
  - `azure-usage-metrics.timer` (**30 min**) → `usage_monitor.py --metrics-only`: refreshes token
    per-model token/call metrics, skips Cost Management (replays cached `billed_costs` so Billed never drops).
  - `cluster-history.timer` (**5 min**) → `ClusterMonitor/recorder.py` → `/var/lib/monitor/history.db`
    (GPU availability history for `/history`; `recorder.py --stats` shows what is recorded).
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
- **8–24h billing lag.** The current month reads ~$0 for the first day(s); token/call counts (from
  metrics) update immediately while billed `$` (from Cost Management) trails. Not a bug.
- **Custom timeframe capped at 1 year** by Azure → `query_cost_management` clamps the span to <365 days.
  Routine runs use a 2-month rolling window (`BILLING_LOOKBACK_MONTHS`); `--backfill [N]` for a deep fill.
- **Two metric vocabularies.** Legacy OpenAI accounts: `ProcessedPromptTokens`/`GeneratedTokens`/`TotalCalls`.
  AIServices accounts + `accounts/projects` children: `InputTokens`/`OutputTokens`/`ModelRequests` (they
  reject the legacy names). `METRIC_BUCKETS` normalizes both into canonical buckets. Projects are
  discovered too, labelled `<child> (project)`.
- **Foundry bills Anthropic models through a separate Marketplace SaaS resource.**
  A Claude deployment on a Foundry account does NOT bill to that account. Cost Management
  reports `.../providers/Microsoft.SaaS/resources/<model>-<parent>-<uid>` (e.g.
  `claude-sonnet-4-dac2bdbdec684f4-...`), so the spend used to land as its own opaque row.
  `<parent>` is the **first 15 hex chars of the hosting account's `properties.internalId`**
  (ARM `GET <account>?api-version=2023-05-01`) — that prefix is the only link back.
  `usage_monitor.fetch_account_internal_ids()` builds the map and
  `saas_parent_name()` folds the row onto the account; `repair_saas_resource_names()`
  heals rows already in the DB (`billed_costs` keeps `resource_id`, so this is offline —
  no Cost Management call). Run `python usage_monitor.py --repair-saas-names` after deploy.
- **The marketplace name clips the model to 15 chars**, so `claude-haiku-4-5` arrives as
  `claude-haiku-4-` and BOTH `claude-sonnet-4-5` and `claude-sonnet-4-6` arrive as
  `claude-sonnet-4`. Never display that token as the version — it names a model that may
  not exist. `resolve_saas_labels()` resolves it against the account's real deployment
  roster (`discover_deployments()`, stored in the snapshot as `deployments_by_account`):
  a unique prefix match names the model; a collision is paired 1:1 by **creation order
  cross-checked against first-billed order** (the marketplace resource is minted with the
  deployment, so the two orders agree); anything else stays unresolved with a trailing
  ellipsis. Nothing in it is Anthropic-specific — DeepSeek/Mistral resolve identically.
- **All Claude models share ONE meter** (`Claude in Microsoft Foundry (Anthropic hosted)
  - claude-ccu-anthropic-hosted-plan - claude-consumption-units`) because CCU is a pooled
  unit. The model is NOT in the meter, and the SaaS resource 404s in ARM, so the deployment
  roster is the only route. Older PAYGO meters (`Claude Sonnet 4.5 - anthropic-... -
  paygo-inference-output-tokens`) do name the model; `model_family()` still handles those.
- **Don't probe Cost Management for extra grouping dimensions.** Tried `ProductOrderName` /
  `MeterSubcategory` to get the model authoritatively — instant 429. One call per 4h run is
  the whole budget.
- Keep `dashboard.py` in sync with `azure_dashboard.py` — it deliberately duplicates the
  resolver and palette (it must stay standalone for the pipeline dir).
- **Resolve the two metric vocabularies PER DEPLOYMENT, not per account.** A Foundry
  account hosts both kinds at once: its OpenAI deployments report `ProcessedPromptTokens`/
  `GeneratedTokens`, its Anthropic/marketplace deployments report ONLY `InputTokens`/
  `OutputTokens`. `query_resource_metrics()` used to stop at the first vocabulary carrying
  *any* data — the legacy metric answered for the GPT deployments, so every Claude token was
  silently dropped and the estimate read $9 against $86 billed. Deployments are claimed by
  the first vocabulary that reports them (gpt-4o reports identical values under both, so it
  is never double-counted). Do not "optimise" this back into a per-account choice.
- **Never sum an undifferentiated metric with a per-deployment one.** `TotalCalls`
  reports a single `(all)` bucket (no deployment dimension, 4,057); `ModelRequests`
  splits per deployment (2,277). They count overlapping traffic, so merging them
  double-counted calls. `query_resource_metrics()` keeps an `(all)` aggregate only when
  NOTHING splits that bucket by deployment. The per-deployment merge is still right for
  tokens, where both vocabularies split and cover disjoint deployments.
- **Per-model rows come from `metric_points`, for EVERY month** (`load_model_rows()`),
  not from the snapshot — verified to reproduce the live snapshot's by_deployment
  numbers exactly, so there is one source rather than snapshot-for-now and
  something-else-for-history. Azure Monitor keeps ~93 days and collection started
  2026-04, so months before that get no model list (correct — better than zeros).
- **`calls` is None, not 0, when unknown.** The per-deployment calls metric
  (`ModelRequests`) reaches back ~31 days while the token metrics go further, so
  older months know tokens but not call counts. Those render as an em dash; writing
  0 would assert the model was never called.
- **`--backfill-metrics [N]`** re-fetches N days (max 93) of Azure Monitor history
  with the current collector. Azure Monitor is NOT rate-limited like Cost Management,
  so this is cheap. It deletes a stale `(all)` aggregate row ONLY where a
  per-deployment row covers the same resource/bucket/DAY — an earlier version deleted
  per-bucket across the whole window and wiped July's real call counts, since
  ModelRequests does not reach that far back.
- **Tracked resources shows tokens/calls per MODEL, never per resource** — a resource
  total sums models priced differently, so it means nothing. Models hang off each row as
  a collapsible list (`_model_rows()`), `(all)` and zero-activity models excluded. The
  resource-level count is still computed: it drives the IDLE/NEW badge.
- **The roster must never show a marketplace id or an idle project.** `build_roster()`
  folds marketplace ids onto their account via the snapshot's `saas_parents` (so the
  display is correct even if `billed_costs` was never repaired) and buckets an
  unresolvable one under "marketplace (unattributed)" rather than printing a 64-char id.
  An `accounts/projects` child with no activity is dropped — its traffic is already on
  the parent account, so it was a duplicate row (volmo-jaxon vs volmo-jaxon-resource).
  An ACTIVE project is still shown, which is why discovery keeps them.
- **The dollar estimate is RETIRED from the UI (2026-09) — don't add it back.** It was
  removed because it diverged too far from billed cost to be trusted: with rates and metrics
  both correct, claude-sonnet-4-5 estimated to the cent ($3.74 = $3.74) but claude-sonnet-4-6
  came in at $41.56 against $77.02 billed (~1.85x), most likely because adaptive-thinking
  tokens are billed as output but absent from Azure's `OutputTokens` metric. A per-model
  dollar figure that can be ~2x wrong is worse than none next to an authoritative billed
  number. What the dashboard shows now: **billed $** (Cost Management, authoritative) and
  **per-model tokens/calls** (Azure Monitor, near-live) — usage, not inferred money.
  The pipeline still computes `estimated_cost_usd` into the snapshot; nothing renders it.
- **Claude rates are Anthropic list rates.** Foundry bills in Claude Consumption Units
  ($0.01/CCU) but rates tokens at standard per-model rates, so `rates.json` uses the
  published $/MTok unchanged (platform.claude.com/docs/en/about-claude/pricing).
- **Resource-name casing.** Cost Management lowercases names (`belo2-yhf`); RM/metrics keep the created
  casing (`BELO2-YHF`). ALL resource-name matching must be **case-insensitive**; display prefers the
  created casing. Do not reintroduce case-sensitive `==` on resource names.
- **SQLite schema is stable** — `usage.db` is forward-compatible; backfills only add `billed_costs` rows.
  Tables: `metric_points` (per-deployment token/call timeseries, canonical bucket names), `billed_costs`
  (daily $ per resource+meter, names as Cost Management returns them = lowercase), `snapshots` (latest
  rollup the dashboard's roster comes from).

## Charts
- `/azure` shows ONE combined "Daily & cumulative spend" panel (bars = that day, line =
  running month total) on a **single $ axis** — both series are USD and the line is the
  running sum of the bars, so a second y-scale would only let them be drawn at arbitrary
  relative heights. The $2,000 budget is an order of magnitude above a normal month, so it
  is drawn only once actually crossed; until then pace + burn live in the subtitle.
- Text scale is ONE knob in two places that must move together: `html { font-size }`
  in the page CSS (every CSS size is rem) and `FONT_SCALE` in
  `azure_dashboard.py` / `dashboard.py`, which scales Plotly's px fonts via `fs()`.
  Change one without the other and the charts drift out of proportion.
- Model families are coloured per-hue ramps (gpt-5.x purple, gpt-4.x blue, Claude rose).
  The Claude ramp is validated as an *ordinal* ramp against the `#0f1117` surface.

## Validating dashboard changes
- `node` IS available on the Windows box (v24) even though the droplet has none.
- `/azure` requires login, so `curl` returns 302. Test the data layer directly:
  `AZURE_USAGE_DB=<db> python -c "from azure_dashboard import build_context; print(build_context().keys())"`.
- To eyeball the rendered page: render `azure.html` offline (stub Flask `url_for`, swap the CDN Plotly
  `<script>` for `plotly.offline.get_plotlyjs()`), then headless Chrome
  `chrome --headless --dump-dom <file>` or `--screenshot=out.png` with `--virtual-time-budget=8000`.
  (Edge/Chrome are on the Windows box; `node`/`playwright` are not.)
