"""
Azure OpenAI usage monitor for the image-text-medical resource group.

Discovers ALL Microsoft.CognitiveServices/accounts resources in the resource
group, polls Azure Monitor metrics for each, and aggregates spending against
a configurable monthly budget.

Required env vars:
    AZURE_TENANT_ID
    AZURE_CLIENT_ID
    AZURE_CLIENT_SECRET

Optional env vars:
    MONTHLY_BUDGET_USD   (default: 2000)
    RATES_PATH           (default: ./rates.json)
    DB_PATH              (default: ./usage.db)

Pip install:
    pip install azure-identity azure-monitor-query azure-mgmt-resource python-dotenv

A local `.env` file in the working directory is auto-loaded.

Run once to test:
    python usage_monitor.py
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from azure.core.exceptions import HttpResponseError
from azure.identity import ClientSecretCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.monitor.query import MetricAggregationType, MetricsQueryClient
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SUBSCRIPTION_ID = "2c083276-c9d5-4db0-8dbb-a631bce1adfc"
RESOURCE_GROUP = "image-text-medical"

MONTHLY_BUDGET_USD = float(os.environ.get("MONTHLY_BUDGET_USD", "2000"))
RATES_PATH = Path(os.environ.get("RATES_PATH", "rates.json"))
DB_PATH = Path(os.environ.get("DB_PATH", "usage.db"))

# Azure exposes two different metric vocabularies for Cognitive Services:
#   - legacy OpenAI accounts use ProcessedPromptTokens / GeneratedTokens / TotalCalls
#   - newer AIServices accounts and accounts/projects children use
#     InputTokens / OutputTokens / ModelRequests (and reject the legacy names)
# We normalize both into canonical buckets so downstream code is vocab-agnostic.
# For each bucket the candidates are tried in order; the first SUPPORTED candidate
# that returns non-zero data wins (an all-zero supported metric is kept only as a
# fallback), so a resource that exposes both vocabularies is never double-counted.
METRIC_BUCKETS = [
    ("prompt_tokens",     ["ProcessedPromptTokens", "InputTokens"]),
    ("completion_tokens", ["GeneratedTokens", "OutputTokens"]),
    ("total_tokens",      ["TotalTokens"]),
    ("calls",             ["TotalCalls", "ModelRequests"]),
]

# How many months of billing to (re)fetch each run. Past months only need to be
# pulled until they stop changing; a small rolling window keeps the just-ended
# month complete after rollover. Use --backfill (or a larger env value) for a
# deep one-time history fill.
BILLING_LOOKBACK_MONTHS = int(os.environ.get("BILLING_LOOKBACK_MONTHS", "2"))

COST_MGMT_API_VERSION = "2023-11-01"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("msal").setLevel(logging.WARNING)
log = logging.getLogger("usage_monitor")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_points (
    timestamp     TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    metric_name   TEXT NOT NULL,
    deployment    TEXT NOT NULL,
    value         REAL NOT NULL,
    PRIMARY KEY (timestamp, resource_name, metric_name, deployment)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_time TEXT PRIMARY KEY,
    payload_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS billed_costs (
    usage_date    TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    meter         TEXT NOT NULL,
    cost_usd      REAL NOT NULL,
    currency      TEXT NOT NULL,
    PRIMARY KEY (usage_date, resource_id, meter)
);
"""


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Rates / cost estimation
# ---------------------------------------------------------------------------

DEFAULT_RATES = {
    "_comment": "USD per 1M tokens. See https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/",
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-35-turbo": {"input": 0.50, "output": 1.50},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-ada-002": {"input": 0.10, "output": 0.0},
    "_fallback": {"input": 1.00, "output": 3.00},
}


def load_rates(path: Path) -> dict:
    if not path.exists():
        log.warning("rates file not found at %s, writing defaults", path)
        path.write_text(json.dumps(DEFAULT_RATES, indent=2))
    return json.loads(path.read_text())


def match_rate(deployment: str, rates: dict) -> dict:
    """Longest-substring match so 'gpt-4o-mini' beats 'gpt-4'."""
    dep_lower = deployment.lower()
    for model in sorted((m for m in rates if not m.startswith("_")), key=len, reverse=True):
        if model in dep_lower:
            return rates[model]
    return rates["_fallback"]


