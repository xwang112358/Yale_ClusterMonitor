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
import re
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
# Label _query_one_metric uses when a metric carries no deployment dimension.
AGGREGATE_DEPLOYMENT = "(all)"

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
    _migrate_metric_points_dedup(conn)
    return conn


def _migrate_metric_points_dedup(conn: sqlite3.Connection) -> None:
    """One-time: collapse minute-stamped metric_points rows into day buckets.

    Earlier versions of `_query_one_metric` stored `dp.timestamp` verbatim,
    which carries the query's wall-clock minute (e.g. T14:12:00) rather than
    the daily bucket start. Every 30-min re-query then wrote a new PRIMARY
    KEY row for the same logical day, accumulating hundreds of duplicates
    per (resource, metric, deployment, day) and inflating any aggregate
    SUM by ~500×. Collapse them now via MAX(value) — which for cumulative
    daily totals is the latest/most-complete reading — keyed to the
    day-start timestamp the new code writes.
    """
    dup_count = conn.execute(
        "SELECT COUNT(*) FROM metric_points "
        "WHERE timestamp NOT LIKE '%T00:00:00+00:00'"
    ).fetchone()[0]
    if dup_count == 0:
        return
    log.info("Migrating %d minute-stamped metric_points rows into day buckets",
             dup_count)
    conn.execute(
        "INSERT OR REPLACE INTO metric_points "
        "(timestamp, resource_name, metric_name, deployment, value) "
        "SELECT substr(timestamp, 1, 10) || 'T00:00:00+00:00', "
        "       resource_name, metric_name, deployment, MAX(value) "
        "FROM metric_points "
        "WHERE timestamp NOT LIKE '%T00:00:00+00:00' "
        "GROUP BY substr(timestamp, 1, 10), resource_name, metric_name, deployment"
    )
    deleted = conn.execute(
        "DELETE FROM metric_points WHERE timestamp NOT LIKE '%T00:00:00+00:00'"
    ).rowcount
    conn.commit()
    log.info("Migration: deleted %d duplicate rows", deleted)


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


# Azure AI Foundry does not bill third-party (Anthropic) models through the
# Cognitive Services account that hosts the deployment. It mints a Marketplace
# SaaS resource per model and bills there instead:
#   .../providers/Microsoft.SaaS/resources/claude-sonnet-4-<parent>-<uid>
# <parent> is the first 15 hex chars of the hosting account's
# properties.internalId (and <model> is clipped to 15 chars too, which is why
# e.g. 'claude-haiku-4-' arrives with a trailing dash). That prefix is the only
# link back to the account, so we resolve it and attribute the spend to the
# account -- otherwise Claude usage shows up as its own opaque row in the
# dashboard instead of stacking onto the resource that incurred it.
SAAS_TYPE_FRAGMENT = "/providers/microsoft.saas/resources/"
SAAS_RESOURCE_RE = re.compile(
    r"^(?P<model>.+)-(?P<parent>[0-9a-f]{15})-(?P<uid>[0-9a-f]{32})$", re.I)
ACCOUNTS_API_VERSION = "2023-05-01"
INTERNAL_ID_PREFIX_LEN = 15


def fetch_account_internal_ids(credential, subscription_id, accounts):
    """Map <first 15 hex of internalId> -> account name, for SaaS attribution.

    One ARM GET per Cognitive Services account. ARM is not rate-limited the way
    Cost Management is, so this is safe to do on every run.
    """
    token = credential.get_token("https://management.azure.com/.default").token
    headers = {"Authorization": f"Bearer {token}"}
    parents = {}
    for name, rid in accounts:
        low = (rid or "").lower()
        if "/providers/microsoft.cognitiveservices/accounts/" not in low:
            continue
        if "/projects/" in low:
            continue  # project children inherit their parent's internalId
        try:
            resp = requests.get(
                f"https://management.azure.com{rid}?api-version={ACCOUNTS_API_VERSION}",
                headers=headers, timeout=30)
        except requests.RequestException as e:
            log.warning("  internalId lookup failed for %s: %s", name, e)
            continue
        if resp.status_code != 200:
            log.warning("  internalId lookup for %s -> HTTP %s", name, resp.status_code)
            continue
        internal = ((resp.json().get("properties") or {}).get("internalId") or "")
        if len(internal) >= INTERNAL_ID_PREFIX_LEN:
            parents[internal[:INTERNAL_ID_PREFIX_LEN].lower()] = name
    return parents


