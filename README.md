# Misha Monitor

A small Flask dashboard that polls Yale's Misha cluster over SSH and
shows partition / node / GPU availability with a per-user login.

Adapted from Carlos Gonzalez's Vanderbilt `compute_monitor`. The
original Vanderbilt-specific code is preserved in [archive/](archive/).

## What you get

- **Per-node card grid** matching the OOD `cluster-status` look —
  hostname, CPU alloc, RAM alloc, GPU type, GPU alloc — colored by
  saturation.
- **Per-GPU-type rollup** at the top: how many H100s / H200s / L40S /
  A40 / A100 are free right now.
- **Lab queue panel**: running and pending jobs for your SLURM account.
- **Click any node** to see the jobs running on it (jobid, user,
  account, GPUs, time used / time limit).
- **Username + password login** — accounts managed via
  [manage_users.py](manage_users.py); passwords hashed with PBKDF2.

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
                          Misha SLURM job (`day` partition, 24h)
                              ├─ runs sinfo / squeue
                              ├─ pipes output via outbound SSH
                              └─ submits its own successor before walltime
```

Misha is on Yale's network so its outbound SSH to your droplet works
without VPN. The droplet has zero credentials for Misha — its only
SSH-facing role is a `monitor` user whose authorized_key is locked
down (via `command="/usr/local/bin/monitor-receive.sh"`) to a single
write-the-snapshot-file action. Compromising the droplet leaks
nothing about Misha.

The SLURM job runs in the `day` partition with `--time=23:55:00`.
Just before walltime, the script submits its own successor with
`--dependency=afterany:$JOBID`, so the chain runs forever without
manual intervention. We prefer `day` over `week` because short-
walltime jobs schedule fast — handoffs are usually <1 min, invisible
to dashboard users.

The pusher runs three SLURM commands every 60s and pipes the raw
output to the droplet:

```bash
sinfo  -h -p gpu,gpu_devel -N -O 'NodeHost,CPUsState,AllocMem,Memory,Gres,GresUsed,StateLong'
squeue -h -p gpu,gpu_devel -t R -O 'NodeList,JobID,UserName,Account,TimeUsed,TimeLimit,tres-alloc,Name'
squeue -h -p gpu,gpu_devel -t PD -O 'JobID,UserName,Account,Partition,Reason,TimeLimit,tres-alloc,Name'
```

No `nvidia-smi` and no SSH-into-compute-nodes — everything is queried
on the Misha login node and visible to any user.

## Repo layout

```
app.py                       Flask app on the droplet (auth + reads snapshot + JSON API)
manage_users.py              CLI to add/remove/reset lab user accounts
templates/
    index.html               Dashboard (dark theme, partition + GPU-type grouping)
    login.html               Login form
demo.html                    Self-contained preview (no server needed)
misha-side/
    pusher.sbatch            SLURM job: polls Misha, pushes snapshot, self-resubmits
    setup.md                 Misha-side install walkthrough
deploy/
    Caddyfile                Droplet TLS + reverse proxy
    monitor-receive.sh       Lockdown script for the droplet's `monitor` SSH user
    misha-monitor.service    systemd unit for Flask on the droplet
    misha-monitor-tunnel.service   (alternative architecture — kept as fallback)
    tunnel.sh                (alternative architecture — kept as fallback)
.env.example                 Config template
users.json.example           Credential file template
requirements.txt             Flask + gunicorn
static/img/favicon.png
DEPLOY.md                    Deployment worksheet — fill in & follow
archive/                     Original Vanderbilt code (kept for reference)
```

## Deploying

See [DEPLOY.md](DEPLOY.md). It's a worksheet — fill in your droplet
IP, Yale host, netid, etc., and it walks through every step.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
$EDITOR .env                              # set SECRET_KEY, MISHA_USER, MISHA_HOST

.venv/bin/python manage_users.py add me --display "Me"

set -a && source .env && set +a
.venv/bin/python app.py
# open http://127.0.0.1:5111 — sign in as 'me'
```

## Limitations / honest caveats

- "GPU alloc" is **scheduler allocation**, not real-time utilization.
  A node showing 4/4 GPUs allocated may have those GPUs sitting idle;
  SLURM doesn't expose that, and you can only run `nvidia-smi` on
  nodes where you have a job. For your own lab's nodes you can
  optionally extend the poller to fan out via `clush -bw @user:$NETID
  nvidia-smi …` and overlay true utilization. See the corresponding
  TODO in `app.py`.
- Job names from `squeue` are clipped to 60 characters. Adjust
  `SQUEUE_FMT_R` / `SQUEUE_FMT_PD` in `app.py` if you want longer.
- Pending-job `Reason` is whatever SLURM reports (`Resources`,
  `Priority`, `QOSMaxJobsPerUserLimit`, etc.) — useful as-is, no
  decoding.
- The login system is intentionally minimal (a JSON file and PBKDF2
  hashes). For more than ~20 users or any sort of audit need, swap in
  Flask-Login + a real DB.
