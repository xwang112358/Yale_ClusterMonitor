"""
Render an interactive Azure usage dashboard from usage.db.

Produces dashboard.html — a self-contained page with interactive Plotly figures,
styled to match the dark theme used by Yale_ClusterMonitor.

New in this version
-------------------
* Month navigation: a Year button row and a Month button row let you read any
  past month that exists in `billed_costs`. The view defaults to the current
  month in US Eastern time and auto-advances when the clock rolls into a new
  month (the "refresh on the 1st" behavior) as long as you haven't manually
  navigated to a past month.
* Zero-cost / new resources are surfaced. A "Tracked resources" table lists
  every resource the monitor discovered — including ones with no billed spend
  yet (flagged NEW / IDLE) — so a freshly created resource is visible before it
  starts costing money, instead of silently dropping out of the billing charts.

Charts (per selected month):
  1. Stacked horizontal bar: billed $ per resource, segmented by model family.
  2. Daily billed line with on-budget burn marker.
  3. Cumulative line with the monthly budget reference.
  4. Per-resource daily stacked bar with a dropdown selector.

All per-month figures are pre-rendered server-side and embedded; switching months
is a client-side Plotly.react() so the page stays a single static file.
"""

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

DB_PATH = Path("usage.db")
OUT_PATH = Path("dashboard.html")

# --- ClusterMonitor palette (kept in sync with templates/index.html) ---
BG = "#0f1117"
CARD = "#1a1d27"
CARD_SOFT = "#20232f"
CARD_BORDER = "#2a2d3a"
TEXT = "#e0e0e0"
TEXT_DIM = "#888"
ACCENT = "#6c8cff"
GREEN = "#4caf50"
YELLOW = "#ff9800"
RED = "#f44336"

# Page and chart text scale. The HTML sets the same multiplier on the root
# font-size (all CSS sizes are rem), so this keeps Plotly's px fonts in step;
# change both together or the charts drift out of proportion with the page.
FONT_SCALE = 1.25


def fs(px):
    """Scale a Plotly font size by FONT_SCALE."""
    return int(round(px * FONT_SCALE))


FAMILY_COLORS = {
    "gpt-5.4":   "#7c3aed",
    "gpt-5.3":   "#9333ea",
    "gpt-5.2":   "#a855f7",
    "gpt-5":     "#c084fc",
    "gpt-4o":    "#2563eb",
    "gpt-4-turbo": "#3b82f6",
    "gpt-4":     "#60a5fa",
    "gpt-3.5":   "#0d9488",
    "o3-mini":   "#ea580c",
    "o3":        "#f97316",
    "o1-mini":   "#facc15",
    "o1":        "#eab308",
    # Foundry-hosted Anthropic models: one hue stepped dark->light by capability
    # tier, rose-shifted so it stays clear of the o-series oranges.
    "claude-opus-4.8":   "#802f35",
    "claude-opus-4.6":   "#9c414a",
    "claude-sonnet-5":   "#b9575d",
    "claude-sonnet-4.6": "#d17a72",
    "claude-sonnet-4.5": "#e5a396",
    "claude-haiku-4.5":  "#f5cbc0",
    "claude":    "#d97757",  # unknown / unresolved model
    "embed":     "#16a34a",
    "other":     "#64748b",
}

FAMILY_PATTERNS = [
    (re.compile(r"\bgpt[\s\-]?5\.4\b", re.I), "gpt-5.4"),
    (re.compile(r"\bgpt[\s\-]?5\.3\b", re.I), "gpt-5.3"),
    (re.compile(r"\bgpt[\s\-]?5\.2\b", re.I), "gpt-5.2"),
    (re.compile(r"\bgpt[\s\-]?5\b",    re.I), "gpt-5"),
    (re.compile(r"^\s*5\.4\b",         re.I), "gpt-5.4"),
    (re.compile(r"^\s*5\.3\b",         re.I), "gpt-5.3"),
    (re.compile(r"^\s*5\.2\b",         re.I), "gpt-5.2"),
    (re.compile(r"\bgpt[\s\-]?4o\b",   re.I), "gpt-4o"),
    (re.compile(r"\bgpt[\s\-]?4[\s\-]?turbo\b", re.I), "gpt-4-turbo"),
    (re.compile(r"\bgpt[\s\-]?4\b",    re.I), "gpt-4"),
    (re.compile(r"\bgpt[\s\-]?3\.?5\b", re.I), "gpt-3.5"),
    (re.compile(r"\bo3[\s\-]?mini\b",  re.I), "o3-mini"),
    (re.compile(r"\bo3\b",             re.I), "o3"),
    (re.compile(r"\bo1[\s\-]?mini\b",  re.I), "o1-mini"),
    (re.compile(r"\bo1\b",             re.I), "o1"),
    (re.compile(r"claude",  re.I),             "claude"),
    (re.compile(r"embed", re.I),               "embed"),
]

MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# Azure AI Foundry bills Anthropic models through a Marketplace SaaS resource
# named '<model>-<parent>-<uid>'; all of them share one generic meter, and
# <model> is clipped to 15 chars so claude-sonnet-4-5 and claude-sonnet-4-6 both
# arrive as 'claude-sonnet-4'. Resolve against the account's deployment roster
# and only name a version when the match is unambiguous. Kept in sync with
# azure_dashboard.py (this module stays standalone by design).
SAAS_MODEL_RE = re.compile(
    r"/providers/microsoft\.saas/resources/(?P<model>.+)-[0-9a-f]{15}-[0-9a-f]{32}$",
    re.I)
CLAUDE_METER_RE = re.compile(
    r"^Claude in Microsoft Foundry\s*\(([^)]+)\).*?([a-z0-9\-]+units)\s*$", re.I)
AMBIGUOUS = "…"


def saas_model_token(resource_id):
    m = SAAS_MODEL_RE.search(resource_id or "")
    return m.group("model").lower() if m else None


def _pretty_model(name):
    parts = name.lower().rstrip("-").rsplit("-", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0] + "-" + parts[1] + "." + parts[2]
    return name.lower().rstrip("-")


# A marketplace SaaS entity is a billing artefact, not a resource anyone manages:
# its spend belongs to the Foundry / Azure OpenAI account hosting the deployment.
# The pipeline already folds these at ingestion, but the roster must not be able to
# show one even if that has not run yet (a brand-new deployment, an un-repaired DB),
# so fold again here from the prefix map the snapshot carries.
MARKETPLACE_RE = re.compile(r"^.+-(?P<parent>[0-9a-f]{15})-[0-9a-f]{32}$", re.I)
UNATTRIBUTED = "marketplace (unattributed)"


