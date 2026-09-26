#!/usr/bin/env python3
"""Yale cluster monitor - Flask dashboard with per-user login.

Renders a node-card view of the GPU partitions plus a queue panel for the
lab account, for one or more clusters (Misha, Bouchet, ...). Each cluster's
data comes from a snapshot file that a pusher job on that cluster drops on
this host over SSH (see misha-side/ and deploy/monitor-receive.sh).
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, get_flashed_messages, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).parent

# ---------- Configuration (env-driven) ----------
# DATA_SOURCE picks where each cluster's snapshot comes from:
#   "file" (default): read the snapshot dropped by that cluster's pusher (recommended).
#   "ssh":            this Flask host SSHes into the cluster directly (legacy / Yale-network host).
DATA_SOURCE = os.environ.get("DATA_SOURCE", "file").lower()
SNAPSHOT_MAX_AGE = int(os.environ.get("SNAPSHOT_MAX_AGE", "300"))  # seconds

LAB_ACCOUNT = os.environ.get("LAB_ACCOUNT", "")
LAB_NETIDS = [s.strip() for s in os.environ.get("LAB_NETIDS", "").split(",") if s.strip()]
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))
USERS_FILE = Path(os.environ.get("USERS_FILE", ROOT / "users.json"))
SECRET_FILE = ROOT / ".flask_secret"

# ---------- Clusters ----------
# CLUSTERS is the ordered list of cluster slugs to serve; the first one is the
# default that `/` and `/api/cluster` render. Every other cluster lives at
# `/<slug>` and `/api/cluster/<slug>`. Each cluster reads its settings from
# <SLUG>_HOST, <SLUG>_USER, <SLUG>_PARTITIONS, <SLUG>_SNAPSHOT_FILE,
# <SLUG>_LAB_ACCOUNT and <SLUG>_LABEL. Misha additionally honours the original
# single-cluster names (MISHA_HOST, SNAPSHOT_FILE, LAB_ACCOUNT, ...) so an
# existing .env keeps working unchanged.
CLUSTER_SLUGS = [s.strip().lower() for s in os.environ.get("CLUSTERS", "misha").split(",")
                 if s.strip()]
if not CLUSTER_SLUGS:
    CLUSTER_SLUGS = ["misha"]
if any(not re.fullmatch(r"[a-z0-9_-]+", s) for s in CLUSTER_SLUGS):
    raise SystemExit(f"CLUSTERS contains an invalid slug: {CLUSTER_SLUGS}")

_LEGACY_MISHA_VARS = {  # <SLUG>_<KEY> falls back to these for misha only
    "HOST": "MISHA_HOST",
    "USER": "MISHA_USER",
    "PARTITIONS": "MISHA_PARTITIONS",
    "SNAPSHOT_FILE": "SNAPSHOT_FILE",
}


def _cluster_config(slug):
    prefix = slug.upper().replace("-", "_")

    def env(key, default=""):
        v = os.environ.get(f"{prefix}_{key}")
        if v is None and slug == "misha" and key in _LEGACY_MISHA_VARS:
            v = os.environ.get(_LEGACY_MISHA_VARS[key])
        return default if v is None else v

    default_snapshot = ("/var/lib/monitor/snapshot.txt" if slug == "misha"
                        else f"/var/lib/monitor/{slug}/snapshot.txt")
    return {
        "slug": slug,
        "label": env("LABEL", slug.capitalize()),
        "host": env("HOST", f"{slug}.ycrc.yale.edu"),
        "user": env("USER", ""),                       # only when DATA_SOURCE=ssh
        "partitions": env("PARTITIONS", "gpu,gpu_devel"),
        "snapshot": Path(env("SNAPSHOT_FILE", default_snapshot)),
        # Per-cluster lab account; falls back to the global one (same lab, two clusters).
        "lab_account": env("LAB_ACCOUNT", LAB_ACCOUNT),
        "url": "/" if slug == CLUSTER_SLUGS[0] else f"/{slug}",
        "api_url": "/api/cluster" if slug == CLUSTER_SLUGS[0] else f"/api/cluster/{slug}",
        "history_url": "/history" if slug == CLUSTER_SLUGS[0] else f"/history/{slug}",
    }


CLUSTERS = {slug: _cluster_config(slug) for slug in CLUSTER_SLUGS}
DEFAULT_CLUSTER = CLUSTER_SLUGS[0]

app = Flask(__name__)


def _load_secret():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env.encode() if isinstance(env, str) else env
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    s = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(s)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return s


app.secret_key = _load_secret()
app.permanent_session_lifetime = timedelta(days=14)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
# Caddy terminates TLS one hop in front of gunicorn and forwards
# X-Forwarded-Proto/For/Host, so request.host_url (used to build invite
# links) says https and the public host instead of 127.0.0.1:5111.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ---------- Auth ----------
# users.json: {username: {"password": <werkzeug hash or null>, "display": str,
#                          "admin": true?, "invite": {"hash": sha256hex, "expires": epoch}?}}
# Accounts are created from the admin page (/admin) as an INVITE: a one-time,
# expiring link the person opens to set their own password. Only the sha256
# of the token is stored; the token exists in the link alone and is shown to
# the admin exactly once. Re-issuing a link for an existing user is the
# password reset. manage_users.py is the bootstrap/emergency path.

INVITE_TTL = int(os.environ.get("INVITE_TTL_SECONDS", str(7 * 24 * 3600)))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")   # e.g. https://qingyuchen-lab-monitor.org
MIN_PASSWORD_LEN = 10
USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")


def load_users():
    if not USERS_FILE.exists():
        return {}
    try:
        data = json.loads(USERS_FILE.read_text())
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}   # drops _comment keys


def save_users(users):
    """Atomic replace, so a crash mid-write never leaves an empty users.json."""
    tmp = USERS_FILE.with_name(USERS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(users, indent=2) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, USERS_FILE)


def current_user():
    """The signed-in user's record, or None if not signed in / since removed."""
    u = session.get("user")
    return load_users().get(u) if u else None


