"""Data + figure layer for the Azure usage dashboard (/azure route).

Reads usage.db (path from AZURE_USAGE_DB env var, default ../usage.db) and
returns template context for templates/azure.html.

Design (kept in sync with the standalone d:\\xwang\\summer26\\monitor\\dashboard.py):
  * Per-month figures are pre-rendered server-side and embedded as JSON. The
    template's JS switches months client-side via Plotly.react(), so reading any
    past month is instant and the page defaults to the current month in US
    Eastern time (auto-rolling on the 1st).
  * A "Tracked resources" table merges the live snapshot roster onto the ET
    current month, so brand-new / idle resources with no billed spend are always
    visible instead of dropping out of the billing-only charts.

Plotly.js is loaded once from CDN by the template; figures are serialized with
fig.to_json() (no inline Plotly.js).
"""

import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import plotly.graph_objects as go

DB_PATH = Path(os.environ.get("AZURE_USAGE_DB",
                              Path(__file__).parent.parent / "usage.db"))

# --- ClusterMonitor palette ---
BG = "#0f1117"
CARD = "#1a1d27"
CARD_SOFT = "#20232f"
CARD_BORDER = "#2a2d3a"
TEXT = "#e0e0e0"
TEXT_DIM = "#888"
ACCENT = "#6c8cff"
GREEN = "#4caf50"
RED = "#f44336"

# Page and chart text scale. The HTML sets the same multiplier on the root
# font-size (all CSS sizes are rem), so this keeps Plotly's px fonts in step;
# change both together or the charts drift out of proportion with the page.
FONT_SCALE = 1.25


def fs(px):
    """Scale a Plotly font size by FONT_SCALE."""
    return int(round(px * FONT_SCALE))


FAMILY_COLORS = {
    "gpt-5.4": "#7c3aed", "gpt-5.3": "#9333ea", "gpt-5.2": "#a855f7", "gpt-5": "#c084fc",
    "gpt-4o": "#2563eb", "gpt-4-turbo": "#3b82f6", "gpt-4": "#60a5fa",
    "gpt-3.5": "#0d9488",
    "o3-mini": "#ea580c", "o3": "#f97316",
    "o1-mini": "#facc15", "o1": "#eab308",
    # Foundry-hosted Anthropic models. One hue stepped dark->light by capability
    # tier so they read as a group; rose-shifted to stay clear of the o-series
    # oranges, and validated as an ordinal ramp against the dark surface.
    "claude-opus-4.8":   "#802f35",
    "claude-opus-4.6":   "#9c414a",
    "claude-sonnet-5":   "#b9575d",
    "claude-sonnet-4.6": "#d17a72",
    "claude-sonnet-4.5": "#e5a396",
    "claude-haiku-4.5":  "#f5cbc0",
    "claude": "#d97757",  # unknown / unresolved model
    "embed": "#16a34a",
    "other": "#64748b",
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


# Azure AI Foundry bills Anthropic models through a Marketplace SaaS resource
# named '<model>-<parent>-<uid>', and every one of them shares a single generic
# meter, so the model is recoverable only from the resource id -- and even then
# only partly: Foundry clips <model> to 15 chars, so claude-sonnet-4-5 and
# claude-sonnet-4-6 both arrive as 'claude-sonnet-4'. We resolve the token
# against the account's real deployment roster and only name a version when the
# match is unambiguous.
SAAS_MODEL_RE = re.compile(
    r"/providers/microsoft\.saas/resources/(?P<model>.+)-[0-9a-f]{15}-[0-9a-f]{32}$",
    re.I)
CLAUDE_METER_RE = re.compile(
    r"^Claude in Microsoft Foundry\s*\(([^)]+)\).*?([a-z0-9\-]+units)\s*$", re.I)
AMBIGUOUS = "…"  # trailing ellipsis marks "some version of this, unresolved"


def saas_model_token(resource_id):
    """The clipped model token from a Foundry SaaS resource id, else None."""
    m = SAAS_MODEL_RE.search(resource_id or "")
    return m.group("model").lower() if m else None


def _pretty_model(name):
    """claude-sonnet-4-6 -> claude-sonnet-4.6 (Foundry writes '.' as '-')."""
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
    # An unresolved label ('claude-sonnet-4...') should still sit in the Claude
    # ramp rather than fall back to the lone generic terracotta, which reads
    # orange next to the o-series. Take the lowest matching step deterministically.
    base = fam.rstrip(AMBIGUOUS)
    kin = sorted(k for k in FAMILY_COLORS if k.startswith(base) and k != "claude")
    if kin:
        return FAMILY_COLORS[kin[0]]
    return FAMILY_COLORS["claude" if fam.startswith("claude") else "other"]


def model_family(meter):
    if not meter:
        return "other"
    for pat, name in FAMILY_PATTERNS:
        if pat.search(meter):
            return name
    return "other"


# ----------------- Figure builders (one month's rows) -----------------

def _base_layout(extra=None):
    layout = dict(
        template="plotly_dark",
        paper_bgcolor=CARD,
        plot_bgcolor=CARD,
        font=dict(family="SF Mono, Cascadia Code, Consolas, monospace",
                  color=TEXT, size=fs(12)),
        margin=dict(l=10, r=20, t=10, b=40),
        hoverlabel=dict(bgcolor=CARD_SOFT, bordercolor=CARD_BORDER,
                        font=dict(family="SF Mono, monospace", color=TEXT)),
    )
    if extra:
        layout.update(extra)
    return layout


def stacked_bar_figure(billed_rows, saas_labels=None):
    res_family_cost = defaultdict(lambda: defaultdict(float))
    res_family_meters = defaultdict(lambda: defaultdict(list))
    saas_labels = saas_labels or {}
    for _date, resource_name, meter, cost, resource_id in billed_rows:
        fam = saas_labels.get(resource_id) or model_family(meter)
        res_family_cost[resource_name][fam] += cost
        res_family_meters[resource_name][fam].append((meter, cost))
    if not res_family_cost:
        return None
    resources_sorted = sorted(res_family_cost.keys(),
                              key=lambda r: sum(res_family_cost[r].values()))
    family_totals = defaultdict(float)
    for r in res_family_cost:
        for f, v in res_family_cost[r].items():
            family_totals[f] += v
    # Drop families with no spend this month: they add legend entries with no
    # visible segment (resources with zero spend are shown by the open-circle
    # marker below instead).
    families_sorted = sorted((f for f, v in family_totals.items() if v > 0),
                             key=lambda f: family_totals[f], reverse=True)

    fig = go.Figure()
    for fam in families_sorted:
        xs = []
        hovers = []
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
            hovertemplate="%{customdata}<extra></extra>",
            customdata=hovers,
        ))

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
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT, size=fs(11))),
        height=max(360, 34 * len(resources_sorted) + 90),
    )))
    return fig