def compute_cost(prompt_tokens: int, completion_tokens: int, rate: dict) -> float:
    return (
        prompt_tokens / 1_000_000 * rate["input"]
        + completion_tokens / 1_000_000 * rate["output"]
    )


# ---------------------------------------------------------------------------
# Azure clients
# ---------------------------------------------------------------------------


def make_credential():
    mode = os.environ.get("AZURE_AUTH_MODE", "service-principal").lower()
    tenant = os.environ.get("AZURE_TENANT_ID")

    if mode in ("interactive", "browser"):
        from azure.identity import InteractiveBrowserCredential
        log.info("Auth mode: interactive browser (a browser window will open)")
        return InteractiveBrowserCredential(tenant_id=tenant) if tenant else InteractiveBrowserCredential()

    if mode in ("device", "device-code", "devicecode"):
        from azure.identity import DeviceCodeCredential
        log.info("Auth mode: device code (paste code in browser)")
        return DeviceCodeCredential(tenant_id=tenant) if tenant else DeviceCodeCredential()

    for var in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
        if not os.environ.get(var):
            log.error("Missing required env var: %s", var)
            sys.exit(1)
    return ClientSecretCredential(
        tenant_id=os.environ["AZURE_TENANT_ID"],
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_secret=os.environ["AZURE_CLIENT_SECRET"],
    )


def discover_accounts(credential, subscription_id, resource_group):
    """List Cognitive Services accounts AND their project children in the RG.

    Returns [(name, resource_id), ...]. AIServices accounts (e.g. Azure AI
    Foundry) expose a Microsoft.CognitiveServices/accounts/projects child
    resource; those children carry their own metrics (InputTokens/OutputTokens/
    ModelRequests) and are easy to miss because they don't match the plain
    'accounts' type filter. We include them, labelled '<child> (project)'.
    """
    rm = ResourceManagementClient(credential, subscription_id)
    accounts, projects = [], []
    for r in rm.resources.list_by_resource_group(resource_group):
        rtype = (r.type or "").lower()
        if rtype == "microsoft.cognitiveservices/accounts":
            accounts.append((r.name, r.id))
        elif rtype == "microsoft.cognitiveservices/accounts/projects":
            # r.name is "parent/child"; surface the child clearly.
            child = r.name.split("/")[-1]
            projects.append((f"{child} (project)", r.id))
    return accounts + projects


def _lookback_start(months: int) -> date:
    """First day of the month `months - 1` calendar months before this month."""
    first_this = datetime.now(timezone.utc).date().replace(day=1)
    y, mo = first_this.year, first_this.month
    mo -= max(months - 1, 0)
    while mo <= 0:
        mo += 12
        y -= 1
    return date(y, mo, 1)


def query_cost_management(credential, subscription_id, resource_group,
                          lookback_months: int = BILLING_LOOKBACK_MONTHS):
    """Query Azure Cost Management for daily billed cost in the RG.

    Returns: list of dicts {usage_date, resource_id, resource_name, meter, cost_usd, currency}.
    Grouped by ResourceId + Meter so we can attribute spend to specific models.
    Spans a rolling window of `lookback_months` (current month + prior months) via
    a Custom timeframe so past-month history persists in `billed_costs`. Cost
    Management data typically lags 8-24 hours.
    """
    token = credential.get_token("https://management.azure.com/.default").token
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CostManagement/query?api-version={COST_MGMT_API_VERSION}"
    )
    start = _lookback_start(lookback_months)
    today = datetime.now(timezone.utc).date()
    # Cost Management rejects Custom timeframes longer than 1 year. Clamp the
    # window so a deep backfill never trips the "cannot exceed 1 year" 400.
    if (today - start).days > 360:
        start = today - timedelta(days=360)
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start.isoformat()}T00:00:00Z",
            "to": f"{today.isoformat()}T23:59:59Z",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "Meter"},
            ],
        },
    }

    # Cost Management has aggressive per-subscription rate limits (~15/5min).
    # Retry on 429 honoring Retry-After up to a few times before giving up.
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for attempt in range(4):
        resp = requests.post(url, headers=headers, json=body, timeout=60)
        if resp.status_code != 429:
            break
        wait = int(resp.headers.get("Retry-After", "20"))
        wait = min(wait, 60)  # cap; oneshot timer shouldn't block too long
        log.warning("Cost Management 429 (attempt %d/4); sleeping %ds", attempt + 1, wait)
        time.sleep(wait)
    if resp.status_code != 200:
        raise HttpResponseError(response=None, message=f"Cost Management {resp.status_code}: {resp.text}")
    payload = resp.json()
    props = payload.get("properties", {})
    columns = [c["name"] for c in props.get("columns", [])]
    idx = {name: columns.index(name) for name in columns}

    rows_out = []
    for row in props.get("rows", []):
        usage_date_raw = row[idx["UsageDate"]]
        usage_date = str(usage_date_raw)
        if usage_date.isdigit() and len(usage_date) == 8:
            usage_date = f"{usage_date[0:4]}-{usage_date[4:6]}-{usage_date[6:8]}"
        rid = row[idx["ResourceId"]] or ""
        rows_out.append({
            "usage_date": usage_date,
            "resource_id": rid,
            "resource_name": rid.split("/")[-1] if rid else "(unknown)",
            "meter": row[idx["Meter"]] if "Meter" in idx else "(no meter)",
            "cost_usd": float(row[idx["Cost"]]),
            "currency": row[idx["Currency"]] if "Currency" in idx else "USD",
        })

    if props.get("nextLink"):
        log.warning("Cost Management response was paginated; not all rows fetched. "
                    "Narrow the timeframe or contact maintainer.")
    return rows_out