def fold_marketplace_name(name, saas_parents):
    """Account name for a marketplace billing id, else a single labelled bucket.

    Never returns the raw id: a 64-char marketplace name in a roster of Foundry
    resources is noise, and bucketing keeps the money visible instead of hiding it.
    """
    m = MARKETPLACE_RE.match(name or "")
    if not m:
        return name
    return (saas_parents or {}).get(m.group("parent").lower()) or UNATTRIBUTED


def _deployment_records(entry):
    """Normalise a roster entry to [{'name','created'}]; tolerates the older
    plain-list-of-names snapshots."""
    out = []
    for d in entry or []:
        if isinstance(d, str):
            out.append({"name": d, "created": ""})
        elif isinstance(d, dict) and d.get("name"):
            out.append({"name": d["name"], "created": d.get("created") or ""})
    return out


def resolve_saas_labels(billed_rows, deployments_by_account):
    """{resource_id: label} for Foundry Marketplace rows, exact where provable.

    Nothing here is Anthropic-specific: any provider Foundry bills through a
    Marketplace SaaS resource (Anthropic, DeepSeek, Mistral, ...) is named
    '<model clipped to 15 chars>-<account prefix>-<uid>' and resolves the same
    way, against whatever deployments the owning account actually has.

    Three cases, in order:
      1. the clipped token prefix-matches exactly one deployment -> that model;
      2. it matches several (claude-sonnet-4-5 vs -4-6 both clip to
         'claude-sonnet-4'), and the colliding deployments and marketplace
         resources can be lined up 1:1 -- creation order matched against
         first-billed order -- so pair them in order. Both orderings must agree
         and be free of ties, otherwise we do not guess;
      3. anything else stays unresolved, marked with a trailing ellipsis.
    Returns (labels, inferred) where `inferred` holds the ids resolved by (2).
    """
    by_account = {a.lower(): _deployment_records(v)
                  for a, v in (deployments_by_account or {}).items()}

    # first-billed day per marketplace resource, and its clipped token
    first_seen, token_of, account_of = {}, {}, {}
    for usage_date, rname, _m, _c, rid in billed_rows:
        token = saas_model_token(rid)
        if not token:
            continue
        token_of[rid] = token
        account_of[rid] = (rname or "").lower()
        if rid not in first_seen or usage_date < first_seen[rid]:
            first_seen[rid] = usage_date

    labels, inferred = {}, set()
    # group colliding resources by (account, clipped token)
    groups = defaultdict(list)
    for rid, token in token_of.items():
        groups[(account_of[rid], token)].append(rid)

    for (account, token), rids in groups.items():
        cands = [d for d in by_account.get(account, [])
                 if d["name"].lower().startswith(token)]
        if len(cands) == 1:
            for rid in rids:
                labels[rid] = _pretty_model(cands[0]["name"])
            continue
        # Pair 1:1 only when both orderings are complete and unambiguous.
        created = [c.get("created") or "" for c in cands]
        billed = [first_seen.get(r, "") for r in rids]
        if (len(cands) == len(rids) > 1
                and all(created) and len(set(created)) == len(created)
                and all(billed) and len(set(billed)) == len(billed)):
            for c, rid in zip(sorted(cands, key=lambda x: x["created"]),
                              sorted(rids, key=lambda r: first_seen[r])):
                labels[rid] = _pretty_model(c["name"])
                inferred.add(rid)
        else:
            for rid in rids:
                labels[rid] = _pretty_model(token) + (AMBIGUOUS if cands else "")
    return labels, inferred


def short_meter(meter):
    """Trim Foundry's very long Anthropic meter so tooltips stay readable."""
    if not meter:
        return "(no meter)"
    m = CLAUDE_METER_RE.match(meter)
    return f"Claude · {m.group(1)} · {m.group(2)}" if m else meter


def _meter_lines(pairs):
    """Tooltip lines for (meter, cost) pairs, identical meters summed.

    Foundry bills every Claude model under one meter, so a month's rows repeat
    the same meter once per day; collapse them instead of listing duplicates.
    """
    agg = defaultdict(float)
    for m, c in pairs:
        agg[short_meter(m)] += c
    rows = sorted(agg.items(), key=lambda mc: mc[1], reverse=True)
    return "<br>".join(f"  · {m}  <b>${c:,.2f}</b>" for m, c in rows) or "  (no meters)"


def family_color(fam):
    if fam in FAMILY_COLORS:
        return FAMILY_COLORS[fam]
    base = fam.rstrip(AMBIGUOUS)
    kin = sorted(k for k in FAMILY_COLORS if k.startswith(base) and k != "claude")
    if kin:
        return FAMILY_COLORS[kin[0]]
    return FAMILY_COLORS["claude" if fam.startswith("claude") else "other"]


def model_family(meter: str) -> str:
    if not meter:
        return "other"
    for pat, name in FAMILY_PATTERNS:
        if pat.search(meter):
            return name
    return "other"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load(conn):
    snap_row = conn.execute(
        "SELECT payload_json FROM snapshots ORDER BY snapshot_time DESC LIMIT 1"
    ).fetchone()
    if not snap_row:
        raise SystemExit("No snapshots — run usage_monitor.py first.")
    snapshot = json.loads(snap_row[0])

    # ALL billed rows (every month we have), newest data wins via the monitor.
    billed_rows = conn.execute(
        """
        SELECT usage_date, resource_name, meter, cost_usd, resource_id
        FROM billed_costs
        ORDER BY usage_date
        """
    ).fetchall()
    return snapshot, billed_rows


def group_by_month(billed_rows):
    """Return {"YYYY-MM": [(usage_date, resource_name, meter, cost), ...]}."""
    months = defaultdict(list)
    for row in billed_rows:
        usage_date = row[0]
        ym = usage_date[:7]
        months[ym].append(row)
    return months


# ---------------------------------------------------------------------------
# Figure builders (operate on one month's rows)
# ---------------------------------------------------------------------------