def daily_and_cumulative_figure(daily, budget):
    """Lab-wide daily spend (bars) and running month total (line), one $ axis.

    Both series are USD, so they share a single axis -- a second y-scale would
    let the two lines be drawn at any relative height and invites misreading.
    The budget is deliberately NOT drawn as a line unless the month gets close
    to it: at $2,000 against a typical $90 month it flattens everything into the
    baseline. Below that it is reported in the subtitle instead.
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
    # The budget is an order of magnitude above a normal month, so drawing it
    # would pin the axis there and flatten the daily bars into the baseline.
    # Draw it only once it is actually crossed -- the moment it starts mattering,
    # and the point at which the axis has to include it anyway. Until then the
    # pace lives in the subtitle, which is where the burn rate goes too (at
    # ~$67/day it is indistinguishable from zero on a month-total scale).
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
        height=360,
        margin=dict(t=64),
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
    resources_sorted = sorted(
        by_res.keys(),
        key=lambda r: sum(sum(d.values()) for d in by_res[r].values()),
        reverse=True,
    )
    fam_totals = defaultdict(float)
    for r in by_res:
        for d in by_res[r]:
            for f, v in by_res[r][d].items():
                fam_totals[f] += v
    families_sorted = sorted(fam_totals.keys(), key=lambda f: fam_totals[f], reverse=True)

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
                    f"<span style='color:#6c8cff;'>model: <b>{fam}</b></span>  ·  "
                    f"<b>${v:,.2f}</b><br>"
                    f"<span style='color:#888;'>billed meters:</span><br>{meter_lines}"
                )
            fig.add_trace(go.Bar(
                x=days_sorted, y=ys, name=fam,
                marker=dict(color=family_color(fam),
                            line=dict(color=BG, width=0.3)),
                visible=(r == resources_sorted[0]),
                hovertemplate="%{customdata}<extra></extra>",
                customdata=hovers, legendgroup=fam,
            ))
            trace_resource.append(r)
        daily_totals = [sum(by_res[r][d].values()) for d in days_sorted]
        cum = []
        running = 0.0
        for v in daily_totals:
            running += v
            cum.append(running)
        fig.add_trace(go.Scatter(
            x=days_sorted, y=cum, name="cumulative",
            mode="lines+markers",
            line=dict(color=TEXT, width=2, dash="dot"),
            marker=dict(size=6, color=TEXT, line=dict(color=BG, width=1)),
            yaxis="y2",
            visible=(r == resources_sorted[0]),
            hovertemplate=f"<b>{r}</b>  ·  %{{x}}<br>"
                          f"<span style='color:#888;'>cumulative</span>  ·  "
                          f"<b>$%{{y:,.2f}}</b><extra></extra>",
            legendgroup="cumulative",
        ))
        trace_resource.append(r)

    buttons = []
    for r in resources_sorted:
        total = sum(sum(d.values()) for d in by_res[r].values())
        visible = [tr == r for tr in trace_resource]
        buttons.append(dict(
            label=f"{r}  —  ${total:,.0f}",
            method="update",
            args=[
                {"visible": visible},
                {"annotations": [dict(
                    text=f"<b>{r}</b>  ·  ${total:,.2f}",
                    showarrow=False, x=0, y=1.18, xref="paper", yref="paper",
                    font=dict(color=TEXT, size=fs(13)), align="left", xanchor="left",
                )]},
            ],
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
        legend=dict(orientation="h", yanchor="top", y=-0.15,
                    xanchor="center", x=0.5, bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT, size=fs(11))),
        height=420,
        updatemenus=[dict(
            buttons=buttons, direction="down",
            x=1, xanchor="right", y=1.22, yanchor="top",
            bgcolor=CARD_SOFT, bordercolor=CARD_BORDER,
            font=dict(color=TEXT, size=fs(11)), showactive=True,
            pad=dict(l=8, r=8, t=4, b=4),
        )],
        annotations=[dict(
            text=f"<b>{first_r}</b>  ·  ${first_total:,.2f}",
            showarrow=False, x=0, y=1.18, xref="paper", yref="paper",
            font=dict(color=TEXT, size=fs(13)), align="left", xanchor="left",
        )],
    )))
    return fig


# ----------------- Per-month assembly -----------------

def fig_json(fig):
    return json.loads(fig.to_json()) if fig is not None else None


def group_by_month(billed_rows):
    months = defaultdict(list)
    for row in billed_rows:
        months[row[0][:7]].append(row)
    return months


def build_month_payload(rows, budget, canonical=None, saas_labels=None):
    # Cost Management lowercases resource names while discovery/metrics keep the
    # created casing. Fold billing onto the created casing so a resource isn't
    # split into two rows (e.g. belo2-yhf vs BELO2-YHF). `canonical` maps
    # lower-cased name -> created casing.
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

    return {
        "billed": billed,
        "pct": pct,
        "over": billed > budget,
        "figs": {
            "stacked": fig_json(stacked_bar_figure(rows, saas_labels)),
            "daily_cumulative": fig_json(daily_and_cumulative_figure(daily, budget)),
            "per_resource": fig_json(per_resource_daily_figure(rows, saas_labels)),
        },
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
        if not (tokens or calls):
            continue
        out.append({"name": name, "tokens": tokens, "calls": calls})
    out.sort(key=lambda m: (m["tokens"], m["calls"]), reverse=True)
    return out


def _is_inactive_project(entry):
    """True for an accounts/projects child with nothing to report."""
    if not str(entry.get("resource", "")).endswith(" (project)"):
        return False
    return not any((entry.get("total_tokens") or 0,
                    entry.get("calls") or 0,
                    entry.get("estimated_cost_usd") or 0))


def build_roster(snapshot):
    mtd = snapshot.get("month_to_date", {})
    saas_parents = snapshot.get("saas_parents", {})
    roster, seen = [], {}
    for r in mtd.get("by_resource", []):
        # An accounts/projects child is not a resource of its own -- its traffic
        # shows up on the parent account's metrics. Discovery keeps them because a
        # project CAN report separately, but one with no activity is just a
        # duplicate row next to its parent (volmo-jaxon vs volmo-jaxon-resource).
        if _is_inactive_project(r):
            continue
        name = fold_marketplace_name(r["resource"], saas_parents)
        row = seen.get(name)
        if row is None:
            row = {
                "name": name,
                "tokens": r.get("total_tokens"),
                "calls": r.get("calls"),
                "status": r.get("status"),  # "removed" for resources no longer in RG
                "models": _model_rows(r),
            }
            seen[name] = row
            roster.append(row)
            continue
        # Folding collapsed two entries onto one account: merge their totals.
        for k, src_key in (("tokens", "total_tokens"), ("calls", "calls")):
            if r.get(src_key) is not None:
                row[k] = (row.get(k) or 0) + (r.get(src_key) or 0)
        row["models"] = sorted(row.get("models", []) + _model_rows(r),
                               key=lambda m: (m["tokens"], m["calls"]), reverse=True)
        if not r.get("status"):
            row["status"] = None  # a live entry outranks a "removed" one
    return roster


def _json_for_script(obj):
    """Serialize for embedding inside a <script> block (neutralize </script>)."""
    return json.dumps(obj).replace("</", "<\\/")


# ----------------- Public entrypoint -----------------

def build_context(db_path=None):
    """Read all of usage.db and return template context for azure.html."""
    db_path = Path(db_path) if db_path else DB_PATH
    if not db_path.exists():
        return {"error": f"usage.db not found at {db_path}. Run usage_monitor.py."}

    conn = sqlite3.connect(db_path)
    snap_row = conn.execute(
        "SELECT payload_json FROM snapshots ORDER BY snapshot_time DESC LIMIT 1"
    ).fetchone()
    if not snap_row:
        conn.close()
        return {"error": "No snapshots in usage.db. Run usage_monitor.py first."}
    snapshot = json.loads(snap_row[0])

    billed_rows = conn.execute(
        """
        SELECT usage_date, resource_name, meter, cost_usd, resource_id
        FROM billed_costs
        ORDER BY usage_date
        """
    ).fetchall()

    # When the last refresh was the 30-min metrics-only timer, snapshot.generated_at
    # reflects the estimate refresh — but billed $ came from cached billed_costs and
    # hasn't actually moved. Walk back through recent snapshots to find the most
    # recent one that queried Cost Management "live", and use *its* generated_at as
    # the billed refresh time.
    billed_refreshed_at = snapshot.get("generated_at", "")
    if snapshot.get("month_to_date", {}).get("billed_source") != "live":
        for (pj,) in conn.execute(
            "SELECT payload_json FROM snapshots ORDER BY snapshot_time DESC LIMIT 60"
        ).fetchall():
            try:
                p = json.loads(pj)
            except Exception:
                continue
            if p.get("month_to_date", {}).get("billed_source") == "live":
                billed_refreshed_at = p.get("generated_at", "") or billed_refreshed_at
                break
    conn.close()

    budget = snapshot.get("monthly_budget_usd", 0.0)
    # Marketplace billing ids never reach the display layer: fold them onto the
    # account that hosts the deployment before anything is grouped or charted.
    saas_parents = snapshot.get("saas_parents", {})
    billed_rows = [(d, fold_marketplace_name(rn, saas_parents), m, c, rid)
                   for (d, rn, m, c, rid) in billed_rows]
    months = group_by_month(billed_rows)
    month_keys = sorted(months.keys())
    roster = build_roster(snapshot)
    canonical = {r["name"].lower(): r["name"] for r in roster}
    # Resolve each Foundry SaaS resource's clipped model token against the
    # account's real deployment roster, so segments name the actual model.
    # Rows are folded onto the canonical casing first, to match roster keys.
    folded = [(d, canonical.get(rn.lower(), rn), m, c, rid)
              for (d, rn, m, c, rid) in billed_rows]
    saas_labels, _inferred = resolve_saas_labels(
        folded, snapshot.get("deployments_by_account", {}))
    payloads = {ym: build_month_payload(months[ym], budget, canonical, saas_labels)
                for ym in month_keys}

    return {
        "resource_group": snapshot.get("resource_group", ""),
        "generated_at": snapshot.get("generated_at", ""),
        "billed_refreshed_at": billed_refreshed_at,
        "budget": budget,
        "budget_str": f"{budget:,.0f}",
        "months_json": _json_for_script(payloads),
        "month_keys_json": _json_for_script(month_keys),
        "roster_json": _json_for_script(roster),
        "budget_json": json.dumps(budget),
    }
