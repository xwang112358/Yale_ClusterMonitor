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
    "claude":    "#d97757",
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
        SELECT usage_date, resource_name, meter, cost_usd
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
                  color=TEXT, size=12),
        margin=dict(l=10, r=20, t=10, b=40),
        hoverlabel=dict(bgcolor=CARD_SOFT, bordercolor=CARD_BORDER,
                        font=dict(family="SF Mono, monospace", color=TEXT)),
    )
    base.update(extra)
    return base


def stacked_bar_figure(billed_rows):
    """One trace per model family; each trace has one bar per resource.

    Resources whose billing nets to $0 for the month (e.g. offset by a credit)
    get an open-circle marker so they remain visible on the axis.
    """
    res_family_cost = defaultdict(lambda: defaultdict(float))
    res_family_meters = defaultdict(lambda: defaultdict(list))
    for usage_date, resource_name, meter, cost in billed_rows:
        fam = model_family(meter)
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
    families_sorted = sorted(family_totals.keys(),
                             key=lambda f: family_totals[f], reverse=True)

    fig = go.Figure()
    for fam in families_sorted:
        xs, hovers = [], []
        for r in resources_sorted:
            total = res_family_cost[r].get(fam, 0.0)
            xs.append(total)
            meters_detail = sorted(res_family_meters[r].get(fam, []),
                                   key=lambda mc: mc[1], reverse=True)
            meter_lines = "<br>".join(
                f"  · {m}  <b>${c:,.2f}</b>" for m, c in meters_detail
            ) or "  (no meters)"
            hovers.append(
                f"<b style='font-size:13px;'>{r}</b><br>"
                f"<span style='color:#6c8cff;'>model: <b>{fam}</b></span>  ·  "
                f"<b>${total:,.2f}</b><br>"
                f"<span style='color:#888;'>billed meters:</span><br>{meter_lines}"
            )
        fig.add_trace(go.Bar(
            name=fam, y=resources_sorted, x=xs, orientation="h",
            marker=dict(color=FAMILY_COLORS.get(fam, FAMILY_COLORS["other"]),
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
                    x=1, bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT, size=11)),
        height=max(360, 34 * len(resources_sorted) + 90),
    )))
    return fig


def cumulative_line_figure(daily, budget):
    if not daily:
        return None
    days = [r[0] for r in daily]
    cumulative, running = [], 0.0
    for _, c in daily:
        running += c or 0.0
        cumulative.append(running)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=days, y=cumulative, mode="lines+markers",
        line=dict(color=ACCENT, width=2),
        marker=dict(size=7, color=ACCENT, line=dict(color=BG, width=1)),
        fill="tozeroy", fillcolor="rgba(108, 140, 255, 0.12)",
        hovertemplate="<b>%{x}</b><br>Cumulative: $%{y:,.2f}<extra></extra>",
        name="Cumulative billed",
    ))
    fig.add_hline(
        y=budget, line=dict(color=RED, dash="dash", width=1.5),
        annotation_text=f"Monthly budget ${budget:,.0f}",
        annotation_position="top left", annotation_font=dict(color=RED, size=10),
    )
    crossed = next((i for i, v in enumerate(cumulative) if v >= budget), None)
    if crossed is not None:
        fig.add_trace(go.Scatter(
            x=[days[crossed]], y=[cumulative[crossed]], mode="markers",
            marker=dict(size=12, color=RED, symbol="x-thin",
                        line=dict(width=2, color=RED)),
            hovertemplate=f"<b>Budget crossed</b><br>{days[crossed]}: ${cumulative[crossed]:,.2f}<extra></extra>",
            showlegend=False,
        ))
    fig.update_layout(**_base_layout(dict(
        xaxis=dict(gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        yaxis=dict(tickprefix="$", tickformat=",.0f",
                   gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        showlegend=False, height=320,
    )))
    return fig


def daily_line_figure(daily, budget):
    if not daily:
        return None
    days = [r[0] for r in daily]
    costs = [r[1] or 0.0 for r in daily]
    burn = budget / 30.0

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=days, y=costs, mode="lines+markers",
        line=dict(color=GREEN, width=2),
        marker=dict(size=7, color=GREEN, line=dict(color=BG, width=1)),
        fill="tozeroy", fillcolor="rgba(76, 175, 80, 0.15)",
        hovertemplate="<b>%{x}</b><br>$%{y:,.2f}<extra></extra>", name="Daily billed",
    ))
    fig.add_hline(
        y=burn, line=dict(color=RED, dash="dash", width=1),
        annotation_text=f"On-budget burn: ${burn:,.0f}/day",
        annotation_position="top left", annotation_font=dict(color=TEXT_DIM, size=10),
    )
    fig.update_layout(**_base_layout(dict(
        xaxis=dict(gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        yaxis=dict(tickprefix="$", tickformat=",.0f",
                   gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        showlegend=False, height=320,
    )))
    return fig


def per_resource_daily_figure(billed_rows):
    by_res = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    detail = defaultdict(lambda: defaultdict(list))
    all_days = set()
    for usage_date, resource_name, meter, cost in billed_rows:
        fam = model_family(meter)
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
                meters = sorted(detail[r].get((d, fam), []), key=lambda mc: mc[1], reverse=True)
                meter_lines = "<br>".join(f"  · {m}  <b>${c:,.2f}</b>" for m, c in meters) or "  (no meters)"
                hovers.append(
                    f"<b>{r}</b>  ·  {d}<br>"
                    f"<span style='color:#6c8cff;'>model: <b>{fam}</b></span>  ·  <b>${v:,.2f}</b><br>"
                    f"<span style='color:#888;'>billed meters:</span><br>{meter_lines}"
                )
            fig.add_trace(go.Bar(
                x=days_sorted, y=ys, name=fam,
                marker=dict(color=FAMILY_COLORS.get(fam, FAMILY_COLORS["other"]),
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
                                        yref="paper", font=dict(color=TEXT, size=13),
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
                    x=0.5, bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT, size=11)),
        height=420,
        updatemenus=[dict(buttons=buttons, direction="down", x=1, xanchor="right",
                          y=1.22, yanchor="top", bgcolor=CARD_SOFT,
                          bordercolor=CARD_BORDER, font=dict(color=TEXT, size=11),
                          showactive=True, pad=dict(l=8, r=8, t=4, b=4))],
        annotations=[dict(text=f"<b>{first_r}</b>  ·  ${first_total:,.2f}",
                          showarrow=False, x=0, y=1.18, xref="paper", yref="paper",
                          font=dict(color=TEXT, size=13), align="left", xanchor="left")],
    )))
    return fig