def _base_layout(extra):
    base = dict(
        template="plotly_dark",
        paper_bgcolor=CARD,
        plot_bgcolor=CARD,
        font=dict(family="SF Mono, Cascadia Code, Consolas, monospace",
                  color=TEXT, size=fs(12)),
        margin=dict(l=10, r=20, t=10, b=40),
        hoverlabel=dict(bgcolor=CARD_SOFT, bordercolor=CARD_BORDER,
                        font=dict(family="SF Mono, monospace", color=TEXT)),
    )
    base.update(extra)
    return base


def stacked_bar_figure(billed_rows, saas_labels=None):
    """One trace per model family; each trace has one bar per resource.

    Resources whose billing nets to $0 for the month (e.g. offset by a credit)
    get an open-circle marker so they remain visible on the axis.
    """
    res_family_cost = defaultdict(lambda: defaultdict(float))
    res_family_meters = defaultdict(lambda: defaultdict(list))
    saas_labels = saas_labels or {}
    for usage_date, resource_name, meter, cost, resource_id in billed_rows:
        fam = saas_labels.get(resource_id) or model_family(meter)
        res_family_cost[resource_name][fam] += cost
        res_family_meters[resource_name][fam].append((meter, cost))

    if not res_family_cost:
        return None

    resources_sorted = sorted(
        res_family_cost.keys(),
        key=lambda r: sum(res_family_cost[r].values()),
    )
    family_totals = defaultdict(float)
    for r in res_family_cost:
        for f, v in res_family_cost[r].items():
            family_totals[f] += v
    # Families with no spend this month would add legend entries with no visible
    # segment (zero-spend resources are shown by the open-circle marker instead).
    families_sorted = sorted((f for f, v in family_totals.items() if v > 0),
                             key=lambda f: family_totals[f], reverse=True)

    fig = go.Figure()
    for fam in families_sorted:
        xs, hovers = [], []
        for r in resources_sorted:
            total = res_family_cost[r].get(fam, 0.0)
            xs.append(total)
            meter_lines = _meter_lines(res_family_meters[r].get(fam, []))
            hovers.append(
                f"<b style='font-size:13px;'>{r}</b><br>"
                f"<span style='color:#6c8cff;'>model: <b>{fam}</b></span>  ·  "
                f"<b>${total:,.2f}</b><br>"
                f"<span style='color:#888;'>billed meters:</span><br>{meter_lines}"
            )
        fig.add_trace(go.Bar(
            name=fam, y=resources_sorted, x=xs, orientation="h",
            marker=dict(color=family_color(fam),
                        line=dict(color=BG, width=0.5)),
            hovertemplate="%{customdata}<extra></extra>", customdata=hovers,
        ))

    # Mark zero-spend resources so the eye can find them on the axis.
    zero_names = [r for r in resources_sorted
                  if sum(res_family_cost[r].values()) == 0.0]
    if zero_names:
        fig.add_trace(go.Scatter(
            x=[0] * len(zero_names), y=zero_names, mode="markers",
            marker=dict(symbol="circle-open", size=9, color=TEXT_DIM,
                        line=dict(width=1.5, color=TEXT_DIM)),
            name="no billed spend", hoverinfo="text",
            hovertext=[f"<b>{r}</b><br><span style='color:#888;'>no billed spend this month</span>"
                       for r in zero_names],
            showlegend=True,
        ))

    fig.update_layout(**_base_layout(dict(
        barmode="stack",
        xaxis=dict(title="Billed $ (USD)", tickprefix="$", tickformat=",.0f",
                   gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        yaxis=dict(gridcolor=CARD_BORDER, automargin=True),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right",
                    x=1, bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT, size=fs(11))),
        height=max(360, 34 * len(resources_sorted) + 90),
    )))
    return fig


def daily_and_cumulative_figure(daily, budget):
    """Lab-wide daily spend (bars) + running month total (line) on one $ axis.

    Both series are USD and the line is the running sum of the bars, so a second
    y-scale would only let them be drawn at arbitrary relative heights. The
    budget is an order of magnitude above a normal month, so it is drawn only
    once actually crossed; until then the pace and burn rate live in the
    subtitle.
    """
    if not daily:
        return None
    days = [r[0] for r in daily]
    costs = [r[1] or 0.0 for r in daily]
    cum, running = [], 0.0
    for c in costs:
        running += c
        cum.append(running)

    total = cum[-1] if cum else 0.0
    burn = budget / 30.0 if budget else 0.0
    pct = (total / budget * 100.0) if budget else 0.0
    show_budget = bool(budget) and total >= budget

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=days, y=costs, name="daily",
        marker=dict(color=GREEN, line=dict(color=BG, width=0.5)),
        hovertemplate="<b>%{x}</b><br>Daily: $%{y:,.2f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=days, y=cum, name="cumulative", mode="lines+markers",
        line=dict(color=ACCENT, width=2),
        marker=dict(size=7, color=ACCENT, line=dict(color=BG, width=1)),
        hovertemplate="<b>%{x}</b><br>Cumulative: $%{y:,.2f}<extra></extra>",
    ))
    if show_budget:
        fig.add_hline(y=budget, line=dict(color=RED, dash="dash", width=1.5),
                      annotation_text=f"monthly budget ${budget:,.0f}",
                      annotation_position="top left",
                      annotation_font=dict(color=RED, size=fs(10)))
        crossed = next((i for i, v in enumerate(cum) if v >= budget), None)
        if crossed is not None:
            fig.add_trace(go.Scatter(
                x=[days[crossed]], y=[cum[crossed]], mode="markers",
                marker=dict(size=12, color=RED, symbol="x-thin",
                            line=dict(width=2, color=RED)),
                hovertemplate=(f"<b>Budget crossed</b><br>{days[crossed]}: "
                               f"${cum[crossed]:,.2f}<extra></extra>"),
                showlegend=False,
            ))

    sub = f"month to date ${total:,.2f}"
    if budget:
        sub += (f" · {pct:.1f}% of ${budget:,.0f} budget"
                f" · on-budget burn ${burn:,.0f}/day")
    fig.update_layout(**_base_layout(dict(
        barmode="overlay",
        xaxis=dict(gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        yaxis=dict(title="USD", tickprefix="$", tickformat=",.0f",
                   gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER, rangemode="tozero"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT, size=fs(11))),
        height=360, margin=dict(t=64),
        annotations=[dict(text=sub, x=0, y=1.13, xref="paper", yref="paper",
                          showarrow=False, font=dict(color=TEXT_DIM, size=fs(11)),
                          xanchor="left")],
    )))
    return fig


