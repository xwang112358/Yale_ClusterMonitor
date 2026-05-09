# Misha Monitor — Deployment History

A chronological log of the actual deployment performed on **2026-05-09**.
Companion to [DEPLOY.md](DEPLOY.md): DEPLOY.md is the worksheet template;
this file records what was actually run, the outputs seen, and any
deviations from the template.

---

## Pre-flight values (final)

| Field | Value |
|---|---|
| Droplet IP | `159.223.173.141` |
| Droplet SSH user | `root` |
| Dashboard hostname (DNS A record) | `mishamonitor.duckdns.org` (DuckDNS, free subdomain) |
| Yale netid | `xw532` |
| SLURM account | `q_chen` |
| Partitions monitored | `gpu,gpu_devel` (default) |
| Misha-side install path | `~/project/cluster_monitor` (deviation — DEPLOY.md uses `~/cluster_monitor`) |
| First dashboard user | `allen` (display: "Allen Wang") |

---

## Phase D — Droplet setup

### D0 — Reconnect after droplet rebuild

The droplet was restored to base image partway through, which rotated its
host key and broke SSH. Fixed locally:

```powershell
ssh-keygen -R 159.223.173.141
ssh root@159.223.173.141   # accept new fingerprint: SHA256:2VF+oZrVFjFAH/o11WPxDbdMnZ7C+eFtrqEu5n15k0g
```

### D1 — Install dependencies + create monitor user

On the droplet as root:

```bash
apt update
apt install -y caddy python3-venv git
useradd -m -s /bin/bash monitor
mkdir -p /var/lib/monitor
chown monitor:monitor /var/lib/monitor
chmod 755 /var/lib/monitor
```

Verified:
- `monitor` user created (uid 1001).
- `/var/lib/monitor` is `drwxr-xr-x monitor:monitor`.
- `caddy version` → `2.6.2`.

### D2 — Clone repo + install receive script + .ssh skeleton

GitHub authentication note: the repo was private at first, so the initial
HTTPS clone failed (`Password authentication is not supported for Git
operations`). The user added a deploy key on GitHub, after which the
HTTPS clone succeeded without auth (the repo was opened up / a PAT was
established — the clone then "just worked").

```bash
sudo -u monitor git clone https://github.com/xwang112358/Yale_ClusterMonitor.git \
    /home/monitor/ClusterMonitor

cp /home/monitor/ClusterMonitor/deploy/monitor-receive.sh /usr/local/bin/
chmod 755 /usr/local/bin/monitor-receive.sh

mkdir -p /home/monitor/.ssh
chmod 700 /home/monitor/.ssh
touch /home/monitor/.ssh/authorized_keys
chmod 600 /home/monitor/.ssh/authorized_keys
chown -R monitor:monitor /home/monitor/.ssh
```

Verified perms on `monitor-receive.sh`, `~/.ssh/`, `authorized_keys`,
and the cloned repo contents.

### D3 — DNS

Registered `mishamonitor.duckdns.org` at https://www.duckdns.org and set
the A record to `159.223.173.141` (no AAAA / IPv6).

Verified on the droplet:

```bash
getent hosts mishamonitor.duckdns.org
# → 159.223.173.141 mishamonitor.duckdns.org
```

### D4 — venv, .env, first dashboard user

```bash
sudo -u monitor bash -lc '
  set -e
  cd ~/ClusterMonitor
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  cp .env.example .env
  chmod 600 .env
'
python3 -c "import secrets; print(secrets.token_hex(32))"
# (paste output into SECRET_KEY in /home/monitor/ClusterMonitor/.env)

sudo -u monitor nano /home/monitor/ClusterMonitor/.env
```

Final `.env` values (with the rest left at example defaults):

```ini
SECRET_KEY=<64-char hex>
DATA_SOURCE=file
SNAPSHOT_FILE=/var/lib/monitor/snapshot.txt
SNAPSHOT_MAX_AGE=300
LAB_ACCOUNT=q_chen
LAB_NETIDS=xw532
EMAIL_DOMAIN=yale.edu
MISHA_PARTITIONS=gpu,gpu_devel
BIND=127.0.0.1
PORT=5111
USERS_FILE=/home/monitor/ClusterMonitor/users.json
```

First dashboard user:

