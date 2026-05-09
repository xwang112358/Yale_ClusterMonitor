# Misha Monitor — Deployment Worksheet

Fill in the blanks below as you go. Then work through the steps; each
step references the values you've filled in.

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

### Step D2 — Install the receive-only SSH command

```bash
sudo cp /home/<your-droplet-user>/ClusterMonitor/deploy/monitor-receive.sh /usr/local/bin/
sudo chmod 755 /usr/local/bin/monitor-receive.sh
```

(If you haven't cloned the repo yet, do that first — see Step D4.)

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

```bash
sudo -u monitor bash -lc '
  cd ~
  git clone <this-repo-url> ClusterMonitor
  cd ClusterMonitor
  python3 -m venv .venv
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

```bash
sudo sed -i 's/REPLACE_ME_USERNAME/monitor/g' \
    /home/monitor/ClusterMonitor/deploy/misha-monitor.service
sudo cp /home/monitor/ClusterMonitor/deploy/misha-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now misha-monitor.service
sudo systemctl status misha-monitor.service     # should be 'active (running)'
```

The dashboard will say "snapshot file missing" until Step M5 below — expected.

### Step D6 — Caddy (HTTPS)

```bash
sudo cp /home/monitor/ClusterMonitor/deploy/Caddyfile /etc/caddy/Caddyfile
sudo $EDITOR /etc/caddy/Caddyfile     # change misha.example.com to your DNS name
sudo systemctl reload caddy
sudo journalctl -u caddy -f           # watch for "certificate obtained successfully"
```

---

## Phase M — Misha-side setup

You'll need Yale VPN once for these steps; after they're done, the
chain runs autonomously and you never need VPN again to use the
dashboard.

### Step M1 — Copy the pusher to Misha

From your laptop (on VPN):

```bash
scp -r misha-side <your-netid>@misha.ycrc.yale.edu:~/cluster_monitor
```

### Step M2 — Generate the push SSH key on Misha

```bash
ssh <your-netid>@misha.ycrc.yale.edu

# On Misha:
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

On Misha, edit `~/cluster_monitor/pusher.sbatch` and set:

```bash
DROPLET_USER=monitor
DROPLET_HOST=<your droplet IP>
SSH_KEY=$HOME/.ssh/id_ed25519_monitor
PARTITIONS=gpu,gpu_devel
INTERVAL=60
```

### Step M4 — One-shot manual push test

```bash
cd ~/cluster_monitor

# Pipe a fake snapshot through the same SSH path the SLURM job uses:
{
  echo "===META==="; date +%s | sed 's/^/generated_at /'
  echo "===SINFO==="
  sinfo -h -p gpu,gpu_devel -N -O 'Partition:25,NodeHost:30,CPUsState:20,AllocMem:14,Memory:14,Gres:50,GresUsed:80,StateLong:18'
  echo "===SQUEUE_R==="
  squeue -h -p gpu,gpu_devel -t R -O 'NodeList:60,JobID:15,UserName:15,Account:25,TimeUsed:15,TimeLimit:15,TimeLeft:15,EndTime:22,tres-alloc:120,Name:60'
  echo "===SQUEUE_PD==="
  squeue -h -p gpu,gpu_devel -t PD -O 'JobID:15,UserName:15,Account:25,Partition:15,Reason:25,TimeLimit:15,StartTime:22,tres-alloc:120,Name:60'
} | ssh -o BatchMode=yes -i ~/.ssh/id_ed25519_monitor monitor@<DROPLET_IP>
```

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
cd ~/cluster_monitor
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
| Tail pusher log | misha | `tail -f ~/cluster_monitor/monitor_pusher.*.log` |
| Stop the chain | misha | `scancel -n monitor_pusher` |
| Restart the chain | misha | `cd ~/cluster_monitor && sbatch pusher.sbatch` |

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
