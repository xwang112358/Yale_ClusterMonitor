# Azure Usage dashboard — droplet rollout

One-time guide to add the Azure Usage view (`/azure`) to the already-deployed
Misha Monitor on `root@159.223.173.141`. Companion to [upgrade.md](upgrade.md).

---

## Update 2026-06 — month navigation, multi-month history, AIServices + new-resource visibility

The `/azure` view was redesigned and the data pipeline extended. **Apply this to the
already-running droplet** (the original rollout below is for a fresh install).

**What changed**
- `azure_dashboard.py` + `templates/azure.html`: Year/Month button navigation. The page
  opens on the **current month in US Eastern time** and auto-rolls on the 1st; click any
  past month to read it. A **Tracked resources** table now lists *every* discovered
  resource, including idle/brand-new ones with no billed spend (flagged NEW/IDLE) — these
  used to silently drop out of the billing charts. Month switching is client-side
  (`Plotly.react`), so figures for every month are embedded in one page.
- `usage_monitor.py` (data pipeline, **lives in `~/azure-usage-monitor/`, not in this repo**):
  1. Discovers `Microsoft.CognitiveServices/accounts/projects` children (Azure AI Foundry),
     labelled `<child> (project)`.
  2. Normalizes legacy OpenAI metrics *and* the AIServices vocabulary
     (`InputTokens`/`OutputTokens`/`ModelRequests`) into canonical buckets — fixes AIServices
     accounts reporting 0 tokens.
  3. Pulls billing over a rolling window (`BILLING_LOOKBACK_MONTHS`, default 2) instead of
     MonthToDate, so past months persist in `billed_costs`. `--backfill [N]` does a deep
     one-time fill (default 12 months; the Custom timeframe is auto-clamped to <1 year, an
     Azure hard limit).

