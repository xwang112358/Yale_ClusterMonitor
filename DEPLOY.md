# Misha Monitor — Deployment Worksheet

Fill in the blanks below as you go. Then work through the steps; each
step references the values you've filled in.

> **Companion docs:**
> - [upgrade.md](upgrade.md) — day-to-day operations after deploy:
>   add/remove users, change domain, rotate keys, update partitions,
>   pull updates, re-deploy after a droplet rebuild.
> - [deployment_history.md](deployment_history.md) — record of an
>   actual deployment (values, outputs, deviations) — useful as a
>   worked example.

---

## How it works

```
   browser ──HTTPS──▶ DigitalOcean droplet (Caddy :443)
                          │
                          └─ reverse_proxy ─▶ Flask :5111
                                                  ▲
                                                  │ reads
                                          /var/lib/monitor/snapshot.txt
                                                  ▲
                                                  │ ssh-pushes  (every 60s)
                                                  │
                          Misha SLURM job (`day` partition)
                              ├─ runs sinfo / squeue
                              ├─ pipes output via outbound SSH
                              └─ submits its own successor before walltime
```

- **Misha is on Yale's network** so its outbound SSH to your droplet
  works. You never have to put VPN credentials on the droplet.
- **The droplet has zero credentials for Misha.** Compromising the
  droplet leaks nothing about Misha. The droplet's `monitor` user
  accepts an SSH key that's locked down to a single command (it can
  only write the snapshot file).
- **The chain is self-perpetuating.** The SLURM job's `--time` is
  set to 23h 55m (`day` partition cap is 24h). Just before walltime,
  the script submits its own successor with
  `--dependency=afterany:$JOBID`, so the chain runs forever. We
  prefer `day` over `week` because short-walltime jobs schedule
  faster — successor handoff is usually <1 min, well inside the
  dashboard's 5-min staleness budget.
- **No live SSH connection** is ever held. If Misha hiccups, one push
  fails, the next one succeeds.

---

## Pre-flight info to gather

> Fill these in. Keep your filled-in copy of this file off public git
> if you put any private detail in it.