def _metric_unsupported(msg: str) -> bool:
    """True if the error means this metric simply doesn't exist for the resource.

    Two distinct Azure phrasings: legacy accounts say the metric "does not
    support" a dimension/filter; the newer accounts/projects type says it
    "Failed to find metric configuration" for an unknown metric name.
    """
    return "does not support" in msg or "Failed to find metric" in msg


def _query_one_metric(client, resource_id, metric_name, start, end):
    """Query a single Azure metric. Returns [(ts, deployment, value), ...] if the
    metric exists for this resource, or None if the resource doesn't expose it.

    Tries the ModelDeploymentName dimension first (per-deployment breakdown),
    then falls back to no filter for metrics that don't support that dimension
    (e.g. TotalCalls / ModelRequests).
    """
    for filter_arg in ("ModelDeploymentName eq '*'", None):
        kwargs = dict(
            metric_names=[metric_name],
            timespan=(start, end),
            granularity=timedelta(days=1),
            aggregations=[MetricAggregationType.TOTAL],
        )
        if filter_arg:
            kwargs["filter"] = filter_arg
        try:
            response = client.query_resource(resource_id, **kwargs)
        except HttpResponseError as e:
            msg = (e.message or "") if hasattr(e, "message") else str(e)
            if filter_arg and "does not support" in msg:
                continue  # retry the same metric without the dimension filter
            if _metric_unsupported(msg):
                return None  # this resource type has no such metric — skip quietly
            log.warning("    %s: %s", metric_name, msg.splitlines()[0] if msg else e)
            return None

        out = []
        for metric in response.metrics:
            for ts in metric.timeseries:
                dep = "(all)"
                md = ts.metadata_values or {}
                if isinstance(md, dict):
                    for k, v in md.items():
                        if str(k).lower() == "modeldeploymentname":
                            dep = v
                            break
                else:
                    for entry in md:
                        if getattr(entry, "name", None) == "ModelDeploymentName":
                            dep = entry.value
                            break
                for dp in ts.data:
                    if dp.total is not None:
                        out.append((dp.timestamp, dep, float(dp.total)))
        return out
    return None