```bash
sudo -u monitor /home/monitor/ClusterMonitor/.venv/bin/python \
    /home/monitor/ClusterMonitor/manage_users.py add allen --display "Allen Wang"
```

Verified Flask 3.1.3 importable, `.env` and `users.json` both `0600
monitor:monitor`.

### D5 — Flask systemd service

Deviation from DEPLOY.md: rendered the unit via `sed | >` instead of
`sed -i` so the cloned repo file isn't dirtied (future `git pull` stays
clean).

```bash
sed 's/REPLACE_ME_USERNAME/monitor/g' \
    /home/monitor/ClusterMonitor/deploy/misha-monitor.service \
    > /etc/systemd/system/misha-monitor.service

systemctl daemon-reload
systemctl enable --now misha-monitor.service
systemctl status misha-monitor.service --no-pager
```

Verified:
- Status: `active (running)`, gunicorn pid live.
- `ss -tlnp | grep 5111` → `LISTEN 127.0.0.1:5111` owned by gunicorn.
- `curl -sI http://127.0.0.1:5111/` → `HTTP/1.1 302 FOUND`,
  `Location: /login?next=/`.

### D6 — Caddy + HTTPS

Same template-rendering tweak as D5:

```bash
sed 's#misha\.example\.com#mishamonitor.duckdns.org#g' \
    /home/monitor/ClusterMonitor/deploy/Caddyfile \
    > /etc/caddy/Caddyfile

mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy

systemctl reload caddy
journalctl -u caddy -n 50 --no-pager
```

Caddy obtained a Let's Encrypt cert via TLS-ALPN-01 in ~5 seconds.
Two warnings observed and dismissed as cosmetic:
- `Caddyfile input is not formatted` — whitespace-only.
- `no OCSP server specified in certificate` — Let's Encrypt no longer
  publishes OCSP info; harmless.

Verified from the droplet:

```bash
curl -sI https://mishamonitor.duckdns.org/
# HTTP/2 302
# location: /login?next=/
# server: Caddy
# server: gunicorn
```

Verified in browser: login page loaded, signed in as `allen`, dashboard
loaded with the expected "snapshot file missing" banner (no Misha pusher
yet).

---

## Phase M — Misha-side setup

### M1 — Get the pusher onto Misha

Yale Duo 2FA on every SSH/scp made the worksheet's three-step flow
painful. Switched to a single-session approach: one SSH (one Duo prompt),
and clone the (now public) repo on Misha itself.

```bash
ssh xw532@misha.ycrc.yale.edu     # Duo
# on Misha:
cd /tmp
git clone https://github.com/xwang112358/Yale_ClusterMonitor.git
mkdir -p ~/project/cluster_monitor
cp -r Yale_ClusterMonitor/misha-side/. ~/project/cluster_monitor/
rm -rf /tmp/Yale_ClusterMonitor
ls -la ~/project/cluster_monitor
# → pusher.sbatch (3953 B), setup.md (5646 B)
```

(In practice the directory was already populated from an earlier scp
attempt; the conditional `git clone` in the shell snippet was a no-op.)