def discover_deployments(credential, subscription_id, accounts):
    """Map account name -> [deployment names] via ARM.

    The SaaS resource name clips the model to 15 chars, so two deployments can
    collapse to the same token (claude-sonnet-4-5 and claude-sonnet-4-6 both
    become 'claude-sonnet-4'). The dashboard resolves the token against this
    roster: a unique prefix match names the model exactly, anything else stays
    deliberately unresolved rather than asserting a version we cannot prove.
    """
    token = credential.get_token("https://management.azure.com/.default").token
    headers = {"Authorization": f"Bearer {token}"}
    out = {}
    for name, rid in accounts:
        low = (rid or "").lower()
        if "/providers/microsoft.cognitiveservices/accounts/" not in low:
            continue
        if "/projects/" in low:
            continue
        try:
            resp = requests.get(
                f"https://management.azure.com{rid}/deployments"
                f"?api-version={ACCOUNTS_API_VERSION}", headers=headers, timeout=30)
        except requests.RequestException as e:
            log.warning("  deployment list failed for %s: %s", name, e)
            continue
        if resp.status_code != 200:
            log.warning("  deployment list for %s -> HTTP %s", name, resp.status_code)
            continue
        deps = []
        for d in resp.json().get("value", []):
            if not d.get("name"):
                continue
            # createdAt breaks ties when two deployments share the clipped
            # 15-char token (claude-sonnet-4-5 vs claude-sonnet-4-6): the
            # marketplace resource is minted with the deployment, so creation
            # order and first-billed order agree.
            deps.append({
                "name": d["name"],
                "created": (d.get("systemData") or {}).get("createdAt") or "",
                "model": ((d.get("properties") or {}).get("model") or {}).get("name") or "",
                "format": ((d.get("properties") or {}).get("model") or {}).get("format") or "",
            })
        if deps:
            out[name] = sorted(deps, key=lambda x: x["name"])
    return out


def saas_parent_name(resource_id, resource_name, parents):
    """Parent account name for a Marketplace SaaS billing row, else None."""
    if not parents or SAAS_TYPE_FRAGMENT not in (resource_id or "").lower():
        return None
    m = SAAS_RESOURCE_RE.match(resource_name or "")
    if not m:
        return None
    return parents.get(m.group("parent").lower())


def has_unattributed_saas_rows(conn):
    """True if any stored SaaS row still carries its raw marketplace name."""
    rows = conn.execute(
        "SELECT DISTINCT resource_name FROM billed_costs "
        "WHERE lower(resource_id) LIKE ?", ("%" + SAAS_TYPE_FRAGMENT + "%",)
    ).fetchall()
    return any(SAAS_RESOURCE_RE.match(r[0] or "") for r in rows)