### DigitalOcean droplet (Ubuntu)
- IP address: `____________________`
- Public hostname (DNS A record you'll point at it): `____________________`
- SSH login user (root or your sudo user): `____________________`

> Don't have a domain? Free option: register a subdomain at
> [duckdns.org](https://www.duckdns.org) (sign in with GitHub/Google,
> pick a name like `mishamonitor.duckdns.org`, point it at your droplet
> IP). Caddy issues a real Let's Encrypt cert for it. No credit card.
> Works with Caddy's TLS-ALPN-01 challenge — only port 443 needs to be
> reachable.

### Yale Misha
- Your netid: `____________________`
- Your SLURM account (run `sacctmgr show user $USER format=Account` on Misha): `____________________`
- Partitions to monitor (default `gpu,gpu_devel`): `____________________`

### Lab members (login accounts for the dashboard)
- List of usernames + display names you'll create with `manage_users.py`:
  - `____________________`  →  `____________________`
  - `____________________`  →  `____________________`
  - `____________________`  →  `____________________`

---

## Phase D — Droplet setup (do this first)

### Step D1 — Install dependencies

SSH into the droplet:

```bash
sudo apt update
sudo apt install -y caddy python3-venv git

# Service user that owns Flask + receives the SSH push
sudo useradd -m -s /bin/bash monitor

# Snapshot directory writable by the receiver
sudo mkdir -p /var/lib/monitor
sudo chown monitor:monitor /var/lib/monitor
sudo chmod 755 /var/lib/monitor
```

### Step D2 — Clone the repo + install the receive-only SSH command

(Despite ordering, this combines the clone half of D4 with D2 — D2's
files come from the clone, so we do them together.)

Clone the repo as the `monitor` user:

```bash
sudo -u monitor git clone https://github.com/<your-org>/Yale_ClusterMonitor.git \
    /home/monitor/ClusterMonitor
```

> If the repo is private, GitHub will reject password auth. Two options:
> (a) make the repo public — its content is non-secret if you keep
> `.env`, `users.json`, and `.flask_secret` gitignored (already so in
> this repo), or (b) generate a deploy key on the droplet and add it to
> the repo's Deploy keys page. See `upgrade.md` for the deploy-key flow.

Install the receive script:

```bash
sudo cp /home/monitor/ClusterMonitor/deploy/monitor-receive.sh /usr/local/bin/
sudo chmod 755 /usr/local/bin/monitor-receive.sh
```

Set up the `monitor` user's `authorized_keys` with the lockdown:

```bash
sudo mkdir -p /home/monitor/.ssh
sudo chmod 700 /home/monitor/.ssh
sudo touch /home/monitor/.ssh/authorized_keys
sudo chmod 600 /home/monitor/.ssh/authorized_keys
sudo chown -R monitor:monitor /home/monitor/.ssh

# After Step M2 below you'll paste the Misha public key into this file.
# The line MUST start with the command= prefix shown below.
```

The line you'll add later (after generating the Misha-side key) looks like:

```
command="/usr/local/bin/monitor-receive.sh",no-pty,no-X11-forwarding,no-agent-forwarding,no-port-forwarding ssh-ed25519 AAAA...PUBLIC_KEY... misha-monitor-pusher
```

That `command=` prefix means the Misha key can only invoke the
receiver script — it cannot get a shell, port-forward, or anything else.

### Step D3 — DNS

Create an `A` record:

| Name | Type | Value |
|---|---|---|
| `<your hostname>` | `A` | `<droplet IP>` |

Wait until `dig +short <your hostname>` returns the droplet IP.

### Step D4 — Install the Flask app

(Repo is already cloned from D2. Just create the venv and `.env` here.)

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
```

Edit `/home/monitor/ClusterMonitor/.env`:

```ini
SECRET_KEY=<run:  python3 -c "import secrets; print(secrets.token_hex(32))">
DATA_SOURCE=file
SNAPSHOT_FILE=/var/lib/monitor/snapshot.txt
SNAPSHOT_MAX_AGE=300
LAB_ACCOUNT=<your slurm account>
MISHA_PARTITIONS=gpu,gpu_devel
USERS_FILE=/home/monitor/ClusterMonitor/users.json
BIND=127.0.0.1
PORT=5111
```

Create the first dashboard user:

```bash
sudo -u monitor bash -lc '
  cd ~/ClusterMonitor
  .venv/bin/python manage_users.py add <login_name> --display "Display Name"
'
# (you'll be prompted for a password)
```

### Step D5 — Flask service

Render the unit through `sed | >` instead of `sed -i` so the cloned
repo file isn't dirtied (future `git pull` stays clean).

```bash
sudo sed 's/REPLACE_ME_USERNAME/monitor/g' \
    /home/monitor/ClusterMonitor/deploy/misha-monitor.service \
    > /etc/systemd/system/misha-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now misha-monitor.service
sudo systemctl status misha-monitor.service --no-pager     # should be 'active (running)'
```

Sanity-check it's listening:

```bash
ss -tlnp | grep 5111             # gunicorn LISTEN on 127.0.0.1:5111
curl -sI http://127.0.0.1:5111/  # HTTP/1.1 302 FOUND, Location: /login?next=/
```

The dashboard will say "snapshot file missing" until Step M5 below — expected.

### Step D6 — Caddy (HTTPS)

Same pipe-rewrite trick — substitute your DNS name as we drop the file
into place, so the cloned repo stays clean.

```bash
sudo sed 's#misha\.example\.com#<your hostname>#g' \
    /home/monitor/ClusterMonitor/deploy/Caddyfile \
    > /etc/caddy/Caddyfile

sudo mkdir -p /var/log/caddy
sudo chown caddy:caddy /var/log/caddy

sudo systemctl reload caddy
sudo journalctl -u caddy -n 50 --no-pager  # watch for "certificate obtained successfully"
```

Test from the droplet itself:

```bash
curl -sI https://<your hostname>/
# expect: HTTP/2 302, location: /login?next=/, server: Caddy
```

The two warnings you may see in the logs are both harmless:
- `Caddyfile input is not formatted` — purely cosmetic whitespace.
- `no OCSP server specified in certificate` — Let's Encrypt no longer
  publishes OCSP info; nothing to fix.

---

## Phase M — Misha-side setup

You'll need Yale VPN once for these steps; after they're done, the
chain runs autonomously and you never need VPN again to use the
dashboard.

> **Yale Duo 2FA prompts on every SSH/SCP**, which makes the
> "scp-then-ssh" flow painful. The fix is to do everything in one SSH
> session: log in once (one Duo prompt), and pull the pusher files via
> `git clone` directly on Misha. We'll install into
> `~/project/cluster_monitor` (Yale's group-quota project area), not
> `~/`.
>
> Optional: set up SSH ControlMaster on your laptop to multiplex
> connections within a 10-min window. Add to `~/.ssh/config`:
> ```
> Host misha
>     HostName misha.ycrc.yale.edu
>     User <your-netid>
>     ControlMaster auto
>     ControlPath ~/.ssh/cm-%r@%h:%p
>     ControlPersist 10m
> ```

### Step M1 — Get the pusher onto Misha (single SSH session)

From your laptop (on VPN):

```bash
ssh <your-netid>@misha.ycrc.yale.edu          # one Duo prompt
```

Then **on Misha** — do M1 + M2 in one go:

```bash
# Pull the pusher files from the public repo
cd /tmp
rm -rf Yale_ClusterMonitor
git clone https://github.com/<your-org>/Yale_ClusterMonitor.git
mkdir -p ~/project/cluster_monitor
cp -r Yale_ClusterMonitor/misha-side/. ~/project/cluster_monitor/
rm -rf /tmp/Yale_ClusterMonitor
ls -la ~/project/cluster_monitor               # expect: pusher.sbatch, setup.md
```

### Step M2 — Generate the push SSH key on Misha

Still in the same SSH session:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_monitor -C "misha-monitor-pusher"
cat ~/.ssh/id_ed25519_monitor.pub      # ← copy this output
```

Back on the **droplet**, paste it into `/home/monitor/.ssh/authorized_keys`,
prefixed with the lockdown:

```bash
sudo -u monitor tee -a /home/monitor/.ssh/authorized_keys <<'EOF'
command="/usr/local/bin/monitor-receive.sh",no-pty,no-X11-forwarding,no-agent-forwarding,no-port-forwarding ssh-ed25519 AAAA...PASTE_THE_KEY_HERE... misha-monitor-pusher
EOF
```

(Replace `AAAA…misha-monitor-pusher` with the full key body from M2.)

### Step M3 — Configure the pusher

Patch `pusher.sbatch` in place — only `DROPLET_HOST` actually needs
changing; the other defaults match what we want.

```bash
cd ~/project/cluster_monitor
sed -i 's#DROPLET_HOST:-203\.0\.113\.10#DROPLET_HOST:-<your droplet IP>#' pusher.sbatch
grep -n 'DROPLET_HOST\|DROPLET_USER\|SSH_KEY\|PARTITIONS\|INTERVAL' pusher.sbatch
```

Final values should read:
```
DROPLET_USER="${DROPLET_USER:-monitor}"
DROPLET_HOST="${DROPLET_HOST:-<your droplet IP>}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_monitor}"
PARTITIONS="${PARTITIONS:-gpu,gpu_devel}"
INTERVAL="${INTERVAL:-60}"
```

### Step M4 — One-shot manual push test

Pipe a real snapshot through the same SSH path the SLURM job will use,
to confirm key + lockdown + receiver script are wired up correctly
before committing to the long-running job.

```bash
cd ~/project/cluster_monitor

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
    -i ~/.ssh/id_ed25519_monitor monitor@<DROPLET_IP>
```

The `StrictHostKeyChecking=accept-new` makes the first connection TOFU
the droplet's host key without an interactive prompt. Subsequent pushes
verify against `~/.ssh/known_hosts`.

A successful push closes silently — no stdout/stderr from `ssh` means
the receiver wrote the snapshot and exited 0.

Confirm on the **droplet**:

```bash
ls -la /var/lib/monitor/snapshot.txt
# should show a recent mtime and ~30-100 KB size
head -3 /var/lib/monitor/snapshot.txt
# should print:
#   ===META===
#   generated_at 1715200000
#   ...
```

If you get this far, the dashboard at `https://<your hostname>/`
already works — refresh your browser.

### Step M5 — Submit the persistent SLURM job

Still on Misha:

```bash
cd ~/project/cluster_monitor
sbatch pusher.sbatch
squeue --me -n monitor_pusher
tail -f monitor_pusher.<jobid>.log
```

Within ~60s the dashboard should show fresh data. The job runs for
~23h 30m, then submits its own successor. The chain is now persistent.

See `misha-side/setup.md` for stop / restart / troubleshooting.

---

## Step 7 — Smoke test from the public internet

From your laptop (off-VPN, off-Yale-network — phone hotspot is fine):

```bash
curl -sI https://<your hostname>/
# Expect: 302 Found, Location: /login

# Open in a browser:
# https://<your hostname>/
# Sign in with the user you created in Step D4.
```

If the dashboard loads and shows nodes, you're done. Send the URL +
credentials to your labmates — they need nothing on their end.

---

## Step 8 — Add the rest of your lab

```bash
sudo -u monitor bash -lc '
  cd ~/ClusterMonitor
  .venv/bin/python manage_users.py add <login_name> --display "Name"
'
```

The app re-reads `users.json` on every login — no restart needed.

---

## Operations

| Task | Where | Command |
|---|---|---|
| Check Flask is up | droplet | `systemctl status misha-monitor` |
| Check Caddy | droplet | `systemctl status caddy` |
| Tail Flask logs | droplet | `journalctl -u misha-monitor -f` |
| Tail Caddy logs | droplet | `tail -f /var/log/caddy/misha-monitor.log` |
| Restart Flask | droplet | `sudo systemctl restart misha-monitor` |
| Snapshot freshness | droplet | `stat /var/lib/monitor/snapshot.txt` |
| Hot-add a lab user | droplet | `sudo -u monitor .venv/bin/python manage_users.py add <user>` |
| Reset a password | droplet | `… manage_users.py reset <user>` |
| Remove a user | droplet | `… manage_users.py remove <user>` |
| Check pusher chain | misha | `squeue --me -n monitor_pusher` |
| Tail pusher log | misha | `tail -f ~/project/cluster_monitor/monitor_pusher.*.log` |
| Stop the chain | misha | `scancel -n monitor_pusher` |
| Restart the chain | misha | `cd ~/project/cluster_monitor && sbatch pusher.sbatch` |

## Troubleshooting

**Dashboard says "snapshot file missing".**
The pusher hasn't successfully pushed yet. On Misha, check
`squeue --me -n monitor_pusher` — if empty, the chain isn't running;
re-submit. If it's running, check the log on Misha for SSH errors —
likely the public key isn't in the droplet's `monitor` authorized_keys.

**Dashboard says "snapshot is N seconds old (limit 300s)".**
Pusher hasn't pushed in 5+ minutes. Check the job: it may have ended
without submitting a successor (rare; if so, just re-submit it). Or
SSH from Misha to the droplet is broken — check
`~/monitor_pusher_ssh.err` on Misha.

**Pusher SSH fails with "Permission denied (publickey)".**
The lockdown line in the droplet's `authorized_keys` is wrong. Verify
the line starts with `command="/usr/local/bin/monitor-receive.sh"` and
that the public key body matches `~/.ssh/id_ed25519_monitor.pub` on
Misha exactly.

**Pusher SSH fails with "rejected: missing META section".**
The receiver script saw bytes that didn't start with `===META===`. The
pusher's piped output is malformed — usually means a `sinfo` or
`squeue` command failed and dumped an error to stdout. Run the M4
manual test and inspect the bytes.

**Login page loops back to itself.**
`SECRET_KEY` is changing between restarts. Make sure it's set
explicitly in `.env`, not relying on the auto-generated `.flask_secret`.

**Successor failed to submit (chain broken).**
On Misha, `tail` the pusher log — look for `WARN: successor sbatch
failed`. Common reasons: SLURM controller hiccup, account quota.
Re-submit manually with `sbatch pusher.sbatch`. To detect this
automatically, see "Optional: stale-snapshot watchdog" below.

## Optional: stale-snapshot watchdog (droplet)

If you want an alert when the snapshot goes stale (pusher chain broke,
Misha is down, etc.), add a tiny systemd timer on the droplet:

```bash
# /etc/systemd/system/snapshot-watchdog.service
[Unit]
Description=Alert if Misha snapshot stale

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'age=$(( $(date +%%s) - $(stat -c %%Y /var/lib/monitor/snapshot.txt) )); test "$age" -lt 600 || (echo "stale snapshot: $age s old" | mail -s "Misha monitor stale" your@email.example)'

# /etc/systemd/system/snapshot-watchdog.timer
[Unit]
Description=Run snapshot freshness check every 10 min

[Timer]
OnUnitActiveSec=10min
OnBootSec=10min

[Install]
WantedBy=timers.target
```

`sudo systemctl enable --now snapshot-watchdog.timer`. Requires
`mailutils` or any other mail-capable transport on the droplet.

## Policy notes (read before deploying)

This dashboard sits in a gray area of YCRC policy. Nothing it does is
explicitly forbidden, but nothing is explicitly blessed either. The
honest accounting:

**What is transmitted off-Yale:** raw `sinfo` and `squeue` output —
node names, partition states, GPU allocations, **other users' netids
and job metadata** (account, name, GPU/CPU/RAM allocation, time
limit). Any Yale netid can see this already by running `squeue`; the
dashboard mirrors it to a private auth-gated web page.

**What is NOT transmitted:** files from `/gpfs/radev`, job stdout,
code, datasets, model weights, or anything that would constitute
research data, PHI, FERPA-protected info, or export-controlled
material. Yale's Acceptable Use Policy does not flag aggregate
scheduler metadata.

**Edges to be aware of:**

1. Republishing other users' activity outside Yale's auth boundary —
   even behind your dashboard login — is a different category from
   "any Yale netid can see this." Mitigation: keep the dashboard
   password-gated, don't make accounts trivially shared, and don't
   archive the data into something analytical/persistent.
2. The pusher runs a long-lived process in the `day` partition.
   Footprint is tiny (1 CPU, 512M, sleep loop), but it's not
   computational work in the strict sense. The job re-launches itself
   roughly every 23h via `--dependency=afterany`.
3. Outbound SSH from compute nodes is not documented as forbidden,
   but it's not endorsed either.

**Recommended:** send a courtesy email to `hpc@yale.edu`. Two minutes,
sets a paper trail, and you'll know early if YCRC has an objection.
Suggested wording:

> "I'd like to set up a small lab dashboard that shows partition GPU
> availability and our lab's queue. The plan is a 1-CPU, 512M SLURM
> job in the `day` partition that polls `sinfo`/`squeue` once a
> minute and pushes the output via outbound SSH to a private
> password-gated dashboard. The job re-launches itself daily via
> --dependency=afterany. Is there a YCRC policy I should be aware
> of, or a preferred approach?"

If they suggest moving the pusher off Misha (e.g., running it on a
Yale lab workstation instead, or via user cron on a different host),
the same `pusher.sbatch` body works as a plain bash loop — drop the
`#SBATCH` headers and the `--dependency` resubmit logic, and run it
under systemd / cron on the alternative host.

---

## Security checklist

- [ ] `users.json` is `chmod 600`, gitignored.
- [ ] `.env` is `chmod 600`, gitignored.
- [ ] `SECRET_KEY` is set in `.env` to a long random string.
- [ ] `monitor` service user on the droplet doesn't have sudo.
- [ ] `monitor`'s `authorized_keys` line starts with
      `command="/usr/local/bin/monitor-receive.sh"` — the Misha key
      can only invoke the receiver, not get a shell.
- [ ] `/usr/local/bin/monitor-receive.sh` rejects oversize input
      (>8 MiB) and requires a `===META===` header.
- [ ] Misha's `~/.ssh/id_ed25519_monitor` key was generated with
      `-N ''` (no passphrase, since it runs in a SLURM job).

---

## Filling-in summary (copy this section, fill in, keep locally)

```
Droplet IP:               _______________
Droplet hostname (DNS):   _______________
Droplet SSH user:         _______________
Misha netid:              _______________
SLURM account:            _______________
Partitions:               _______________
First lab user (login):   _______________ / _______________ (display)
```