def per_resource_daily_figure(billed_rows, saas_labels=None):
    by_res = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    detail = defaultdict(lambda: defaultdict(list))
    all_days = set()
    saas_labels = saas_labels or {}
    for usage_date, resource_name, meter, cost, resource_id in billed_rows:
        fam = saas_labels.get(resource_id) or model_family(meter)
        by_res[resource_name][usage_date][fam] += cost
        detail[resource_name][(usage_date, fam)].append((meter, cost))
        all_days.add(usage_date)
    if not by_res:
        return None

    days_sorted = sorted(all_days)
    resources_sorted = sorted(by_res.keys(),
                              key=lambda r: sum(sum(d.values()) for d in by_res[r].values()),
                              reverse=True)
    gfam = defaultdict(float)
    for r in by_res:
        for d in by_res[r]:
            for f, v in by_res[r][d].items():
                gfam[f] += v
    families_sorted = sorted(gfam.keys(), key=lambda f: gfam[f], reverse=True)

    fig = go.Figure()
    trace_resource = []
    for r in resources_sorted:
        for fam in families_sorted:
            ys = [by_res[r][d].get(fam, 0.0) for d in days_sorted]
            if all(v == 0.0 for v in ys):
                continue
            hovers = []
            for d in days_sorted:
                v = by_res[r][d].get(fam, 0.0)
                meter_lines = _meter_lines(detail[r].get((d, fam), []))
                hovers.append(
                    f"<b>{r}</b>  ·  {d}<br>"
                    f"<span style='color:#6c8cff;'>model: <b>{fam}</b></span>  ·  <b>${v:,.2f}</b><br>"
                    f"<span style='color:#888;'>billed meters:</span><br>{meter_lines}"
                )
            fig.add_trace(go.Bar(
                x=days_sorted, y=ys, name=fam,
                marker=dict(color=family_color(fam),
                            line=dict(color=BG, width=0.3)),
                visible=(r == resources_sorted[0]),
                hovertemplate="%{customdata}<extra></extra>", customdata=hovers,
                legendgroup=fam,
            ))
            trace_resource.append(r)
        daily_totals = [sum(by_res[r][d].values()) for d in days_sorted]
        cum, running = [], 0.0
        for v in daily_totals:
            running += v
            cum.append(running)
        fig.add_trace(go.Scatter(
            x=days_sorted, y=cum, name="cumulative", mode="lines+markers",
            line=dict(color=TEXT, width=2, dash="dot"),
            marker=dict(size=6, color=TEXT, line=dict(color=BG, width=1)),
            yaxis="y2", visible=(r == resources_sorted[0]),
            hovertemplate=f"<b>{r}</b>  ·  %{{x}}<br><span style='color:#888;'>cumulative</span>  ·  <b>$%{{y:,.2f}}</b><extra></extra>",
            legendgroup="cumulative",
        ))
        trace_resource.append(r)

    buttons = []
    for r in resources_sorted:
        total = sum(sum(d.values()) for d in by_res[r].values())
        visible = [tr == r for tr in trace_resource]
        buttons.append(dict(
            label=f"{r}  —  ${total:,.0f}", method="update",
            args=[{"visible": visible},
                  {"annotations": [dict(text=f"<b>{r}</b>  ·  ${total:,.2f}",
                                        showarrow=False, x=0, y=1.18, xref="paper",
                                        yref="paper", font=dict(color=TEXT, size=fs(13)),
                                        align="left", xanchor="left")]}],
        ))
    first_r = resources_sorted[0]
    first_total = sum(sum(d.values()) for d in by_res[first_r].values())
    fig.update_layout(**_base_layout(dict(
        barmode="stack",
        margin=dict(l=10, r=70, t=90, b=40),
        xaxis=dict(gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER, domain=[0, 1]),
        yaxis=dict(title="Daily billed $", tickprefix="$", tickformat=",.0f",
                   gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        yaxis2=dict(title="Cumulative $", tickprefix="$", tickformat=",.0f",
                    overlaying="y", side="right", showgrid=False,
                    zerolinecolor=CARD_BORDER, color=TEXT_DIM),
        showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center",
                    x=0.5, bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT, size=fs(11))),
        height=420,
        updatemenus=[dict(buttons=buttons, direction="down", x=1, xanchor="right",
                          y=1.22, yanchor="top", bgcolor=CARD_SOFT,
                          bordercolor=CARD_BORDER, font=dict(color=TEXT, size=fs(11)),
                          showactive=True, pad=dict(l=8, r=8, t=4, b=4))],
        annotations=[dict(text=f"<b>{first_r}</b>  ·  ${first_total:,.2f}",
                          showarrow=False, x=0, y=1.18, xref="paper", yref="paper",
                          font=dict(color=TEXT, size=fs(13)), align="left", xanchor="left")],
    )))
    return fig


# ---------------------------------------------------------------------------
# Per-month payload
# ---------------------------------------------------------------------------


def fig_json(fig):
    return json.loads(fig.to_json()) if fig is not None else None


def build_month_payload(ym, rows, budget, canonical=None, saas_labels=None):
    """Per-month billing payload: KPIs, figures, and a billed-by-resource table.

    The 'live roster' (all currently discovered resources, including idle/new
    ones with no spend) is embedded separately and merged client-side onto
    whichever month is current in US Eastern time, so surfacing new resources is
    decoupled from the UTC vs ET month boundary.

    `canonical` maps lower-cased name -> created casing. Cost Management
    lowercases resource names while discovery/metrics keep the created casing;
    folding billing onto the created casing stops a resource splitting into two
    rows (e.g. belo2-yhf vs BELO2-YHF).
    """
    if canonical:
        rows = [(d, canonical.get(rn.lower(), rn), m, c, rid)
                for (d, rn, m, c, rid) in rows]
    daily_map = defaultdict(float)
    res_billed = defaultdict(float)
    for usage_date, rn, _m, cost, _rid in rows:
        daily_map[usage_date] += cost
        res_billed[rn] += cost
    daily = sorted(daily_map.items())

    billed = round(sum(c for _, c in daily), 2)
    pct = round((billed / budget) * 100, 1) if budget else 0.0

    resources = [{"name": n, "billed": round(b, 2), "idle": round(b, 2) == 0.0}
                 for n, b in res_billed.items()]
    resources.sort(key=lambda r: r["billed"], reverse=True)

    figs = {
        "stacked": fig_json(stacked_bar_figure(rows, saas_labels)),
        "daily_cumulative": fig_json(daily_and_cumulative_figure(daily, budget)),
        "per_resource": fig_json(per_resource_daily_figure(rows, saas_labels)),
    }
    return {
        "billed": billed,
        "pct": pct,
        "over": billed > budget,
        "figs": figs,
        "resources": resources,
    }


