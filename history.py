"""Aggregate the recorded GPU history for the /history page.

Reads HISTORY_DB (written by recorder.py) and answers, per high-end GPU
type: when are cards USABLE (hour-of-week heatmap), how did usable cards
and queued demand move over the window, and which hour-of-week slots are
the best bet. "Usable" = free AND on a node that still has an idle CPU and
memory; a free card on a fully-allocated node is a false signal and is not
counted. Rows recorded before the usable column existed count free as usable. Returns plain numbers; the page draws them with Plotly client-side
so the droplet's single core does no chart work.

Hours are in US Eastern (the clusters' and the lab's clock), Monday first.
"""
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HISTORY_DB = Path(os.environ.get("HISTORY_DB", "/var/lib/monitor/history.db"))
FOCUS_TYPES = [t.strip().lower() for t in os.environ.get(
    "HISTORY_GPU_TYPES", "a100,h100,h200,b200,rtx_pro_6000_blackwell").split(",") if t.strip()]
TZ = ZoneInfo(os.environ.get("HISTORY_TZ", "America/New_York"))
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
RANGES = (7, 30, 90)
MIN_SLOT_SAMPLES = 3           # a best-slot needs at least this many samples


def _open():
    if not HISTORY_DB.exists():
        return None
    return sqlite3.connect(f"file:{HISTORY_DB}?mode=ro", uri=True)


def _hour_of_week(ts):
    d = datetime.fromtimestamp(ts, TZ)
    return d.weekday(), d.hour


def _grid(fill=0.0):
    return [[fill] * 24 for _ in range(7)]


def build_history(slug, days=30, now=None):
    """Context for history.html. `days` is clamped to RANGES."""
    days = min(RANGES, key=lambda r: abs(r - int(days or 30)))
    now = int(now or time.time())
    since = now - days * 86400
    ctx = {"cluster": slug, "days": days, "ranges": list(RANGES), "tz": str(TZ),
           "day_names": DAY_NAMES, "types": [], "heat": {}, "timeline": {}, "best": {},
           "span": None, "error": None}
    conn = _open()
    if conn is None:
        ctx["error"] = "No history recorded yet. The recorder starts filling the database on its first run."
        return ctx
    try:
        # Types present for this cluster, in FOCUS_TYPES order.
        present = {r[0] for r in conn.execute(
            "SELECT DISTINCT gpu_type FROM samples WHERE cluster = ? "
            "UNION SELECT DISTINCT gpu_type FROM hourly WHERE cluster = ?", (slug, slug))}
        types = [t for t in FOCUS_TYPES if t in present]
        ctx["types"] = types
        n, lo, hi = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM samples WHERE cluster = ?", (slug,)).fetchone()
        h_lo = conn.execute("SELECT MIN(hour_ts) FROM hourly WHERE cluster = ?", (slug,)).fetchone()[0]
        first = min(x for x in (lo, h_lo) if x is not None) if (lo or h_lo) else None
        ctx["span"] = {"first": first, "last": hi, "samples": n,
                       "days_recorded": round((hi - first) / 86400, 1) if first and hi else 0}
        if not types:
            ctx["error"] = "Nothing recorded for this cluster yet."
            return ctx

        # Source rows: raw 5-min samples inside the raw-retention window,
        # hourly averages beyond it (older raw rows are gone by design).
        rows = conn.execute(
            "SELECT ts, gpu_type, COALESCE(usable, free), total, pending_gpus, free FROM samples "
            "WHERE cluster = ? AND ts >= ? ORDER BY ts", (slug, since)).fetchall()
        raw_lo = min((r[0] for r in rows), default=None)
        if raw_lo is None or raw_lo > since + 3600:
            hrows = conn.execute(
                "SELECT hour_ts, gpu_type, COALESCE(avg_usable, avg_free), avg_total, avg_pending_gpus, avg_free "
                "FROM hourly WHERE cluster = ? AND hour_ts >= ? AND hour_ts < ? ORDER BY hour_ts",
                (slug, since, raw_lo or now)).fetchall()
            rows = [(h, t, au, at, ap, af) for (h, t, au, at, ap, af) in hrows] + rows

        bucket = 3600 if days > 7 else 900        # timeline resolution: 15 min / 1 h
        for t in types:
            heat_sum, heat_n, heat_any = _grid(), _grid(0), _grid(0)
            tl = {}
            total_seen = 0
            for ts, gt, usable, total, pend_gpus, free in rows:
                if gt != t:
                    continue
                dow, hour = _hour_of_week(ts)
                heat_sum[dow][hour] += usable
                heat_n[dow][hour] += 1
                heat_any[dow][hour] += 1 if usable > 0 else 0
                b = ts - ts % bucket
                acc = tl.setdefault(b, [0.0, 0.0, 0, 0.0])
                acc[0] += usable
                acc[1] += pend_gpus
                acc[2] += 1
                acc[3] += free or 0
                total_seen = max(total_seen, total or 0)
            avg = [[round(heat_sum[d][h] / heat_n[d][h], 2) if heat_n[d][h] else None for h in range(24)]
                   for d in range(7)]
            p_any = [[round(heat_any[d][h] / heat_n[d][h], 3) if heat_n[d][h] else None for h in range(24)]
                     for d in range(7)]
            ctx["heat"][t] = {"avg_free": avg, "p_any_free": p_any, "n": heat_n, "total": total_seen}
            keys = sorted(tl)
            ctx["timeline"][t] = {
                "ts": keys,
                "free": [round(tl[k][0] / tl[k][2], 2) for k in keys],        # usable cards
                "free_any": [round(tl[k][3] / tl[k][2], 2) for k in keys],    # free incl. unusable
                "pending_gpus": [round(tl[k][1] / tl[k][2], 1) for k in keys],
                "total": total_seen,
                "bucket_seconds": bucket,
            }
            slots = [(avg[d][h], p_any[d][h], d, h) for d in range(7) for h in range(24)
                     if heat_n[d][h] >= MIN_SLOT_SAMPLES]
            slots.sort(key=lambda s: (-(s[1] or 0), -(s[0] or 0)))
            ctx["best"][t] = [{"day": DAY_NAMES[d], "hour": h, "avg_free": a, "p_any_free": p,
                               "n": heat_n[d][h]} for a, p, d, h in slots[:5]]
        return ctx
    finally:
        conn.close()