**The SQLite schema is unchanged** — the existing droplet `usage.db` is forward-compatible.
`billed_costs` just accumulates more months; `metric_points` stores canonical bucket names
going forward (the dashboard doesn't read it). No migration needed.

**Deploy steps**

```bash
ssh root@159.223.173.141

# 1) Flask app: new view + template
sudo -u monitor bash -lc 'cd ~/ClusterMonitor && git pull --ff-only'
systemctl restart misha-monitor

# 2) Data pipeline: copy the updated usage_monitor.py into ~/azure-usage-monitor/
#    (from your laptop, since it is not in the Flask repo):
#    scp D:\xwang\summer26\monitor\usage_monitor.py monitor@159.223.173.141:/home/monitor/azure-usage-monitor/usage_monitor.py

# 3) One-time backfill so past months show up in the new nav (honors 429 retry):
sudo -u monitor bash -lc 'cd ~/azure-usage-monitor && ~/ClusterMonitor/.venv/bin/python usage_monitor.py --backfill 12'
```

The 4-hour timer then keeps the rolling 2-month window fresh automatically — no timer
change required. Verify at `https://mishamonitor.duckdns.org/azure`: the Year/Month bar
should show every month with data, defaulting to the current month.

> **Plotly version**: the template loads `plotly-2.35.2.min.js` from CDN and uses
> `Plotly.react`/`Plotly.purge` (both present in 2.x). No pip change needed for the view.

---

## What this adds

| Piece | Path |
|---|---|
| New Flask route | `/azure` (login-required) |
| Nav button on the Misha page | "Azure Usage →" (top-left of the header) |
| New module | `azure_dashboard.py` (builds Plotly figures from `usage.db`) |
| New template | `templates/azure.html` |
| New Python dep | `plotly` (added to `requirements.txt`) |
| New runtime data file | `usage.db` (SQLite — produced by `usage_monitor.py`) |

## Prerequisites

Before starting, confirm:

- The Azure service principal `image-text-medical-monitor` has
  `Cost Management Reader` **and** `Monitoring Reader` on the subscription.
  Validate locally:
  ```powershell
  cd D:\xwang\summer26\monitor
  .\.venv\Scripts\python.exe query_cost.py
  ```
  Expect HTTP 200 + the MTD table. If you see 403, fix roles before deploying.
- You have the four Azure env vars (`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_CLIENT_SECRET`, plus optional `MONTHLY_BUDGET_USD`). They live in
  `D:\xwang\summer26\monitor\.env` already.

## Step 1 — Commit the new code locally and push to GitHub

```powershell
cd D:\xwang\summer26\monitor\Yale_ClusterMonitor
git status                    # confirm: azure_dashboard.py, templates/azure.html,
                              # modified app.py, index.html, requirements.txt
git add azure_dashboard.py templates/azure.html templates/index.html app.py requirements.txt
git commit -m "Add Azure Usage dashboard at /azure"
git push origin main
```

`users.json` should NOT be in the commit — `.gitignore` already excludes it.

## Step 2 — Pull on the droplet, install plotly, restart

```bash
ssh root@159.223.173.141

sudo -u monitor bash -lc '
  cd ~/ClusterMonitor
  git pull --ff-only
  .venv/bin/pip install -r requirements.txt
'

systemctl restart misha-monitor
systemctl status misha-monitor --no-pager
```

At this point `/azure` will render an **error block** ("usage.db not found").
That's expected — the data file doesn't exist on the droplet yet. The Misha
page still works normally.

## Step 3 — Set up the Azure data pipeline on the droplet

The dashboard reads `usage.db`. You have two options for where that file
comes from:

### Option A (recommended): generate it on the droplet via cron

The SP works headlessly, so the simplest setup is to run `usage_monitor.py`
on the droplet every few hours.

```bash
# Still as root@droplet
sudo -u monitor bash -lc '
  cd ~
  # Pull the monitor script alongside the Flask app
  git clone https://github.com/<your-fork>/azure-usage-monitor.git ~/azure-usage-monitor
  cd ~/azure-usage-monitor

  # Reuse the Flask app venv (or make a fresh one)
  ~/ClusterMonitor/.venv/bin/pip install \
      azure-identity "azure-monitor-query<2.0" azure-mgmt-resource \
      python-dotenv requests plotly
'
```

> If `usage_monitor.py` is in the same repo as the Flask app (i.e. you
> committed it to `Yale_ClusterMonitor/`), skip the clone — it's already on
> the droplet under `~/ClusterMonitor/`.

Create `~/azure-usage-monitor/.env` (or `~/ClusterMonitor/.env.azure` if
co-located) with the SP creds. **Set permissions to `0600 monitor:monitor`**:

```bash
sudo -u monitor tee ~/azure-usage-monitor/.env >/dev/null <<'EOF'
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=a0d010f4-bdd9-45a8-af55-4be006857766
AZURE_CLIENT_SECRET=...
MONTHLY_BUDGET_USD=2000
EOF
sudo chmod 0600 /home/monitor/azure-usage-monitor/.env
sudo chown monitor:monitor /home/monitor/azure-usage-monitor/.env
```

First-time run to produce `usage.db`:

```bash
sudo -u monitor bash -lc '
  cd ~/azure-usage-monitor
  ~/ClusterMonitor/.venv/bin/python usage_monitor.py
'
# Expect: a JSON snapshot printed and ~/azure-usage-monitor/usage.db created.
```

Tell the Flask app where the DB is. Edit the systemd unit env:

```bash
EDITOR=nano systemctl edit misha-monitor
```

Add (inside the `[Service]` section the editor opens):

```ini
[Service]
Environment="AZURE_USAGE_DB=/home/monitor/azure-usage-monitor/usage.db"
```

Save, then:

```bash
systemctl daemon-reload
systemctl restart misha-monitor
```

Add a systemd timer (or cron) to refresh every 4 hours. Systemd timer is
cleaner — drop two files in `/etc/systemd/system/`:

```bash
cat >/etc/systemd/system/azure-usage-monitor.service <<'EOF'
[Unit]
Description=Refresh Azure usage snapshot
After=network-online.target

[Service]
Type=oneshot
User=monitor
WorkingDirectory=/home/monitor/azure-usage-monitor
ExecStart=/home/monitor/ClusterMonitor/.venv/bin/python usage_monitor.py
EOF

cat >/etc/systemd/system/azure-usage-monitor.timer <<'EOF'
[Unit]
Description=Run Azure usage refresh every 4 hours

[Timer]
OnBootSec=2min
OnUnitActiveSec=4h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now azure-usage-monitor.timer
systemctl list-timers azure-usage-monitor.timer     # confirm next-run time
```

### Option B: push usage.db from your laptop

Skip Step 3's Azure-side install. Instead, on your laptop, periodically:

```powershell
cd D:\xwang\summer26\monitor
.\.venv\Scripts\python.exe usage_monitor.py
scp usage.db monitor@159.223.173.141:/home/monitor/usage.db
```

Then on the droplet, point the app at it:

```bash
EDITOR=nano systemctl edit misha-monitor
# add: Environment="AZURE_USAGE_DB=/home/monitor/usage.db"
systemctl daemon-reload
systemctl restart misha-monitor
```

This is simpler but you have to remember to run it. Cost Management lags
8-24h anyway, so once-a-day is fine.

## Step 4 — Verify

1. Visit `https://mishamonitor.duckdns.org/` → log in → confirm the
   "Azure Usage →" button is in the top-left of the header.
2. Click it → `/azure` should show KPIs, the resource bar chart, and the
   per-resource dropdown.
3. Click "← Misha Monitor" → confirm it returns to `/`.
4. From the droplet:
   ```bash
   journalctl -u misha-monitor --since '5 min ago' | tail -20
   ls -la /home/monitor/azure-usage-monitor/usage.db   # check mtime
   systemctl list-timers azure-usage-monitor.timer     # next refresh
   ```

## Troubleshooting

### `/azure` shows "usage.db not found"

The `AZURE_USAGE_DB` env var doesn't point at a real file. Check:

```bash
systemctl show misha-monitor -p Environment | tr ' ' '\n' | grep AZURE
ls -la /home/monitor/azure-usage-monitor/usage.db
```

Fix the path or run `usage_monitor.py` manually once to populate it.

### `/azure` shows "Azure dashboard module not available"

`plotly` didn't install. Re-run the pip install:

```bash
sudo -u monitor /home/monitor/ClusterMonitor/.venv/bin/pip install plotly
systemctl restart misha-monitor
```

### Timer fires but `usage.db` doesn't update

```bash
journalctl -u azure-usage-monitor.service --since '1 hour ago'
```

Most common: `.env` is missing or has the wrong secret. Re-run by hand:

```bash
sudo -u monitor bash -lc 'cd ~/azure-usage-monitor && ~/ClusterMonitor/.venv/bin/python usage_monitor.py'
```

A 403 means the SP role grant rolled back — re-check Azure portal IAM.

### "MTD billed" is $0 or way too low

Cost Management data lags 8-24h. Right after the start of a new month,
expect almost nothing for the first day. Check the snapshot's
`generated_at` in the header — if it's >24h old the timer hasn't fired
(see above).

### Old client secret expired

Azure client secrets expire (default 6-24 months). When that happens the
timer will start failing with `AADSTS7000215`. Rotate per the steps in the
project's history — generate a new secret in the app registration, copy
the **Value** (not the ID) into the droplet's `.env`, restart the timer.

## Rollback

If something breaks the Misha view itself, revert just the Flask app
changes:

```bash
sudo -u monitor bash -lc '
  cd ~/ClusterMonitor
  git log --oneline -5      # find the commit before "Add Azure Usage"
  git revert <bad-commit>   # or: git reset --hard <previous-commit>
'
systemctl restart misha-monitor
```

The Misha page never imports `azure_dashboard.py` at module load (it's a
lazy import inside the `/azure` route), so plotly being broken or missing
cannot crash the Misha view. If the Misha view is broken, the bug is in
`app.py` or one of the templates — not the new module.