def _model_rows(entry):
    """Per-model activity for a resource, biggest first.

    Drops the "(all)" pseudo-deployment (an account total, not a model) and
    anything with no activity, so the table lists models that actually ran.
    """
    out = []
    for d in entry.get("by_deployment", []) or []:
        name = d.get("deployment") or ""
        if name == "(all)" or not name:
            continue
        tokens, calls = d.get("total_tokens") or 0, d.get("calls") or 0
        est = d.get("estimated_cost_usd") or 0
        if not (tokens or calls or est):
            continue
        out.append({"name": name, "tokens": tokens, "calls": calls, "est": est})
    out.sort(key=lambda m: (m["est"], m["tokens"]), reverse=True)
    return out


def _is_inactive_project(entry):
    """True for an accounts/projects child with nothing to report."""
    if not str(entry.get("resource", "")).endswith(" (project)"):
        return False
    return not any((entry.get("total_tokens") or 0,
                    entry.get("calls") or 0,
                    entry.get("estimated_cost_usd") or 0))


def build_roster(snapshot):
    """Current full resource roster from the latest snapshot — every discovered
    resource with its MTD token/call activity. Merged onto the live month."""
    mtd = snapshot.get("month_to_date", {})
    saas_parents = snapshot.get("saas_parents", {})
    roster, seen = [], {}
    for r in mtd.get("by_resource", []):
        if _is_inactive_project(r):
            continue
        name = fold_marketplace_name(r["resource"], saas_parents)
        row = seen.get(name)
        if row is None:
            row = {"name": name, "est": r.get("estimated_cost_usd"),
                   "tokens": r.get("total_tokens"), "calls": r.get("calls"),
                   "models": _model_rows(r)}
            seen[name] = row
            roster.append(row)
            continue
        for k, src_key in (("est", "estimated_cost_usd"), ("tokens", "total_tokens"),
                           ("calls", "calls")):
            if r.get(src_key) is not None:
                row[k] = (row.get(k) or 0) + (r.get(src_key) or 0)
        row["models"] = sorted(row.get("models", []) + _model_rows(r),
                               key=lambda m: (m["est"], m["tokens"]), reverse=True)
    return roster, mtd.get("estimated_cost_usd")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Azure Usage — __RG__</title>
<style>
  :root {
    --bg: __BG__; --card: __CARD__; --card-soft: __CARD_SOFT__;
    --card-border: __CARD_BORDER__; --text: __TEXT__; --text-dim: __TEXT_DIM__;
    --accent: __ACCENT__; --green: __GREEN__; --yellow: __YELLOW__; --red: __RED__;
  }
  /* Page text scale. Every size below is in rem, so this one knob scales all of
     them. Keep it in step with FONT_SCALE in azure_dashboard.py / dashboard.py,
     which applies the same multiplier to Plotly's px font sizes. */
  html { font-size: 125%; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'SF Mono','Cascadia Code','Consolas',monospace;
         background: var(--bg); color: var(--text); padding: 20px; min-height: 100vh; }
  header { display: flex; justify-content: space-between; align-items: baseline;
           margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid var(--card-border);
           gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 1.15rem; font-weight: 600; letter-spacing: 0.5px; }
  header h1 .rg { color: var(--text-dim); font-weight: 400; font-size: 0.85rem; margin-left: 8px; }
  header .meta { font-size: 0.72rem; color: var(--text-dim); }

  /* Month navigation */
  .navbar { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
  .navrow { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .navrow .navlabel { font-size: 0.66rem; color: var(--text-dim); text-transform: uppercase;
                      letter-spacing: 1.2px; width: 58px; }
  .btn { background: var(--card); border: 1px solid var(--card-border); color: var(--text);
         border-radius: 6px; padding: 5px 12px; font-family: inherit; font-size: 0.78rem;
         cursor: pointer; transition: all 0.12s; }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn.active { background: var(--accent); border-color: var(--accent); color: #0b0d13; font-weight: 600; }
  .btn:disabled { opacity: 0.32; cursor: default; }
  .btn .dot { color: var(--green); font-size: 0.7rem; margin-left: 5px; }
  .live-pill { font-size: 0.62rem; color: var(--green); border: 1px solid var(--green);
               border-radius: 10px; padding: 1px 8px; margin-left: 8px; letter-spacing: 0.5px; }

  .kpi-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr));
               gap: 10px; margin-bottom: 18px; }
  .kpi { background: var(--card); border: 1px solid var(--card-border); border-radius: 8px; padding: 10px 14px; }
  .kpi .label { font-size: 0.68rem; color: var(--text-dim); text-transform: uppercase;
                letter-spacing: 1.2px; margin-bottom: 4px; }
  .kpi .value { font-size: 1.35rem; font-weight: 600; }
  .kpi .sub { font-size: 0.7rem; color: var(--text-dim); margin-top: 3px; }
  .kpi .value.over { color: var(--red); }
  .kpi .value.ok { color: var(--green); }

  .section-title { font-size: 0.72rem; color: var(--text-dim); text-transform: uppercase;
                   letter-spacing: 1.2px; margin: 22px 0 10px; display: flex;
                   align-items: baseline; justify-content: space-between; }
  .section-title .hint { font-size: 0.68rem; color: var(--text-dim); text-transform: none; letter-spacing: 0.4px; }

  .panel { background: var(--card); border: 1px solid var(--card-border); border-radius: 8px; padding: 10px; }
  .panel-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media (max-width: 1000px) { .panel-row { grid-template-columns: 1fr; } }

  table.res { width: 100%; border-collapse: collapse; font-size: 0.76rem; }
  table.res th, table.res td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--card-border); }
  table.res th:first-child, table.res td:first-child { text-align: left; }
  table.res th { color: var(--text-dim); font-weight: 500; text-transform: uppercase;
                 font-size: 0.62rem; letter-spacing: 1px; }
  table.res tr:hover td { background: var(--card-soft); }
  tr.res-row.has-models { cursor: pointer; }
  tr.res-row.has-models:hover td { background: var(--card-soft); }
  .caret { display: inline-block; margin-left: 10px; color: var(--text-dim);
           transition: transform 0.12s ease; }
  tr.res-row.open .caret { transform: rotate(90deg); }
  .mcount { color: var(--text-dim); font-size: 0.62rem; margin-left: 6px; }
  tr.model-row td { color: var(--text-dim); font-size: 0.7rem; border-top: none; }
  tr.model-row .model-name { padding-left: 26px; }
  .badge { font-size: 0.58rem; padding: 1px 6px; border-radius: 8px; letter-spacing: 0.6px; margin-left: 8px; }
  .badge.new { color: var(--yellow); border: 1px solid var(--yellow); }
  .badge.idle { color: var(--text-dim); border: 1px solid var(--card-border); }
  .dim { color: var(--text-dim); }

  .notes { background: var(--card); border: 1px solid var(--card-border); border-left: 3px solid var(--accent);
           border-radius: 6px; padding: 12px 16px; margin-bottom: 18px; font-size: 0.78rem;
           line-height: 1.55; color: var(--text); }
  .notes-title { font-size: 0.66rem; color: var(--accent); text-transform: uppercase;
                 letter-spacing: 1.4px; margin-bottom: 6px; font-weight: 600; }
  .notes ul { list-style: none; padding: 0; margin: 0; }
  .notes li { padding: 3px 0; }
  .notes li::before { content: "›"; color: var(--accent); margin-right: 8px; font-weight: 600; }
  .notes b { color: var(--text); }
  .notes a { color: var(--accent); text-decoration: none; border-bottom: 1px dotted var(--accent); }

  .empty { padding: 28px 14px; color: var(--text-dim); font-size: 0.82rem; text-align: center; }
  .plotly-host { width: 100%; }
  .footer { margin-top: 28px; padding-top: 14px; border-top: 1px solid var(--card-border);
            color: var(--text-dim); font-size: 0.7rem; display: flex; justify-content: space-between; }