def query_resource_metrics(client, resource_id, start, end):
    """Returns: list of (timestamp_dt, canonical_bucket, deployment, value).

    Resolves each canonical bucket (prompt_tokens / completion_tokens /
    total_tokens / calls) against the legacy and AIServices metric vocabularies,
    picking the first supported candidate that carries data so resources exposing
    both vocabularies aren't double-counted.
    """
    points = []
    for bucket, candidates in METRIC_BUCKETS:
        best = None  # (ts, dep, value) list from the chosen candidate
        for cand in candidates:
            res = _query_one_metric(client, resource_id, cand, start, end)
            if res is None:
                continue  # unsupported for this resource type — try next vocab
            if any(v for _, _, v in res):
                best = res  # has real data — prefer it and stop
                break
            if best is None:
                best = res  # supported but all-zero — keep only as fallback
        if best is not None:
            for ts, dep, val in best:
                points.append((ts, bucket, dep, val))
    return points


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def build_resource_summary(resource_name: str, points: list, rates: dict) -> dict:
    """MTD totals per deployment for a single resource."""
    now = datetime.now(timezone.utc)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    breakdown = {}
    for ts, metric, dep, value in points:
        if ts < start_of_month:
            continue
        breakdown.setdefault(dep, {})
        breakdown[dep][metric] = breakdown[dep].get(metric, 0.0) + value

    deployments = []
    resource_cost = 0.0
    total_tokens = 0
    total_calls = 0

    for dep, m in sorted(breakdown.items()):
        prompt = int(m.get("prompt_tokens", 0))
        comp = int(m.get("completion_tokens", 0))
        rate = match_rate(dep, rates)
        cost = compute_cost(prompt, comp, rate)
        deployments.append({
            "deployment": dep,
            "prompt_tokens": prompt,
            "completion_tokens": comp,
            "total_tokens": int(m.get("total_tokens", 0)),
            "calls": int(m.get("calls", 0)),
            "estimated_cost_usd": round(cost, 2),
        })
        resource_cost += cost
        total_tokens += int(m.get("total_tokens", 0))
        total_calls += int(m.get("calls", 0))

    return {
        "resource": resource_name,
        "estimated_cost_usd": round(resource_cost, 2),
        "total_tokens": total_tokens,
        "calls": total_calls,
        "by_deployment": deployments,
    }