# ---------------------------------------------------------------------------
# Per-month payload
# ---------------------------------------------------------------------------


def fig_json(fig):
    return json.loads(fig.to_json()) if fig is not None else None


def build_month_payload(ym, rows, budget, canonical=None):
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
        rows = [(d, canonical.get(rn.lower(), rn), m, c) for (d, rn, m, c) in rows]
    daily_map = defaultdict(float)
    res_billed = defaultdict(float)
    for usage_date, rn, _m, cost in rows:
        daily_map[usage_date] += cost
        res_billed[rn] += cost
    daily = sorted(daily_map.items())

    billed = round(sum(c for _, c in daily), 2)
    pct = round((billed / budget) * 100, 1) if budget else 0.0

    resources = [{"name": n, "billed": round(b, 2), "idle": round(b, 2) == 0.0}
                 for n, b in res_billed.items()]
    resources.sort(key=lambda r: r["billed"], reverse=True)

    figs = {
        "stacked": fig_json(stacked_bar_figure(rows)),
        "daily": fig_json(daily_line_figure(daily, budget)),
        "cumulative": fig_json(cumulative_line_figure(daily, budget)),
        "per_resource": fig_json(per_resource_daily_figure(rows)),
    }
    return {
        "billed": billed,
        "pct": pct,
        "over": billed > budget,
        "figs": figs,
        "resources": resources,
    }


def build_roster(snapshot):
    """Current full resource roster from the latest snapshot — every discovered
    resource with its MTD token/call activity. Merged onto the live month."""
    mtd = snapshot.get("month_to_date", {})
    roster = []
    for r in mtd.get("by_resource", []):
        roster.append({
            "name": r["resource"],
            "est": r.get("estimated_cost_usd"),
            "tokens": r.get("total_tokens"),
            "calls": r.get("calls"),
        })
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
    <li><b>Billed vs estimated:</b> "billed" is the real invoiced number from Cost Management; the
        token-based estimate is a diagnostic only and is current-month-only.</li>
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

<div class="panel-row">
  <div><div class="section-title">Daily billed<span class="hint">selected month</span></div>
       <div class="panel"><div id="fig-daily" class="plotly-host"></div></div></div>
  <div><div class="section-title">Cumulative<span class="hint">vs monthly budget</span></div>
       <div class="panel"><div id="fig-cumulative" class="plotly-host"></div></div></div>
</div>

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
const FIGS = ["stacked","daily","cumulative","per_resource"];

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
    const divId = "fig-" + (f === "per_resource" ? "per-resource" : f);
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
    tr.innerHTML =
      '<td>' + r.name + badge + '</td>' +
      '<td>' + (r.billed ? fmtMoney(r.billed) : '<span class="dim">$0.00</span>') + '</td>' +
      '<td>' + (r.est == null ? '<span class="dim">—</span>' : fmtMoney(r.est)) + '</td>' +
      '<td>' + (r.tokens == null ? '<span class="dim">—</span>' : fmtInt(r.tokens)) + '</td>' +
      '<td>' + (r.calls == null ? '<span class="dim">—</span>' : fmtInt(r.calls)) + '</td>';
    tb.appendChild(tr);
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
    payloads = {ym: build_month_payload(ym, months[ym], budget, canonical) for ym in month_keys}

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