</style>
<script>__PLOTLYJS__</script>
</head>
<body>

<header>
  <h1>Azure Usage<span class="rg">__RG__</span></h1>
  <span class="meta">snapshot: __GENERATED_AT__</span>
</header>

<div class="navbar">
  <div class="navrow"><span class="navlabel">Year</span><span id="year-bar"></span></div>
  <div class="navrow"><span class="navlabel">Month</span><span id="month-bar"></span>
       <span id="live-pill" class="live-pill" style="display:none;">● VIEWING CURRENT MONTH</span></div>
</div>

<div class="notes">
  <div class="notes-title">Notes</div>
  <ul>
    <li><b>Month view:</b> the dashboard opens on the <b>current month (US Eastern)</b> and rolls over
        automatically on the 1st. Use the Year / Month buttons to read any past month we have data for.</li>
    <li><b>Data latency:</b> numbers come from Azure Cost Management and lag the real world by roughly
        <b>8–24 hours</b>. A freshly created resource shows up under <b>Tracked resources</b> (flagged NEW)
        before it has any billed spend — don't panic if it reads $0.</li>
    <li><b>Who to contact:</b> ping <a href="mailto:allen.wang.xw532@yale.edu">Allen Wang</a> or
        <a href="mailto:hyunjae.kim@yale.edu">Hyunjae Kim</a> about unexpected spend, a new deployment/key,
        a budget bump, or a 429/quota error.</li>
    <li><b>Billed vs estimated:</b> "Billed" is the authoritative invoiced figure from Cost
        Management, but it <b>lags actual token usage by ~8&ndash;24h</b> &mdash; so early in the
        month, or right after heavy use, it under-reports. The <b>token-based estimate</b> refreshes
        every <b style="color:var(--red)">~30&nbsp;min</b> from Azure Monitor metrics (current month
        only), while <b>billed</b> updates every <b style="color:var(--red)">4h</b> &mdash; so
        <b>check the estimate first</b> for a live read on spend, and treat billed as the final word
        once it catches up.</li>
  </ul>
</div>

<div class="kpi-strip">
  <div class="kpi"><div class="label">Billed <span id="kpi-period" class="dim"></span></div>
    <div id="kpi-billed" class="value">—</div><div class="sub">real invoiced (Cost Management)</div></div>
  <div class="kpi"><div class="label">Monthly budget</div>
    <div class="value">$__BUDGET__</div><div id="kpi-pct" class="sub">—</div></div>
  <div class="kpi"><div class="label">Token-based estimate</div>
    <div id="kpi-est" class="value">—</div><div id="kpi-est-sub" class="sub">current month only</div></div>
  <div class="kpi"><div class="label">Tracked resources</div>
    <div id="kpi-res" class="value">—</div><div id="kpi-res-sub" class="sub">—</div></div>
</div>

<div class="section-title">Spend by resource
  <span class="hint">stacked by model family · hover a segment · ○ = no billed spend yet</span></div>
<div class="panel"><div id="fig-stacked" class="plotly-host"></div></div>

<div class="section-title">Daily &amp; cumulative spend
  <span class="hint">whole lab · bars = that day · line = running month total</span></div>
<div class="panel"><div id="fig-daily-cumulative" class="plotly-host"></div></div>

<div class="section-title">Per-resource daily trend
  <span class="hint">pick a resource from the dropdown · sorted by spend</span></div>
<div class="panel"><div id="fig-per-resource" class="plotly-host"></div></div>

<div class="section-title">Tracked resources
  <span class="hint" id="res-table-hint">every resource discovered in the group</span></div>
<div class="panel"><table class="res"><thead><tr>
  <th>Resource</th><th>Billed $</th><th>Est $</th><th>Tokens</th><th>Calls</th>
</tr></thead><tbody id="res-tbody"></tbody></table></div>

<div class="footer">
  <span>Data source: Azure Cost Management <code>/query</code> + Azure Monitor metrics</span>
  <span>Refresh: re-run <code>usage_monitor.py</code></span>
</div>

<script>
const MONTHS = __MONTHS_JSON__;
const MONTH_KEYS = __MONTH_KEYS__;     // sorted ascending, e.g. ["2026-04","2026-05"]
const BUDGET = __BUDGET_NUM__;
const ROSTER = __ROSTER_JSON__;        // all currently discovered resources (live)
const SNAP_ESTIMATED = __SNAP_ESTIMATED__;  // token-based estimate, current MTD
const MLAB = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const PLOT_CFG = {displaylogo:false, responsive:true, modeBarButtonsToRemove:["lasso2d","select2d"]};
const FIGS = ["stacked","daily_cumulative","per_resource"];

