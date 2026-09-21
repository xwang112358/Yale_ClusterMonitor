#!/usr/bin/env python3
"""Record compact GPU-availability aggregates from the cluster snapshots.

Runs every 5 minutes from cluster-history.timer (deploy/) as the `monitor`
user with the app's .env. It reads each cluster's snapshot through app.py's
own parser and appends per-GPU-type COUNTS to HISTORY_DB (SQLite). Nothing
per user or per job is stored (see the policy notes in DEPLOY.md): the
archive is aggregates only. /history renders it (history.py).

Tables
  samples          (ts, cluster, gpu_type) one per timer tick (5 min); raw rows kept HISTORY_RAW_DAYS
  cluster_samples  (ts, cluster) running / pending totals
  hourly           per-hour rollup of samples, kept forever (a few MB per year)

`ts` is the pusher's own timestamp, so running the recorder twice on the same
snapshot is a no-op (INSERT OR IGNORE on the primary key), and a stale or
missing snapshot is skipped rather than recorded as zeros.

    python recorder.py            # one pass: record + hourly rollup + retention
    python recorder.py --stats    # print row counts and the recorded span
"""
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HISTORY_DB = Path(os.environ.get("HISTORY_DB", "/var/lib/monitor/history.db"))
RAW_RETENTION_DAYS = int(os.environ.get("HISTORY_RAW_DAYS", "90"))
ROLLUP_WINDOW = 48 * 3600     # re-derive hourly rows for the last 48h each pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts            INTEGER NOT NULL,   -- snapshot time (epoch seconds, from the pusher)
    cluster       TEXT    NOT NULL,
    gpu_type      TEXT    NOT NULL,
    total         INTEGER NOT NULL,
    alloc         INTEGER NOT NULL,
    free          INTEGER NOT NULL,
    nodes_free    INTEGER NOT NULL,   -- nodes of this type with at least one free card
    pending_jobs  INTEGER NOT NULL,   -- competing jobs asking for this type (held excluded)
    pending_gpus  INTEGER NOT NULL,   -- cards those jobs ask for
    pending_held  INTEGER NOT NULL,   -- jobs asking for this type that are held / not eligible
    PRIMARY KEY (ts, cluster, gpu_type)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_cluster_ts ON samples (cluster, ts);

CREATE TABLE IF NOT EXISTS cluster_samples (
    ts            INTEGER NOT NULL,
    cluster       TEXT    NOT NULL,
    running       INTEGER NOT NULL,
    pending       INTEGER NOT NULL,
    untyped_jobs  INTEGER NOT NULL,   -- pending jobs asking for a GPU of any type
    untyped_gpus  INTEGER NOT NULL,
    PRIMARY KEY (ts, cluster)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS hourly (
    hour_ts           INTEGER NOT NULL,   -- start of the hour (epoch seconds, UTC)
    cluster           TEXT    NOT NULL,
    gpu_type          TEXT    NOT NULL,
    avg_free          REAL    NOT NULL,
    min_free          INTEGER NOT NULL,
    max_free          INTEGER NOT NULL,
    p_any_free        REAL    NOT NULL,   -- share of samples with >= 1 free card
    avg_pending_gpus  REAL    NOT NULL,
    avg_total         REAL    NOT NULL,
    n                 INTEGER NOT NULL,   -- samples in the hour
    PRIMARY KEY (hour_ts, cluster, gpu_type)
) WITHOUT ROWID;
"""


def open_db(path=None):
    path = Path(path or HISTORY_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.executescript(SCHEMA)
    return conn


def record(conn, slug, data):
    """Insert one snapshot's aggregates. Returns the number of type rows written."""
    ts = int(data["generated_at"])
    conn.execute(
        "INSERT OR IGNORE INTO cluster_samples VALUES (?,?,?,?,?,?)",
        (ts, slug, data.get("running_jobs_total", 0), data.get("pending_jobs_total", 0),
         data.get("pending_untyped_jobs", 0), data.get("pending_untyped_gpus", 0)),
    )
    n = 0
    for g in data.get("gpu_summary", []):
        cur = conn.execute(
            "INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, slug, g["type"].lower(), g["total"], g["alloc"], g["total"] - g["alloc"],
             g.get("nodes_with_free", 0), g.get("pending_jobs", 0), g.get("pending_gpus", 0),
             g.get("pending_held", 0)),
        )
        n += cur.rowcount
    return n


def rollup(conn, now=None):
    """Re-derive hourly rows for the recent window and apply raw retention.

    Hours older than the window were rolled up by earlier passes, so they are
    complete by the time their raw samples age out.
    """
    now = int(now or time.time())
    hour_start = now - now % 3600
    since = hour_start - ROLLUP_WINDOW
    conn.execute(
        """INSERT OR REPLACE INTO hourly
           SELECT (ts / 3600) * 3600, cluster, gpu_type,
                  AVG(free), MIN(free), MAX(free), AVG(free > 0),
                  AVG(pending_gpus), AVG(total), COUNT(*)
           FROM samples WHERE ts >= ? GROUP BY (ts / 3600) * 3600, cluster, gpu_type""",
        (since,),
    )
    cutoff = now - RAW_RETENTION_DAYS * 86400
    conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM cluster_samples WHERE ts < ?", (cutoff,))


def stats(conn):
    out = {}
    for table in ("samples", "cluster_samples", "hourly"):
        col = "hour_ts" if table == "hourly" else "ts"
        n, lo, hi = conn.execute(f"SELECT COUNT(*), MIN({col}), MAX({col}) FROM {table}").fetchone()
        out[table] = {"rows": n,
                      "from": datetime.fromtimestamp(lo).isoformat(" ", "minutes") if lo else None,
                      "to": datetime.fromtimestamp(hi).isoformat(" ", "minutes") if hi else None}
    return out


def main(argv):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import app as monitor   # reads the same .env: CLUSTERS, snapshot paths, staleness

    conn = open_db()
    if "--stats" in argv:
        for table, s in stats(conn).items():
            print(f"{table:16s} {s['rows']:>8} rows   {s['from']}  ->  {s['to']}")
        return 0

    written = {}
    for slug, cfg in monitor.CLUSTERS.items():
        try:
            data = monitor.fetch_cluster(cfg)
        except Exception as e:                        # stale / missing snapshot: skip, never zeros
            print(f"{slug}: skipped ({str(e)[:120]})")
            continue
        written[slug] = record(conn, slug, data)
    rollup(conn)
    conn.commit()
    size_kb = HISTORY_DB.stat().st_size // 1024 if HISTORY_DB.exists() else 0
    print(f"{datetime.now().isoformat(' ', 'seconds')} recorded {written} ({size_kb} KB on disk)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