### M2 — Generate push SSH key on Misha

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_monitor -C "misha-monitor-pusher"
cat ~/.ssh/id_ed25519_monitor.pub
# → ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHcO4BrSnRtd9vXnJnOqtNbEmpA2ZW6KHzN3pSAf0h0x misha-monitor-pusher
```

Pasted to droplet's `monitor` authorized_keys with the lockdown prefix:

```bash
sudo -u monitor tee -a /home/monitor/.ssh/authorized_keys <<'EOF'
command="/usr/local/bin/monitor-receive.sh",no-pty,no-X11-forwarding,no-agent-forwarding,no-port-forwarding ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHcO4BrSnRtd9vXnJnOqtNbEmpA2ZW6KHzN3pSAf0h0x misha-monitor-pusher
EOF
```

Verified: `authorized_keys` is one line, 210 bytes, `0600 monitor:monitor`.

### M3 — Configure pusher.sbatch

```bash
cd ~/project/cluster_monitor
sed -i 's#DROPLET_HOST:-203\.0\.113\.10#DROPLET_HOST:-159.223.173.141#' pusher.sbatch
grep -n 'DROPLET_HOST\|DROPLET_USER\|SSH_KEY\|PARTITIONS\|INTERVAL' pusher.sbatch
```

Final config in `pusher.sbatch`:
```
DROPLET_USER="${DROPLET_USER:-monitor}"
DROPLET_HOST="${DROPLET_HOST:-159.223.173.141}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_monitor}"
PARTITIONS="${PARTITIONS:-gpu,gpu_devel}"
INTERVAL="${INTERVAL:-60}"
```

### M4 — Manual one-shot push test

Piped a real snapshot through the same SSH path the SLURM job would use:

```bash
{
  echo "===META==="
  echo "generated_at $(date +%s)"
  echo "node $(hostname)"
  echo "job_id manual-test"
  echo "===SINFO==="
  sinfo -h -p gpu,gpu_devel -N -O 'Partition:25,NodeHost:30,CPUsState:20,AllocMem:14,Memory:14,Gres:50,GresUsed:80,StateLong:18'
  echo "===SQUEUE_R==="
  squeue -h -p gpu,gpu_devel -t R -O 'NodeList:60,JobID:15,UserName:15,Account:25,TimeUsed:15,TimeLimit:15,TimeLeft:15,EndTime:22,tres-alloc:120,Name:60'
  echo "===SQUEUE_PD==="
  squeue -h -p gpu,gpu_devel -t PD -O 'JobID:15,UserName:15,Account:25,Partition:15,Reason:25,TimeLimit:15,StartTime:22,tres-alloc:120,Name:60'
} | ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    -i ~/.ssh/id_ed25519_monitor monitor@159.223.173.141
```

First connection TOFU'd the droplet's host key. SSH closed silently
afterwards (success).

Verified on droplet:
- `/var/lib/monitor/snapshot.txt` was 43 KB, mtime "just now",
  `0664 monitor:monitor`.
- `head -3` showed `===META===` / `generated_at <epoch>` / `node login2.misha.ycrc.yale.edu`.
- `wc -l` → 138 lines.

### M5 — Submit the persistent SLURM job

```bash
cd ~/project/cluster_monitor
sbatch pusher.sbatch
squeue --me -n monitor_pusher
tail -f monitor_pusher.*.log
```

Job 1932411 landed on `r817u35n04.misha.ycrc.yale.edu`. Log showed:

```
[2026-05-09T01:28:01-04:00] pusher started — job 1932411 on r817u35n04.misha.ycrc.yale.edu
[2026-05-09T01:28:01-04:00] config: droplet=monitor@159.223.173.141 partitions=gpu,gpu_devel interval=60s
```

Snapshot mtime confirmed advancing every ~60s on the droplet.

The chain is self-perpetuating from here: at ~23h 30m the job submits its
own successor via `--dependency=afterany`, and `realpath "$0"` picks up
the `~/project/cluster_monitor/pusher.sbatch` location automatically.

---

## Step 7 — Public-internet smoke test

_Pending — to be done off Yale VPN (e.g., phone hotspot)._

Acceptance: `curl -sI https://mishamonitor.duckdns.org/` returns 302
`location: /login?next=/`, browser login as `allen` succeeds, dashboard
shows live GPU node data with snapshot age <60s.

## Step 8 — Lab users

_Pending — only `allen` exists so far._ Add others as needed via:

```bash
sudo -u monitor /home/monitor/ClusterMonitor/.venv/bin/python \
    /home/monitor/ClusterMonitor/manage_users.py add <login> --display "Display Name"
```

---

## Deviations from DEPLOY.md (summary)

| # | DEPLOY.md says | We did | Reason |
|---|---|---|---|
| 1 | `~/cluster_monitor` on Misha | `~/project/cluster_monitor` | User preference; `~/project` is Misha's project quota. |
| 2 | `sudo sed -i` to render systemd unit / Caddyfile in the cloned repo | `sed | > /etc/...` | Keeps the cloned repo clean for future `git pull`. |
| 3 | `scp -r misha-side ...` then ssh | `git clone` on Misha in a single ssh session | Yale Duo 2FA prompts on every connection; one session = one prompt. |
| 4 | Manual M4 lacks `StrictHostKeyChecking=accept-new` | Added it | Avoids the interactive host-key-prompt on first connect. |

## Operational reference

See the table at the bottom of [DEPLOY.md](DEPLOY.md), with one path
substitution: replace `~/cluster_monitor` with `~/project/cluster_monitor`
in any Misha-side commands.