def _last_known_summary_for_removed(conn, resource_name: str):
    """Return the most recent snapshot's by_resource entry for a now-removed resource.

    Used as a fallback for resources that show up in this month's billed_costs
    but are no longer in RG discovery (deleted / moved out). The snapshot's
    cached entry captures the *live* MTD numbers as of the last successful
    discovery — accurate, single-counted, and unlike the persisted
    metric_points table not corrupted by overlapping 35-day query windows.
    Returns None if no past snapshot had this resource in the current month.
    """
    now = datetime.now(timezone.utc)
    current_ym = now.strftime("%Y-%m")
    rows = conn.execute(
        "SELECT payload_json FROM snapshots ORDER BY snapshot_time DESC LIMIT 200"
    ).fetchall()
    for (pj,) in rows:
        try:
            payload = json.loads(pj)
        except Exception:
            continue
        if not (payload.get("generated_at", "") or "").startswith(current_ym):
            continue
        for entry in payload.get("month_to_date", {}).get("by_resource", []):
            if entry.get("resource", "").lower() != resource_name.lower():
                continue
            if (entry.get("total_tokens") or 0) > 0 or (entry.get("calls") or 0) > 0:
                return entry
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(lookback_months: int = BILLING_LOOKBACK_MONTHS, query_cost: bool = True):
    log.info("Polling usage for resource group %s%s", RESOURCE_GROUP,
             "" if query_cost else " (metrics-only)")

    credential = make_credential()
    metrics_client = MetricsQueryClient(credential)
    conn = init_db(DB_PATH)
    rates = load_rates(RATES_PATH)

    try:
        accounts = discover_accounts(credential, SUBSCRIPTION_ID, RESOURCE_GROUP)
    except HttpResponseError as e:
        log.error("Could not list resources in '%s': %s", RESOURCE_GROUP, e.message or e)
        if e.status_code == 403:
            log.error(
                "403 Forbidden — confirm Monitoring Reader on the resource group "
                "has propagated to the service principal."
            )
        sys.exit(2)

    if not accounts:
        log.warning("No Cognitive Services accounts found in '%s'", RESOURCE_GROUP)
        sys.exit(0)

    log.info(
        "Discovered %d account(s): %s",
        len(accounts),
        [n for n, _ in accounts],
    )

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=35)

    resource_summaries = []
    estimated_total = 0.0

    for resource_name, resource_id in accounts:
        try:
            points = query_resource_metrics(metrics_client, resource_id, start, end)
        except HttpResponseError as e:
            log.warning("Skipping %s — metrics query failed: %s", resource_name, e.message or e)
            continue

        log.info("  %s: %d data points", resource_name, len(points))

        for ts, metric, dep, value in points:
            conn.execute(
                "INSERT OR REPLACE INTO metric_points "
                "(timestamp, resource_name, metric_name, deployment, value) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts.isoformat(), resource_name, metric, dep, value),
            )

        summary = build_resource_summary(resource_name, points, rates)
        resource_summaries.append(summary)
        estimated_total += summary["estimated_cost_usd"]

    # --- Cost Management: real billed dollars per resource per meter ---
    billed_total = 0.0
    billed_by_resource = {}   # name -> total cost
    billed_by_meter = {}      # (resource_name, meter) -> cost
    meter_totals = {}         # meter -> cost (across whole RG)
    billed_source = "live"    # "live" | "cache" — surfaced in the snapshot
    current_ym = end.strftime("%Y-%m")  # snapshot headline is current-month-only
    # Cost Management lowercases resource names in its ResourceIds, while
    # discovery/metrics preserve the created casing. Reconcile billing onto the
    # created casing so a resource isn't split into two snapshot rows
    # (e.g. belo2-yhf from billing vs BELO2-YHF from metrics).
    canonical = {name.lower(): name for name, _ in accounts}

    def _canon(n):
        return canonical.get(n.lower(), n)

    def _replay_cached_billing():
        """Aggregate the current month's cached billed_costs rows into the billing
        dicts. Used on a --metrics-only run and as the Cost Management 429 fallback,
        so the dashboard keeps showing the last-known billed $ instead of $0."""
        rows = conn.execute(
            "SELECT resource_name, meter, cost_usd FROM billed_costs "
            "WHERE usage_date >= date('now', 'start of month')"
        ).fetchall()
        log.info("  %d cached billed-cost rows from SQLite", len(rows))
        tot = 0.0
        for resource_name, meter, cost in rows:
            rname = _canon(resource_name)
            tot += cost
            billed_by_resource[rname] = billed_by_resource.get(rname, 0.0) + cost
            billed_by_meter[(rname, meter)] = billed_by_meter.get((rname, meter), 0.0) + cost
            meter_totals[meter] = meter_totals.get(meter, 0.0) + cost
        return tot

    if not query_cost:
        # Metrics-only run (the frequent 30-min timer): refresh tokens/estimate
        # without touching the rate-limited Cost Management API; billed $ comes
        # from the cache (the 4h full run keeps it fresh).
        log.info("Metrics-only run — skipping Cost Management; billed from cache")
        billed_source = "cache"
        billed_total += _replay_cached_billing()
    else:
        try:
            log.info("Querying Cost Management for billed cost (lookback %d month(s))...",
                     lookback_months)
            cost_rows = query_cost_management(credential, SUBSCRIPTION_ID, RESOURCE_GROUP,
                                              lookback_months=lookback_months)
            log.info("  %d billed-cost rows", len(cost_rows))
            for r in cost_rows:
                # Persist every row (all months) so past-month history accumulates...
                conn.execute(
                    "INSERT OR REPLACE INTO billed_costs "
                    "(usage_date, resource_id, resource_name, meter, cost_usd, currency) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (r["usage_date"], r["resource_id"], r["resource_name"],
                     r["meter"], r["cost_usd"], r["currency"]),
                )
                # ...but the snapshot headline only aggregates the current month.
                if r["usage_date"][:7] != current_ym:
                    continue
                rname = _canon(r["resource_name"])
                billed_total += r["cost_usd"]
                billed_by_resource[rname] = billed_by_resource.get(rname, 0.0) + r["cost_usd"]
                key = (rname, r["meter"])
                billed_by_meter[key] = billed_by_meter.get(key, 0.0) + r["cost_usd"]
                meter_totals[r["meter"]] = meter_totals.get(r["meter"], 0.0) + r["cost_usd"]
        except HttpResponseError as e:
            # Don't blow away the last-known-good snapshot. Replay the most recent
            # billed_costs rows for the current month from SQLite so the dashboard
            # keeps showing yesterday's reality instead of $0.
            log.warning("Cost Management query failed (%s) — falling back to cached billed_costs", e.message or e)
            billed_source = "cache"
            billed_total += _replay_cached_billing()

    # Merge billed cost into each resource summary; also include resources that
    # only show up in billing (e.g., no token metrics yet OR — for deleted /
    # moved-out resources — no longer in RG discovery). For the latter, replay
    # the persisted metric_points so the dashboard can still attribute the
    # pre-deletion token spend; otherwise the headline estimate undercounts
    # billed by exactly the missing-resource $.
    known_names_lc = {s["resource"].lower() for s in resource_summaries}
    for name in billed_by_resource:
        if name.lower() in known_names_lc:
            continue
        last = _last_known_summary_for_removed(conn, name)
        if last:
            # Freeze the last-known live summary in place; mark "removed" so the
            # dashboard can flag it.
            summary = {
                "resource": last.get("resource", name),
                "estimated_cost_usd": float(last.get("estimated_cost_usd", 0.0)),
                "total_tokens": int(last.get("total_tokens", 0)),
                "calls": int(last.get("calls", 0)),
                "by_deployment": last.get("by_deployment", []),
                "status": "removed",
            }
            log.info("Froze last-known live summary for removed resource %s "
                     "(est $%.2f, %d tokens, %d calls)",
                     name, summary["estimated_cost_usd"],
                     summary["total_tokens"], summary["calls"])
            resource_summaries.append(summary)
            estimated_total += summary["estimated_cost_usd"]
        else:
            resource_summaries.append({
                "resource": name,
                "estimated_cost_usd": 0.0,
                "total_tokens": 0,
                "calls": 0,
                "by_deployment": [],
            })
    for summary in resource_summaries:
        summary["billed_cost_usd"] = round(billed_by_resource.get(summary["resource"], 0.0), 2)
        summary["by_meter"] = sorted(
            [
                {"meter": meter, "cost_usd": round(cost, 4)}
                for (rname, meter), cost in billed_by_meter.items()
                if rname == summary["resource"]
            ],
            key=lambda m: m["cost_usd"],
            reverse=True,
        )

    conn.commit()

    estimated_total = round(estimated_total, 2)
    billed_total = round(billed_total, 2)
    # Headline % of budget is now driven by billed cost (estimate kept for diagnostic).
    pct = round((billed_total / MONTHLY_BUDGET_USD) * 100, 1) if MONTHLY_BUDGET_USD else 0.0

    snapshot = {
        "generated_at": end.isoformat(),
        "resource_group": RESOURCE_GROUP,
        "monthly_budget_usd": MONTHLY_BUDGET_USD,
        "month_to_date": {
            "billed_cost_usd": billed_total,
            "billed_source": billed_source,
            "estimated_cost_usd": estimated_total,
            "percent_of_budget": pct,
            "by_resource": sorted(
                resource_summaries,
                key=lambda r: r.get("billed_cost_usd", 0.0),
                reverse=True,
            ),
            "by_meter": sorted(
                [{"meter": m, "cost_usd": round(c, 2)} for m, c in meter_totals.items()],
                key=lambda x: x["cost_usd"],
                reverse=True,
            ),
        },
    }

    conn.execute(
        "INSERT OR REPLACE INTO snapshots (snapshot_time, payload_json) VALUES (?, ?)",
        (end.isoformat(), json.dumps(snapshot)),
    )
    # Bound the snapshots table — the metrics-only timer now writes one every ~30 min.
    conn.execute(
        "DELETE FROM snapshots WHERE snapshot_time NOT IN "
        "(SELECT snapshot_time FROM snapshots ORDER BY snapshot_time DESC LIMIT 1000)"
    )
    conn.commit()
    conn.close()

    log.info(
        "MTD billed: $%.2f / $%.2f budget (%.1f%%)  |  estimate from tokens: $%.2f  |  %d resource(s)",
        billed_total, MONTHLY_BUDGET_USD, pct, estimated_total, len(resource_summaries),
    )
    print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    # `--backfill [N]` pulls N months of billing history (default 13) in one run,
    # for a deep one-time fill of the past-month dashboard views. Routine runs use
    # the small rolling window (BILLING_LOOKBACK_MONTHS) to stay light on the
    # Cost Management rate limit.
    months = BILLING_LOOKBACK_MONTHS
    query_cost = True
    if "--metrics-only" in sys.argv:
        # Frequent (30-min) refresh of token metrics / estimate; skips the
        # rate-limited Cost Management query — billed $ comes from cache.
        query_cost = False
    if "--backfill" in sys.argv:
        i = sys.argv.index("--backfill")
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            months = int(sys.argv[i + 1])
        else:
            months = 12  # ~1 year; the query clamps the span to < 365 days
        log.info("Backfill mode: pulling %d months of billing history", months)
    main(lookback_months=months, query_cost=query_cost)