let selected = null;       // "YYYY-MM"
let followCurrent = true;  // auto-advance on rollover until the user navigates

function etMonth() {
  const parts = new Intl.DateTimeFormat('en-US',
      {timeZone:'America/New_York', year:'numeric', month:'2-digit'}).formatToParts(new Date());
  let y="", m="";
  for (const p of parts) { if (p.type==='year') y=p.value; if (p.type==='month') m=p.value; }
  return y + "-" + m;
}

function fmtMoney(v) { return "$" + Number(v).toLocaleString('en-US',{minimumFractionDigits:2, maximumFractionDigits:2}); }
function fmtInt(v) { return Number(v).toLocaleString('en-US'); }

function years() {
  const ys = new Set(MONTH_KEYS.map(k => k.slice(0,4)));
  ys.add(etMonth().slice(0,4));         // ensure the current ET year exists for rollover
  return Array.from(ys).sort();
}

function monthsInYear(y) {
  // available months (have data) for that year
  return MONTH_KEYS.filter(k => k.startsWith(y)).map(k => parseInt(k.slice(5,7),10));
}

function buildNav() {
  const cur = etMonth();
  const yb = document.getElementById('year-bar');
  yb.innerHTML = "";
  years().forEach(y => {
    const b = document.createElement('button');
    b.className = 'btn'; b.textContent = y; b.dataset.year = y;
    b.onclick = () => selectYear(y, true);
    yb.appendChild(b);
  });
  renderMonthBar(selected ? selected.slice(0,4) : cur.slice(0,4));
}

function renderMonthBar(year) {
  const cur = etMonth();
  const avail = monthsInYear(year);
  const mb = document.getElementById('month-bar');
  mb.innerHTML = "";
  for (let m = 1; m <= 12; m++) {
    const key = year + "-" + String(m).padStart(2,'0');
    const b = document.createElement('button');
    b.className = 'btn'; b.dataset.month = key;
    b.innerHTML = MLAB[m-1] + (key === cur ? '<span class="dot">●</span>' : '');
    const hasData = avail.includes(m);
    const isCurrent = (key === cur);
    b.disabled = !hasData && !isCurrent;   // current month always clickable (may be empty)
    b.onclick = () => selectMonth(key, true);
    mb.appendChild(b);
  }
  // mark active year
  document.querySelectorAll('#year-bar .btn').forEach(el =>
    el.classList.toggle('active', el.dataset.year === year));
}

function selectYear(year, userAction) {
  renderMonthBar(year);
  const cur = etMonth();
  let target;
  if (year === cur.slice(0,4) && MONTHS[cur]) target = cur;   // current month, only if it has data
  const avail = monthsInYear(year);
  if (!target) {
    target = avail.length ? year + "-" + String(Math.max(...avail)).padStart(2,'0') : cur;
  }
  selectMonth(target, false);
}

function selectMonth(key, userAction) {
  const cur = etMonth();
  // followCurrent simply means "the view is pinned to the live month"; the
  // rollover watcher only auto-advances while this holds.
  followCurrent = (key === cur);
  selected = key;
  // sync the month bar to this key's year
  if (document.querySelectorAll('#month-bar .btn[data-month="'+key+'"]').length === 0) {
    renderMonthBar(key.slice(0,4));
  }
  document.querySelectorAll('#month-bar .btn').forEach(el =>
    el.classList.toggle('active', el.dataset.month === key));
  document.querySelectorAll('#year-bar .btn').forEach(el =>
    el.classList.toggle('active', el.dataset.year === key.slice(0,4)));
  document.getElementById('live-pill').style.display = (key === cur) ? '' : 'none';
  if (window.history && history.replaceState) history.replaceState(null, '', '#' + key);
  renderMonth(key);
}

function monthName(key) {
  const y = key.slice(0,4), m = parseInt(key.slice(5,7),10);
  return MLAB[m-1] + " " + y;
}

// Build the resource table rows. For the live month we merge the full current
// roster so idle / brand-new resources (no spend yet) are always visible; for a
// past month we just list whatever was billed that month.
function buildResourceRows(key, data) {
  const isLive = (key === etMonth());
  const rows = {};   // keyed by lower-cased name so casing variants collapse to one row
  (data ? data.resources : []).forEach(r => {
    rows[r.name.toLowerCase()] = {name: r.name, billed: r.billed, est: null,
                                  tokens: null, calls: null, idle: r.idle};
  });
  if (isLive) {
    ROSTER.forEach(r => {
      const k = r.name.toLowerCase();
      const ex = rows[k] || {name: r.name, billed: 0, idle: true};
      ex.name = r.name;            // prefer the created casing from discovery
      ex.est = r.est; ex.tokens = r.tokens; ex.calls = r.calls;
      ex.idle = (ex.billed === 0);
      rows[k] = ex;
    });
  }
  return Object.values(rows).sort((a, b) =>
      (b.billed - a.billed) || ((b.calls || 0) - (a.calls || 0)));
}