def is_admin():
    rec = current_user()
    return bool(rec and rec.get("admin"))


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def require_csrf():
    sent = request.form.get("csrf", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400)


@app.context_processor
def inject_auth():
    return {"csrf_token": csrf_token,
            "is_admin": is_admin() if "user" in session else False}


def _start_session(username, rec):
    session.clear()
    session.permanent = True
    session["user"] = username
    session["display"] = rec.get("display", username)
    csrf_token()


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        # A removed user is signed out at their next request: the session
        # cookie alone is not enough, the record must still exist.
        if "user" not in session or current_user() is None:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if "user" not in session or current_user() is None:
            session.clear()
            return redirect(url_for("login", next=request.path))
        if not is_admin():
            abort(404)      # non-admins get no hint that the page exists
        return view(*a, **kw)
    return wrapper


def password_problem(p1, p2):
    if len(p1) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if p1 != p2:
        return "Passwords do not match."
    return None


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_invite(users, username, display=None):
    """Create the user if new, attach a fresh invite, return the raw token."""
    token = secrets.token_urlsafe(32)
    rec = users.get(username)
    if rec is None:
        rec = users[username] = {"password": None, "display": display or username}
    elif display:
        rec["display"] = display
    rec["invite"] = {"hash": _token_hash(token), "expires": int(time.time()) + INVITE_TTL}
    return token


def find_invite(token):
    """(username, record) for a live invite token, else None."""
    h = _token_hash(token)
    now = time.time()
    for username, rec in load_users().items():
        inv = rec.get("invite")
        if inv and hmac.compare_digest(inv.get("hash", ""), h):
            return (username, rec) if inv.get("expires", 0) > now else None
    return None


