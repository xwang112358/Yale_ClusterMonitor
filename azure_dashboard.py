"""Figure builders for the Azure usage dashboard.

Reads usage.db (path from AZURE_USAGE_DB env var, default ../usage.db)
and produces Plotly figures as HTML divs ready to inject into a Jinja
template. Plotly.js is loaded once from CDN by the template — figures
themselves use include_plotlyjs=False.

Same dark palette as templates/index.html so the Azure page reads as
part of the same product.
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

FAMILY_COLORS = {
    "gpt-5.4": "#7c3aed", "gpt-5.3": "#9333ea", "gpt-5.2": "#a855f7", "gpt-5": "#c084fc",
    "gpt-4o": "#2563eb", "gpt-4-turbo": "#3b82f6", "gpt-4": "#60a5fa",
    "gpt-3.5": "#0d9488",
    "o3-mini": "#ea580c", "o3": "#f97316",
    "o1-mini": "#facc15", "o1": "#eab308",
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
    (re.compile(r"embed", re.I),               "embed"),
]


def model_family(meter):
    if not meter:
        return "other"
    for pat, name in FAMILY_PATTERNS:
        if pat.search(meter):
            return name
    return "other"


# ----------------- Figure builders -----------------

def _base_layout(extra=None):
    layout = dict(
        template="plotly_dark",
        paper_bgcolor=CARD,
        plot_bgcolor=CARD,
        font=dict(family="SF Mono, Cascadia Code, Consolas, monospace",
                  color=TEXT, size=12),
        margin=dict(l=10, r=20, t=10, b=40),
        hoverlabel=dict(bgcolor=CARD_SOFT, bordercolor=CARD_BORDER,
                        font=dict(family="SF Mono, monospace", color=TEXT)),
    )
    if extra:
        layout.update(extra)
    return layout


def stacked_bar_figure(billed_rows):
    res_family_cost = defaultdict(lambda: defaultdict(float))
    res_family_meters = defaultdict(lambda: defaultdict(list))
    for _date, resource_name, meter, cost in billed_rows:
        fam = model_family(meter)
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
    families_sorted = sorted(family_totals.keys(),
                             key=lambda f: family_totals[f], reverse=True)

    fig = go.Figure()
    for fam in families_sorted:
        xs = []
        hovers = []
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
            hovertemplate="%{customdata}<extra></extra>",
            customdata=hovers,
        ))
    fig.update_layout(**_base_layout(dict(
        barmode="stack",
        xaxis=dict(title="MTD billed $ (USD)", tickprefix="$", tickformat=",.0f",
                   gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        yaxis=dict(gridcolor=CARD_BORDER, automargin=True),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, bgcolor="rgba(0,0,0,0)",
                    font=dict(color=TEXT, size=11)),
        height=max(360, 36 * len(resources_sorted) + 80),
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
        hovertemplate="<b>%{x}</b><br>$%{y:,.2f}<extra></extra>",
    ))
    fig.add_hline(y=burn, line=dict(color=RED, dash="dash", width=1),
                  annotation_text=f"On-budget burn: ${burn:,.0f}/day",
                  annotation_position="top left",
                  annotation_font=dict(color=TEXT_DIM, size=10))
    fig.update_layout(**_base_layout(dict(
        xaxis=dict(gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        yaxis=dict(tickprefix="$", tickformat=",.0f",
                   gridcolor=CARD_BORDER, zerolinecolor=CARD_BORDER),
        showlegend=False, height=320,
    )))
    return fig


def cumulative_line_figure(daily, budget):
    if not daily:
        return None
    days = [r[0] for r in daily]
    running = 0.0
    cum = []
    for _, c in daily:
        running += c or 0.0
        cum.append(running)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=days, y=cum, mode="lines+markers",
        line=dict(color=ACCENT, width=2),
        marker=dict(size=7, color=ACCENT, line=dict(color=BG, width=1)),
        fill="tozeroy", fillcolor="rgba(108, 140, 255, 0.12)",
        hovertemplate="<b>%{x}</b><br>Cumulative: $%{y:,.2f}<extra></extra>",
    ))
    fig.add_hline(y=budget, line=dict(color=RED, dash="dash", width=1.5),
                  annotation_text=f"Monthly budget ${budget:,.0f}",
                  annotation_position="top left",
                  annotation_font=dict(color=RED, size=10))
    crossed = next((i for i, v in enumerate(cum) if v >= budget), None)
    if crossed is not None:
        fig.add_trace(go.Scatter(
            x=[days[crossed]], y=[cum[crossed]], mode="markers",
            marker=dict(size=12, color=RED, symbol="x-thin",
                        line=dict(width=2, color=RED)),
            hovertemplate=f"<b>Budget crossed</b><br>{days[crossed]}: ${cum[crossed]:,.2f}<extra></extra>",
            showlegend=False,
        ))
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
                meters = sorted(detail[r].get((d, fam), []),
                                key=lambda mc: mc[1], reverse=True)
                meter_lines = "<br>".join(
                    f"  · {m}  <b>${c:,.2f}</b>" for m, c in meters
                ) or "  (no meters)"
                hovers.append(
                    f"<b>{r}</b>  ·  {d}<br>"
                    f"<span style='color:#6c8cff;'>model: <b>{fam}</b></span>  ·  "
                    f"<b>${v:,.2f}</b><br>"
                    f"<span style='color:#888;'>billed meters:</span><br>{meter_lines}"
                )
            fig.add_trace(go.Bar(
                x=days_sorted, y=ys, name=fam,
                marker=dict(color=FAMILY_COLORS.get(fam, FAMILY_COLORS["other"]),
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
                    text=f"<b>{r}</b>  ·  MTD ${total:,.2f}",
                    showarrow=False, x=0, y=1.18, xref="paper", yref="paper",
                    font=dict(color=TEXT, size=13), align="left", xanchor="left",
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
                    font=dict(color=TEXT, size=11)),
        height=420,
        updatemenus=[dict(
            buttons=buttons, direction="down",
            x=1, xanchor="right", y=1.22, yanchor="top",
            bgcolor=CARD_SOFT, bordercolor=CARD_BORDER,
            font=dict(color=TEXT, size=11), showactive=True,
            pad=dict(l=8, r=8, t=4, b=4),
        )],
        annotations=[dict(
            text=f"<b>{first_r}</b>  ·  MTD ${first_total:,.2f}",
            showarrow=False, x=0, y=1.18, xref="paper", yref="paper",
            font=dict(color=TEXT, size=13), align="left", xanchor="left",
        )],
    )))
    return fig


# ----------------- Public entrypoint -----------------

def _fig_html(fig, div_id):
    """Render a figure as a div + Plotly.newPlot call (no inline Plotly.js)."""
    if fig is None:
        return f'<div id="{div_id}" class="empty">No data.</div>'
    return fig.to_html(
        include_plotlyjs=False, full_html=False, div_id=div_id,
        config={"displaylogo": False, "responsive": True,
                "modeBarButtonsToRemove": ["lasso2d", "select2d"]},
    )


def build_context(db_path=None):
    """Read the latest snapshot from usage.db and return template context."""
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
        SELECT usage_date, resource_name, meter, cost_usd
        FROM billed_costs
        WHERE usage_date >= date('now', 'start of month')
        ORDER BY usage_date
        """
    ).fetchall()
    daily = conn.execute(
        """
        SELECT usage_date AS day, SUM(cost_usd) AS cost
        FROM billed_costs
        WHERE usage_date >= date('now', 'start of month')
        GROUP BY day ORDER BY day
        """
    ).fetchall()
    conn.close()

    mtd = snapshot["month_to_date"]
    billed = mtd.get("billed_cost_usd", 0.0)
    estimated = mtd.get("estimated_cost_usd", 0.0)
    pct = mtd.get("percent_of_budget", 0.0)
    budget = snapshot.get("monthly_budget_usd", 0.0)
    n_resources = len({r["resource"] for r in mtd.get("by_resource", [])
                       if r.get("billed_cost_usd", 0) > 0})

    return {
        "resource_group": snapshot.get("resource_group", ""),
        "generated_at": snapshot.get("generated_at", ""),
        "billed": billed,
        "estimated": estimated,
        "pct": pct,
        "budget": budget,
        "over": billed > budget,
        "gap": billed - estimated,
        "n_resources": n_resources,
        "fig_stacked": _fig_html(stacked_bar_figure(billed_rows), "fig-stacked"),
        "fig_daily": _fig_html(daily_line_figure(daily, budget), "fig-daily"),
        "fig_cumulative": _fig_html(cumulative_line_figure(daily, budget), "fig-cumulative"),
        "fig_per_resource": _fig_html(per_resource_daily_figure(billed_rows), "fig-per-resource"),
    }