def repair_saas_resource_names(conn, parents):
    """Re-attribute already-stored SaaS rows to their parent account.

    billed_costs keeps the full resource_id, so rows written before this mapping
    existed can be healed in place with no Cost Management call. Idempotent: once
    a row carries the account name it no longer matches SAAS_RESOURCE_RE.
    """
    if not parents:
        return 0
    rows = conn.execute(
        "SELECT DISTINCT resource_id, resource_name FROM billed_costs "
        "WHERE lower(resource_id) LIKE ?", ("%" + SAAS_TYPE_FRAGMENT + "%",)
    ).fetchall()
    fixed = 0
    for rid, rname in rows:
        parent = saas_parent_name(rid, rname, parents)
        if not parent or parent == rname:
            continue
        cur = conn.execute(
            "UPDATE billed_costs SET resource_name = ? WHERE resource_id = ?",
            (parent, rid))
        fixed += cur.rowcount
        log.info("  re-attributed %s -> %s (%d row(s))", rname, parent, cur.rowcount)
    conn.commit()
    return fixed


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
                          lookback_months: int = BILLING_LOOKBACK_MONTHS,
                          saas_parents=None):
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
        rname = rid.split("/")[-1] if rid else "(unknown)"
        # Foundry-hosted Anthropic models bill through their own SaaS resource;
        # attribute them to the account that hosts the deployment.
        rname = saas_parent_name(rid, rname, saas_parents) or rname
        rows_out.append({
            "usage_date": usage_date,
            "resource_id": rid,
            "resource_name": rname,
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
                        # Azure returns dp.timestamp with the query's wall-clock
                        # minute (e.g. 14:12:00) instead of snapping to the daily
                        # bucket start, even when granularity=days. Without this
                        # snap, every 30-min re-query writes a new PRIMARY KEY row
                        # for the same logical day, accumulating hundreds of
                        # duplicates per day and inflating any later aggregation.
                        day_start = dp.timestamp.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        out.append((day_start, dep, float(dp.total)))
        return out
    return None


def query_resource_metrics(client, resource_id, start, end):
    """Returns: list of (timestamp_dt, canonical_bucket, deployment, value).

    Resolves each canonical bucket (prompt_tokens / completion_tokens /
    total_tokens / calls) against the legacy and AIServices metric vocabularies.

    The two vocabularies are resolved PER DEPLOYMENT, not per account. A Foundry
    account routinely hosts both kinds at once: its OpenAI deployments report
    ProcessedPromptTokens/GeneratedTokens while its Anthropic (and other
    marketplace) deployments report only InputTokens/OutputTokens. Stopping at
    the first vocabulary that carried *any* data silently dropped every Claude
    token, because the legacy metric answered first for the GPT deployments --
    which is how a resource could bill $86 and estimate $9.

    Deployments are still claimed by the first vocabulary that reports them, so
    a deployment appearing under both (gpt-4o reports identical values to
    ProcessedPromptTokens and InputTokens) is never counted twice.
    """
    points = []
    for bucket, candidates in METRIC_BUCKETS:
        claimed = set()       # deployments already taken by an earlier vocabulary
        zero_fallback = None  # supported but all-zero, used only if nothing lands
        aggregate = None      # a candidate with NO deployment dimension at all
        for cand in candidates:
            res = _query_one_metric(client, resource_id, cand, start, end)
            if res is None:
                continue  # unsupported for this resource type — try next vocab
            with_data = {dep for _ts, dep, val in res if val}
            if with_data and with_data <= {AGGREGATE_DEPLOYMENT}:
                # An undifferentiated account total (TotalCalls reports one "(all)"
                # bucket; ModelRequests splits per deployment). The two count
                # overlapping traffic, so they must never be summed -- keep this
                # only if nothing else splits the bucket by deployment.
                if aggregate is None:
                    aggregate = res
                continue
            fresh = with_data - claimed - {AGGREGATE_DEPLOYMENT}
            if not fresh:
                if zero_fallback is None:
                    zero_fallback = res
                continue
            for ts, dep, val in res:
                if dep in fresh:
                    points.append((ts, bucket, dep, val))
            claimed |= fresh
        if not claimed:
            src = aggregate if aggregate is not None else zero_fallback
            if src is not None:
                for ts, dep, val in src:
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
    # Scan the full snapshot ring (cleanup bounds it at 1000 rows ≈ last ~3 weeks at
    # the 30-min cadence) so we can recover even resources removed >5 days ago.
    rows = conn.execute(
        "SELECT payload_json FROM snapshots ORDER BY snapshot_time DESC LIMIT 1000"
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
            # Skip recovered/frozen entries (any prior run that already used
            # this code path) — we want the original live-queried snapshot,
            # not a possibly-corrupted recovery of one.
            if entry.get("status") == "removed":
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

    # Resolve <internalId prefix> -> account name so Foundry Marketplace (Anthropic)
    # spend can be attributed to the account that hosts the deployment. Only needed
    # when we are about to write new billing rows, or when old rows still carry a
    # raw marketplace name -- keeps the 30-min metrics-only timer free of ARM calls.
    saas_parents = {}
    if query_cost or has_unattributed_saas_rows(conn):
        try:
            saas_parents = fetch_account_internal_ids(credential, SUBSCRIPTION_ID, accounts)
            log.info("  %d account prefix(es) for SaaS attribution", len(saas_parents))
        except Exception as e:  # never let attribution break the poll
            log.warning("Could not build SaaS parent map: %s", e)
    try:
        deployments_by_account = discover_deployments(credential, SUBSCRIPTION_ID, accounts)
        log.info("  deployment roster for %d account(s)", len(deployments_by_account))
    except Exception as e:  # roster is a display nicety; never fail the poll for it
        log.warning("Could not list deployments: %s", e)
        deployments_by_account = {}
    healed = repair_saas_resource_names(conn, saas_parents)
    if healed:
        log.info("Re-attributed %d cached SaaS billing row(s) to parent accounts", healed)

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
                                              lookback_months=lookback_months,
                                              saas_parents=saas_parents)
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
        # account -> [deployment names]; lets the dashboard resolve the SaaS
        # resource's clipped model token to the real model.
        "deployments_by_account": deployments_by_account,
        # <internalId prefix> -> account, so the dashboard can fold a marketplace
        # billing id onto its account even if billed_costs has not been repaired.
        "saas_parents": saas_parents,
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


def repair_names_only():
    """--repair-saas-names: re-attribute cached SaaS rows, no Cost Management call.

    Lets the fix land on existing history immediately instead of waiting for the
    next 4h full run (and without spending a Cost Management request).
    """
    credential = make_credential()
    conn = init_db(DB_PATH)
    accounts = discover_accounts(credential, SUBSCRIPTION_ID, RESOURCE_GROUP)
    parents = fetch_account_internal_ids(credential, SUBSCRIPTION_ID, accounts)
    log.info("Resolved %d account prefix(es)", len(parents))
    healed = repair_saas_resource_names(conn, parents)
    log.info("Re-attributed %d row(s)", healed)
    conn.close()


if __name__ == "__main__":
    # `--backfill [N]` pulls N months of billing history (default 13) in one run,
    # for a deep one-time fill of the past-month dashboard views. Routine runs use
    # the small rolling window (BILLING_LOOKBACK_MONTHS) to stay light on the
    # Cost Management rate limit.
    if "--repair-saas-names" in sys.argv:
        # One-shot maintenance: heal historical rows, then exit.
        repair_names_only()
        sys.exit(0)
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