def invite_link(token):
    base = PUBLIC_URL or request.host_url.rstrip("/")
    return f"{base}/invite/{token}"


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session and current_user() is not None:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        users = load_users()
        u = request.form.get("username", "").strip().lower()
        p = request.form.get("password", "")
        rec = users.get(u)
        if rec and rec.get("password") and check_password_hash(rec["password"], p):
            _start_session(u, rec)
            nxt = request.args.get("next") or url_for("index")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("index")
            return redirect(nxt)
        error = "Invalid username or password."
    return render_template("login.html", error=error,
                           cluster_labels=[c["label"] for c in CLUSTERS.values()])


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/invite/<token>", methods=["GET", "POST"])
def invite(token):
    found = find_invite(token)
    if not found:
        return render_template("invite.html", invalid=True, ttl_days=INVITE_TTL // 86400), 404
    username, rec = found
    error = None
    if request.method == "POST":
        p1 = request.form.get("new_password", "")
        p2 = request.form.get("confirm", "")
        error = password_problem(p1, p2)
        if not error:
            users = load_users()
            rec = users[username]
            rec["password"] = generate_password_hash(p1)
            rec.pop("invite", None)          # one use
            save_users(users)
            _start_session(username, rec)
            return redirect(url_for("index"))
    return render_template("invite.html", username=username, display=rec.get("display"),
                           error=error, min_len=MIN_PASSWORD_LEN)


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    error = message = None
    if request.method == "POST":
        require_csrf()
        users = load_users()
        rec = users[session["user"]]
        current = request.form.get("current", "")
        p1 = request.form.get("new_password", "")
        p2 = request.form.get("confirm", "")
        if not (rec.get("password") and check_password_hash(rec["password"], current)):
            error = "Current password is incorrect."
        else:
            error = password_problem(p1, p2)
            if not error and p1 == current:
                error = "The new password must differ from the current one."
        if not error:
            rec["password"] = generate_password_hash(p1)
            rec.pop("invite", None)
            save_users(users)
            message = "Password changed."
    return render_template("account.html", user=session["user"],
                           display=session.get("display", session["user"]),
                           clusters=list(CLUSTERS.values()),
                           error=error, message=message, min_len=MIN_PASSWORD_LEN)


def server_status():
    """Droplet health for the admin page: disk, memory, load, and the data
    files that grow (so the admin can judge when to resize). Every probe is
    optional — on a non-Linux dev box the /proc reads simply yield None."""
    import shutil
    now = time.time()
    st = {"disk": None, "mem": None, "load": None, "uptime_days": None,
          "cpus": os.cpu_count(), "files": []}
    try:
        du = shutil.disk_usage("/")
        st["disk"] = {"total_gb": du.total / 2**30, "free_gb": du.free / 2**30,
                      "used_pct": 100.0 * (du.total - du.free) / du.total}
    except OSError:
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])
        st["mem"] = {"total_mb": info["MemTotal"] / 1024, "available_mb": info["MemAvailable"] / 1024,
                     "used_pct": 100.0 * (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"]}
    except (OSError, KeyError, ValueError):
        pass
    try:
        st["load"] = os.getloadavg()
    except (OSError, AttributeError):
        pass
    try:
        with open("/proc/uptime") as f:
            st["uptime_days"] = float(f.read().split()[0]) / 86400
    except (OSError, ValueError):
        pass
    candidates = [("GPU history (history.db)", os.environ.get("HISTORY_DB", "/var/lib/monitor/history.db")),
                  ("Azure usage (usage.db)", os.environ.get("AZURE_USAGE_DB", ""))]
    candidates += [(f"{c['label']} snapshot", str(c["snapshot"])) for c in CLUSTERS.values()]
    for label, path in candidates:
        if not path:
            continue
        p = Path(path)
        if p.exists():
            s = p.stat()
            st["files"].append({"label": label, "path": str(p), "mb": s.st_size / 2**20,
                                "age_s": int(now - s.st_mtime)})
    return st


@app.route("/admin")
@admin_required
def admin():
    now = time.time()
    rows = []
    for username, rec in sorted(load_users().items()):
        inv = rec.get("invite")
        status = None
        if inv:
            status = "pending" if inv.get("expires", 0) > now else "expired"
        rows.append({
            "username": username,
            "display": rec.get("display", username),
            "admin": bool(rec.get("admin")),
            "has_password": bool(rec.get("password")),
            "invite": status,
        })
    return render_template("admin.html", rows=rows, user=session["user"],
                           server=server_status(),
                           ttl_days=INVITE_TTL // 86400,
                           new_invite=session.pop("new_invite", None),
                           messages=get_flashed_messages(with_categories=True))


@app.route("/admin/invite", methods=["POST"])
@admin_required
def admin_invite():
    require_csrf()
    username = request.form.get("username", "").strip().lower()
    display = request.form.get("display", "").strip()[:64]
    if not USERNAME_RE.fullmatch(username):
        flash("Username must be 1-32 characters of letters, digits, . _ - (a NetID like abc123).", "error")
        return redirect(url_for("admin"))
    users = load_users()
    token = issue_invite(users, username, display or None)
    save_users(users)
    # Shown once, on the next GET, then forgotten: the link is never stored.
    session["new_invite"] = {"user": username, "link": invite_link(token)}
    return redirect(url_for("admin"))


@app.route("/admin/remove", methods=["POST"])
@admin_required
def admin_remove():
    require_csrf()
    username = request.form.get("username", "").strip().lower()
    users = load_users()
    if username == session["user"]:
        flash("You cannot remove your own account.", "error")
    elif username not in users:
        flash(f"No such user: {username}", "error")
    else:
        del users[username]
        save_users(users)
        flash(f"Removed {username}.", "ok")
    return redirect(url_for("admin"))


# ---------- SLURM polling ----------

SINFO_FMT = ("Partition:25,NodeHost:30,CPUsState:20,AllocMem:14,Memory:14,"
             "Gres:50,GresUsed:80,StateLong:18")
SQUEUE_FMT_R = ("NodeList:60,JobID:15,UserName:15,Account:25,"
                "TimeUsed:15,TimeLimit:15,TimeLeft:15,EndTime:22,"
                "tres-alloc:120,Name:60")
SQUEUE_FMT_PD = ("JobID:15,UserName:15,Account:25,Partition:15,Reason:25,"
                 "TimeLimit:15,StartTime:22,tres-alloc:120,Name:60")

GRES_RE = re.compile(r"gpu:([^:\s]+):(\d+)")
# Pending-job reasons that mean "not eligible to start", whatever is free.
# Such jobs are not competing for a card, so demand counts exclude them
# (and shows them as "held"). Everything else — Resources, Priority, QOS and
# account limits — is a job that will take a card as soon as it may.
HELD_REASONS = {"JobHeldUser", "JobHeldAdmin", "Dependency", "DependencyNeverSatisfied",
                "BeginTime", "JobArrayTaskLimit", "Reservation"}
# Pending-job reasons that mean "this user/account is at a quota": the job will
# take a card once its own earlier jobs finish, so free hardware does not help
# it and it does not compete for the card being released right now.
QUOTA_REASON_RE = re.compile(r"^(QOS|Max|Assoc|Grp)|Limit")
# Node states in which SLURM will not place a job, so unallocated cards on such
# a node are not free: an admin drained r817u29n06 and its 4 idle H100s read as
# "4 free" for a day. sinfo suffixes the state with markers (drained*, mixed-);
# parse_sinfo strips them so these names match.
UNAVAILABLE_STATES = {"down", "drain", "drained", "draining", "maint", "fail", "failing",
                      "unknown", "inval", "future"}
STATE_MARKERS = "*~#!%@$-+^"
TRES_GPU_RE = re.compile(r"gres/gpu(?::([^=]+))?=(\d+)")
TRES_CPU_RE = re.compile(r"\bcpu=(\d+)")
TRES_MEM_RE = re.compile(r"\bmem=([\d.]+)([KMGT])")


def ssh_run(cluster, remote_cmd, timeout=20):
    target = f"{cluster['user']}@{cluster['host']}" if cluster["user"] else cluster["host"]
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new", target, remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(f"ssh: {result.stderr.strip()[:200]}")
    return result.stdout


def expand_nodelist(s):
    """Expand SLURM hostlist syntax. Handles 'a,b', 'pre[01,03-05]suf', combos."""
    s = s.strip()
    if not s:
        return []
    out = []
    parts = []
    depth = 0
    cur = ""
    for ch in s:
        if ch == "[":
            depth += 1
            cur += ch
        elif ch == "]":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    for p in parts:
        if "[" not in p:
            out.append(p)
            continue
        prefix, rest = p.split("[", 1)
        body, suffix = rest.split("]", 1)
        for token in body.split(","):
            token = token.strip()
            if "-" in token and token.replace("-", "").isdigit():
                lo, hi = token.split("-")
                width = len(lo)
                for i in range(int(lo), int(hi) + 1):
                    out.append(f"{prefix}{i:0{width}d}{suffix}")
            else:
                out.append(f"{prefix}{token}{suffix}")
    return out


def parse_tres(tres):
    cpus = int(TRES_CPU_RE.search(tres).group(1)) if TRES_CPU_RE.search(tres) else 0
    gpus = 0
    gpu_type = None
    # SLURM lists the generic entry before the typed one
    # ("gres/gpu=1,gres/gpu:h200=1"), so the first match alone would report
    # every job as untyped. Prefer the typed entry when there is one.
    for m in TRES_GPU_RE.finditer(tres):
        gpus = int(m.group(2))
        if m.group(1):
            gpu_type = m.group(1).lower()
            break
    mem_mb = 0
    m = TRES_MEM_RE.search(tres)
    if m:
        v = float(m.group(1))
        unit = m.group(2)
        mem_mb = int(v * {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit])
    return cpus, gpus, gpu_type, mem_mb


def parse_sinfo(raw):
    """Parse sinfo -N output. With Partition: in the format, sinfo emits one
    line per (partition, node) pair, so the same physical node appears once
    per partition it's a member of. We dedupe by host and accumulate the
    partitions list per node — first partition seen is the 'primary' (used
    for UI grouping)."""
    by_host = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        cols = line.split()
        if len(cols) < 8:
            continue
        partition, host, cpustate, alloc_mem, total_mem, gres, gres_used, state = cols[:8]
        # Partition column may have a trailing '*' marking the cluster default
        partition = partition.rstrip("*")
        try:
            a, _i, _o, t = (int(x) for x in cpustate.split("/"))
        except ValueError:
            continue
        gtype, gtotal, gused = None, 0, 0
        m = GRES_RE.search(gres)
        if m:
            gtype, gtotal = m.group(1), int(m.group(2))
        m = GRES_RE.search(gres_used)
        if m:
            gused = int(m.group(2))
        existing = by_host.get(host)
        if existing:
            if partition not in existing["partitions"]:
                existing["partitions"].append(partition)
            continue
        by_host[host] = {
            "host": host,
            "partitions": [partition],
            "cpu_alloc": a, "cpu_total": t,
            "mem_alloc_mb": int(alloc_mem) if alloc_mem.isdigit() else 0,
            "mem_total_mb": int(total_mem) if total_mem.isdigit() else 0,
            "gpu_type": gtype,
            "gpu_alloc": gused,
            "gpu_total": gtotal,
            "state": state.lower().rstrip(STATE_MARKERS),
            "state_raw": state,
            "jobs": [],
        }
    return list(by_host.values())


def parse_squeue_running(raw):
    by_node = {}
    all_jobs = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        # Columns now (10): NodeList JobID User Account TimeUsed TimeLimit
        #                   TimeLeft EndTime tres-alloc Name...
        cols = line.split(None, 9)
        if len(cols) < 9:
            continue
        (nodelist, jobid, user, account, used, limit,
         time_left, end_time, tres) = cols[:9]
        name = cols[9].strip() if len(cols) > 9 else ""
        cpus, gpus, gpu_type, mem_mb = parse_tres(tres)
        job = {
            "jobid": jobid, "user": user, "account": account,
            "time_used": used, "time_limit": limit,
            "time_left": time_left, "end_time": end_time,
            "cpus": cpus, "mem_mb": mem_mb, "gpus": gpus, "gpu_type": gpu_type,
            "name": name, "nodelist": nodelist, "state": "R",
        }
        all_jobs.append(job)
        for node in expand_nodelist(nodelist):
            by_node.setdefault(node, []).append(job)
    return by_node, all_jobs


def _fmt_widths(fmt):
    """Column widths of a `squeue -O` / `sinfo -O` format string ("JobID:15,...")."""
    return [int(f.rsplit(":", 1)[1]) for f in fmt.split(",")]


_PD_WIDTHS = _fmt_widths(SQUEUE_FMT_PD)


def _split_pending(line):
    """Split a pending-job line into its 9 columns.

    `squeue -O` pads every column to its declared width, so slice by width:
    a whitespace split shifts every later column when the Reason is empty
    (a job SLURM has not evaluated yet) or contains a space
    ("ReqNodeNotAvail, UnavailableNodes:..."), which made such jobs parse
    with the wrong TRES and drop out of the demand counts. Falls back to a
    whitespace split for a line that is not the padded width.
    """
    if len(line) >= sum(_PD_WIDTHS[:-1]):
        cols, pos = [], 0
        for w in _PD_WIDTHS:
            cols.append(line[pos:pos + w].strip())
            pos += w
        if cols[0].split("_")[0].isdigit():      # sanity: JobID (array ids are 123_4)
            return cols
    return line.split(None, 8)


def parse_squeue_pending(raw):
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        # Columns (9): JobID User Account Partition Reason TimeLimit
        #              StartTime tres-alloc Name...
        cols = _split_pending(line)
        if len(cols) < 8:
            continue
        (jobid, user, account, partition, reason, limit,
         start_time, tres) = cols[:8]
        name = cols[8].strip() if len(cols) > 8 else ""
        if not reason:
            reason = "None"
        cpus, gpus, gpu_type, mem_mb = parse_tres(tres)
        out.append({
            "jobid": jobid, "user": user, "account": account,
            "partition": partition, "reason": reason, "time_limit": limit,
            "start_time": start_time,
            "cpus": cpus, "mem_mb": mem_mb, "gpus": gpus, "gpu_type": gpu_type,
            "name": name, "state": "PD",
        })
    return out


def fetch_via_ssh(cluster):
    parts = cluster["partitions"]
    sinfo_cmd = f"sinfo -h -p {parts} -N -O '{SINFO_FMT}'"
    squeue_r_cmd = f"squeue -h -p {parts} -t R -O '{SQUEUE_FMT_R}'"
    squeue_pd_cmd = f"squeue -h -p {parts} -t PD -O '{SQUEUE_FMT_PD}'"
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_sinfo = pool.submit(ssh_run, cluster, sinfo_cmd)
        f_squeue_r = pool.submit(ssh_run, cluster, squeue_r_cmd)
        f_squeue_pd = pool.submit(ssh_run, cluster, squeue_pd_cmd)
        return f_sinfo.result(), f_squeue_r.result(), f_squeue_pd.result(), None


def fetch_via_file(cluster):
    """Read the snapshot dropped by the cluster's pusher and split into
    its three sections. Raises if the file is missing or stale."""
    snapshot = cluster["snapshot"]
    if not snapshot.exists():
        raise RuntimeError(f"snapshot file missing at {snapshot} — pusher running?")
    age = time.time() - snapshot.stat().st_mtime
    if age > SNAPSHOT_MAX_AGE:
        raise RuntimeError(f"snapshot is {int(age)}s old (limit {SNAPSHOT_MAX_AGE}s) — pusher down?")
    raw = snapshot.read_text(errors="replace")

    sections, current, buf = {}, None, []
    for line in raw.splitlines():
        if line.startswith("===") and line.endswith("==="):
            if current is not None:
                sections[current] = "\n".join(buf)
            current = line.strip("= ")
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf)

    meta = {}
    for line in sections.get("META", "").splitlines():
        if " " in line:
            k, _, v = line.partition(" ")
            meta[k] = v
    pusher_ts = float(meta.get("generated_at", time.time()))
    return (
        sections.get("SINFO", ""),
        sections.get("SQUEUE_R", ""),
        sections.get("SQUEUE_PD", ""),
        pusher_ts,
    )


def fetch_cluster(cluster):
    if DATA_SOURCE == "file":
        sinfo_raw, squeue_r_raw, squeue_pd_raw, pusher_ts = fetch_via_file(cluster)
    else:
        sinfo_raw, squeue_r_raw, squeue_pd_raw, pusher_ts = fetch_via_ssh(cluster)
    lab_account = cluster["lab_account"]

    nodes = parse_sinfo(sinfo_raw)
    by_node, running = parse_squeue_running(squeue_r_raw)
    pending = parse_squeue_pending(squeue_pd_raw)

    for n in nodes:
        n["jobs"] = by_node.get(n["host"], [])
        # Earliest end time among GPU-using jobs on this node — i.e. when the
        # next GPU on this node is guaranteed to free up (jobs may end sooner).
        gpu_ends = [j["end_time"] for j in n["jobs"]
                    if j.get("gpus", 0) > 0 and j.get("end_time")
                    and j["end_time"] not in ("N/A", "Unknown", "")]
        n["next_gpu_free_at"] = min(gpu_ends) if gpu_ends else None

    # Per-GPU-type rollup
    gpu_summary = {}
    for n in nodes:
        if not n["gpu_type"]:
            continue
        d = gpu_summary.setdefault(n["gpu_type"], {
            "type": n["gpu_type"], "total": 0, "alloc": 0,
            "nodes_total": 0, "nodes_with_free": 0,
        })
        d["total"] += n["gpu_total"]
        d["alloc"] += n["gpu_alloc"]
        d["nodes_total"] += 1
        unavailable = n["state"] in UNAVAILABLE_STATES
        idle_cards = n["gpu_total"] - n["gpu_alloc"]
        if unavailable:
            # Cards SLURM cannot hand out: not allocated, but not free either.
            d["unavailable"] = d.get("unavailable", 0) + idle_cards
        elif idle_cards > 0:
            d["nodes_with_free"] += 1
            # A free card is only usable if the node still has a CPU and some
            # memory to give the job: a card on a node with 48/48 CPUs allocated
            # is free on paper and unschedulable in practice.
            cpu_idle = n["cpu_total"] - n["cpu_alloc"]
            mem_free_mb = n["mem_total_mb"] - n["mem_alloc_mb"]
            if cpu_idle > 0 and mem_free_mb >= 1024:
                d["usable"] = d.get("usable", 0) + idle_cards
        # Track earliest GPU-job end among nodes of this type that are full.
        if n["gpu_alloc"] >= n["gpu_total"] and n.get("next_gpu_free_at"):
            cur = d.get("soonest_free_at")
            if cur is None or n["next_gpu_free_at"] < cur:
                d["soonest_free_at"] = n["next_gpu_free_at"]
    # Demand: what the pending queue is asking for, per GPU type. A release
    # time alone misleads on a busy cluster (the freed card goes to the head
    # of this queue, not to whoever is looking at the page), so each chip
    # also shows how many jobs/cards are already waiting for that type.
    # Requests with no type (gres/gpu=N, "any card") can land on any type;
    # they are reported once, cluster-wide, rather than added to every chip.
    pending_untyped_jobs = pending_untyped_gpus = pending_held_jobs = pending_quota_jobs = 0
    for j in pending:
        if j.get("gpus", 0) <= 0:
            continue
        t = (j.get("gpu_type") or "").lower()
        reason = (j.get("reason") or "").split("(")[0]
        if reason in HELD_REASONS:
            pending_held_jobs += 1
            if t and t in gpu_summary:
                gpu_summary[t]["pending_held"] = gpu_summary[t].get("pending_held", 0) + 1
            continue
        if QUOTA_REASON_RE.search(reason):
            pending_quota_jobs += 1
            if t and t in gpu_summary:
                gpu_summary[t]["pending_quota"] = gpu_summary[t].get("pending_quota", 0) + 1
            continue
        if t and t in gpu_summary:
            gpu_summary[t]["pending_jobs"] = gpu_summary[t].get("pending_jobs", 0) + 1
            gpu_summary[t]["pending_gpus"] = gpu_summary[t].get("pending_gpus", 0) + j["gpus"]
        else:
            pending_untyped_jobs += 1
            pending_untyped_gpus += j["gpus"]
    for d in gpu_summary.values():
        d.setdefault("pending_jobs", 0)
        d.setdefault("pending_gpus", 0)
        d.setdefault("pending_held", 0)
        d.setdefault("pending_quota", 0)
        d.setdefault("unavailable", 0)
        d["free"] = max(0, d["total"] - d["alloc"] - d["unavailable"])
        d["usable"] = min(d.get("usable", 0), d["free"])
    gpu_summary_list = sorted(gpu_summary.values(), key=lambda d: d["type"])

    lab_running = [j for j in running if lab_account and j["account"] == lab_account]
    lab_pending = [j for j in pending if lab_account and j["account"] == lab_account]

    # Per-user GPU occupancy across all running jobs (so labmates can see
    # who's holding which cards and reach out).
    by_user = {}
    for j in running:
        if j.get("gpus", 0) <= 0:
            continue
        u = by_user.setdefault(j["user"], {
            "user": j["user"], "accounts": set(), "jobs": 0, "gpus": 0,
            "gpu_types": set(), "nodes": set(), "soonest_end": None,
        })
        u["accounts"].add(j["account"])
        u["jobs"] += 1
        u["gpus"] += j["gpus"]
        if j.get("gpu_type"):
            u["gpu_types"].add(j["gpu_type"])
        for nd in expand_nodelist(j.get("nodelist", "")):
            u["nodes"].add(nd)
        et = j.get("end_time")
        if et and et not in ("N/A", "Unknown", ""):
            if u["soonest_end"] is None or et < u["soonest_end"]:
                u["soonest_end"] = et
    gpu_users = sorted(
        ({
            "user": u["user"],
            "accounts": sorted(u["accounts"]),
            "jobs": u["jobs"],
            "gpus": u["gpus"],
            "gpu_types": sorted(u["gpu_types"]),
            "nodes": sorted(u["nodes"]),
            "soonest_end": u["soonest_end"],
        } for u in by_user.values()),
        key=lambda d: d["gpus"], reverse=True,
    )

    return {
        "generated_at": pusher_ts or time.time(),
        "data_source": DATA_SOURCE,
        "cluster": cluster["slug"],
        "partitions": cluster["partitions"].split(","),
        "nodes": nodes,
        "gpu_summary": gpu_summary_list,
        "pending_untyped_jobs": pending_untyped_jobs,
        "pending_untyped_gpus": pending_untyped_gpus,
        "pending_held_jobs": pending_held_jobs,
        "pending_quota_jobs": pending_quota_jobs,
        "running_jobs_total": len(running),
        "pending_jobs_total": len(pending),
        "lab_running": lab_running,
        "lab_pending": lab_pending,
        "lab_account": lab_account,
        "lab_netids": LAB_NETIDS,
        "gpu_users": gpu_users,
        "email_domain": os.environ.get("EMAIL_DOMAIN", "yale.edu"),
    }


# One cache entry per cluster: each pusher has its own cadence, and a failure
# on one cluster must not mark the other one stale.
_cache = {slug: {"data": None, "ts": 0.0, "error": None} for slug in CLUSTERS}


def get_data(cluster):
    c = _cache[cluster["slug"]]
    now = time.time()
    if c["data"] and (now - c["ts"]) < CACHE_TTL and not c["error"]:
        return c["data"]
    try:
        data = fetch_cluster(cluster)
        c["data"] = data
        c["ts"] = now
        c["error"] = None
        return data
    except Exception as e:
        msg = str(e)[:240]
        if c["data"]:
            stale = dict(c["data"])
            stale["stale"] = True
            stale["error"] = msg
            stale["age_seconds"] = int(now - c["ts"])
            return stale
        return {"error": msg, "cluster": cluster["slug"], "nodes": [], "gpu_summary": [],
                "lab_running": [], "lab_pending": [], "running_jobs_total": 0,
                "pending_jobs_total": 0, "lab_account": cluster["lab_account"],
                "lab_netids": LAB_NETIDS, "generated_at": now}


# ---------- Routes ----------

def _render_cluster(cluster):
    return render_template(
        "index.html",
        display=session.get("display", session.get("user", "")),
        user=session.get("user", ""),
        cluster=cluster,
        clusters=list(CLUSTERS.values()),
        partitions=cluster["partitions"],
        lab_account=cluster["lab_account"],
    )


@app.route("/")
@login_required
def index():
    return _render_cluster(CLUSTERS[DEFAULT_CLUSTER])


@login_required
def _cluster_page(slug):
    return _render_cluster(CLUSTERS[slug])


# Static routes (/azure, /login, ...) outrank this converter rule in Werkzeug's
# matching order, so `/<slug>` only sees paths nothing else claimed. Unknown
# slugs 404 before the login redirect so stray requests don't bounce to /login.
@app.route("/<slug>")
def cluster_page(slug):
    if slug not in CLUSTERS:
        abort(404)
    return _cluster_page(slug)


@app.route("/api/cluster")
@login_required
def api_cluster():
    return jsonify(get_data(CLUSTERS[DEFAULT_CLUSTER]))


@app.route("/api/cluster/<slug>")
@login_required
def api_cluster_named(slug):
    if slug not in CLUSTERS:
        return jsonify({"error": f"unknown cluster {slug!r}"}), 404
    return jsonify(get_data(CLUSTERS[slug]))


def _render_history(cluster):
    try:
        from history import build_history
        ctx = build_history(cluster["slug"], days=request.args.get("days", 30, type=int))
    except Exception as e:
        ctx = {"error": f"Failed to build history: {e}", "days": 30, "ranges": [7, 30, 90],
               "types": [], "span": None}
    return render_template(
        "history.html",
        ctx=ctx,
        cluster=cluster,
        clusters=list(CLUSTERS.values()),
        display=session.get("display", session.get("user", "")),
        user=session.get("user", ""),
    )


@app.route("/history")
@login_required
def history():
    return _render_history(CLUSTERS[DEFAULT_CLUSTER])


@app.route("/history/<slug>")
def history_named(slug):
    if slug not in CLUSTERS:
        abort(404)
    return _history_named(slug)


@login_required
def _history_named(slug):
    return _render_history(CLUSTERS[slug])


@app.route("/azure")
@login_required
def azure():
    # Lazy import: keeps Misha Monitor working even if plotly isn't installed.
    try:
        from azure_dashboard import build_context
        ctx = build_context()
    except ImportError as e:
        ctx = {"error": f"Azure dashboard module not available: {e}. "
                        "Run: pip install plotly"}
    except Exception as e:
        ctx = {"error": f"Failed to build dashboard: {e}"}
    return render_template(
        "azure.html",
        ctx=ctx,
        display=session.get("display", session.get("user", "")),
        user=session.get("user", ""),
    )


@app.route("/healthz")
def healthz():
    now = time.time()
    return jsonify({
        "ok": True,
        "clusters": {slug: {"cache_age_seconds": int(now - c["ts"]) if c["ts"] else None}
                     for slug, c in _cache.items()},
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5111"))
    bind = os.environ.get("BIND", "127.0.0.1")  # tunnel-safe default; set 0.0.0.0 to expose
    app.run(host=bind, port=port, debug=False)