function renderMonth(key) {
  const data = MONTHS[key];
  const isLive = (key === etMonth());
  document.getElementById('kpi-period').textContent = "· " + monthName(key);

  // KPIs
  const billed = data ? data.billed : 0;
  const pct = data ? data.pct : 0;
  const over = data ? data.over : false;
  const bEl = document.getElementById('kpi-billed');
  bEl.textContent = fmtMoney(billed);
  bEl.className = "value " + (over ? "over" : "ok");
  const pctEl = document.getElementById('kpi-pct');
  pctEl.textContent = pct.toFixed(1) + "% used";
  pctEl.style.color = over ? "var(--red)" : "var(--green)";

  const est = isLive ? SNAP_ESTIMATED : null;
  document.getElementById('kpi-est').textContent = (est == null) ? "—" : fmtMoney(est);
  document.getElementById('kpi-est-sub').textContent = (est == null)
      ? "current month only" : "gap vs billed: " + fmtMoney(billed - est);

  // figures
  FIGS.forEach(f => {
    const divId = "fig-" + f.replace(/_/g, "-");
    const fig = data && data.figs[f];
    if (fig) { Plotly.react(divId, fig.data, fig.layout, PLOT_CFG); }
    else { emptyFig(divId, data ? "No data for this chart."
                                : "No billing yet for " + monthName(key) + " — Cost Management lags 8–24h."); }
  });

  // resource table
  const rows = buildResourceRows(key, data);
  const nBilling = rows.filter(r => !r.idle).length;
  const nIdle = rows.length - nBilling;
  document.getElementById('kpi-res').textContent = rows.length || "—";
  document.getElementById('kpi-res-sub').textContent =
      rows.length ? (nBilling + " billing · " + nIdle + " idle/new")
                  : "no resources";
  document.getElementById('res-table-hint').textContent = isLive
      ? "every resource discovered in the group · NEW = no spend yet · IDLE = has calls, no bill"
      : "resources billed in " + monthName(key);

  const tb = document.getElementById('res-tbody');
  tb.innerHTML = "";
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="5" class="dim" style="text-align:center;padding:18px;">No data for this month.</td></tr>';
    return;
  }
  rows.forEach(r => {
    let badge = "";
    if (r.idle) badge = (r.calls ? '<span class="badge idle">IDLE</span>'
                                 : '<span class="badge new">NEW</span>');
    const tr = document.createElement('tr');
    const models = r.models || [];
    // Tokens and calls are per MODEL: a resource-level total sums unrelated
    // models priced differently. Models hang off the row as a collapsible list.
    const toggle = models.length
      ? '<span class="caret">▸</span><span class="mcount">' + models.length
        + (models.length === 1 ? ' model' : ' models') + '</span>'
      : '';
    tr.className = models.length ? 'res-row has-models' : 'res-row';
    tr.innerHTML =
      '<td>' + r.name + badge + toggle + '</td>' +
      '<td>' + (r.billed ? fmtMoney(r.billed) : '<span class="dim">$0.00</span>') + '</td>' +
      '<td>' + (r.est == null ? '<span class="dim">—</span>' : fmtMoney(r.est)) + '</td>' +
      '<td class="dim">—</td>' +
      '<td class="dim">—</td>';
    tb.appendChild(tr);
    const kids = [];
    models.forEach(m => {
      const mtr = document.createElement('tr');
      mtr.className = 'model-row';
      mtr.hidden = true;
      mtr.innerHTML =
        '<td class="model-name">' + m.name + '</td>' +
        '<td class="dim">—</td>' +
        '<td>' + (m.est ? fmtMoney(m.est) : '<span class="dim">$0.00</span>') + '</td>' +
        '<td>' + fmtInt(m.tokens) + '</td>' +
        '<td>' + fmtInt(m.calls) + '</td>';
      tb.appendChild(mtr); kids.push(mtr);
    });
    if (kids.length) {
      tr.addEventListener('click', () => {
        const open = tr.classList.toggle('open');
        kids.forEach(k => { k.hidden = !open; });
      });
    }
  });
}

function emptyFig(divId, msg) {
  Plotly.purge(divId);
  document.getElementById(divId).innerHTML = '<div class="empty">' + msg + '</div>';
}

function initialMonth() {
  // Always open on the current (US Eastern) month — that is the "refresh on the
  // 1st" behavior. Early in a month it may have no billing yet (Cost Management
  // lags 8–24h); the charts say so and the roster table still lists every
  // resource, so brand-new resources stay visible. Past months are a click away.
  return etMonth();
}

// rollover watch: if the ET month changes and we're still following "current",
// advance the view to the new month (mirrors the refresh-on-the-1st behavior).
function watchRollover() {
  setInterval(() => {
    const cur = etMonth();
    if (followCurrent && selected !== cur) {
      buildNav();
      selectMonth(cur, false);
    }
  }, 60000);
}

buildNav();
const _hash = location.hash.slice(1);
selectMonth(/^\d{4}-\d{2}$/.test(_hash) ? _hash : initialMonth(), false);
watchRollover();
</script>
</body>
</html>
"""


def render(snapshot, billed_rows):
    budget = snapshot.get("monthly_budget_usd", 0.0)
    rg = snapshot.get("resource_group", "")
    generated_at = snapshot.get("generated_at", "")

    months = group_by_month(billed_rows)
    month_keys = sorted(months.keys())

    roster, snap_estimated = build_roster(snapshot)
    canonical = {r["name"].lower(): r["name"] for r in roster}
    saas_parents = snapshot.get("saas_parents", {})
    billed_rows = [(d, fold_marketplace_name(rn, saas_parents), m, c, rid)
                   for (d, rn, m, c, rid) in billed_rows]
    months = group_by_month(billed_rows)
    month_keys = sorted(months.keys())
    folded = [(d, canonical.get(rn.lower(), rn), m, c, rid)
              for (d, rn, m, c, rid) in billed_rows]
    saas_labels, _inferred = resolve_saas_labels(
        folded, snapshot.get("deployments_by_account", {}))
    payloads = {ym: build_month_payload(ym, months[ym], budget, canonical, saas_labels)
                for ym in month_keys}

    html = PAGE
    repl = {
        "__RG__": rg,
        "__GENERATED_AT__": generated_at,
        "__BG__": BG, "__CARD__": CARD, "__CARD_SOFT__": CARD_SOFT,
        "__CARD_BORDER__": CARD_BORDER, "__TEXT__": TEXT, "__TEXT_DIM__": TEXT_DIM,
        "__ACCENT__": ACCENT, "__GREEN__": GREEN, "__YELLOW__": YELLOW, "__RED__": RED,
        "__BUDGET__": f"{budget:,.0f}",
        "__BUDGET_NUM__": json.dumps(budget),
        "__PLOTLYJS__": get_plotlyjs(),
        "__MONTHS_JSON__": json.dumps(payloads),
        "__MONTH_KEYS__": json.dumps(month_keys),
        "__ROSTER_JSON__": json.dumps(roster),
        "__SNAP_ESTIMATED__": json.dumps(snap_estimated),
    }
    for k, v in repl.items():
        html = html.replace(k, v)

    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH.resolve()}  ({len(month_keys)} month(s): {', '.join(month_keys) or 'none'})")


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} not found — run usage_monitor.py first.")
    conn = sqlite3.connect(DB_PATH)
    snapshot, billed_rows = load(conn)
    conn.close()
    render(snapshot, billed_rows)


if __name__ == "__main__":
    main()
