# Adding Bouchet (a second cluster) to the monitor

Bouchet is served by the **same** droplet, Flask app, login and Caddy as Misha.
What is new is one more snapshot file, one more pusher job (on Bouchet), and
one more SSH key locked to write only that file. Nothing on the Misha side
changes.

```
   browser ──HTTPS──▶ droplet (Caddy :443) ─▶ Flask :5111
                                                 ├─ /          reads /var/lib/monitor/snapshot.txt          ◀── Misha pusher   (key A)
                                                 └─ /bouchet   reads /var/lib/monitor/bouchet/snapshot.txt  ◀── Bouchet pusher (key B)
```

The droplet decides which file a push lands in from the **key** it arrives
with: key A's `authorized_keys` line runs `monitor-receive.sh` (Misha, the
original), key B's runs `monitor-receive.sh bouchet`. The pushed bytes never
choose the destination, so neither cluster can overwrite the other's file.

Do Phase D (droplet) first — it takes a minute and makes `/bouchet` exist
(showing "snapshot file missing" until Phase B). Then Phase B on Bouchet.

---

## Pre-flight

| Field | Value |
|---|---|
| Droplet | `root@159.223.173.141` → `https://qingyuchen-lab-monitor.org` (alias: `mishamonitor.duckdns.org`) |
| Bouchet login | `xw532@bouchet.ycrc.yale.edu` (Duo on every connection — do everything in one session) |
| SLURM account | **`pi_qc88`** — the Chen lab is named differently on Bouchet (Misha: `q_chen`), and your *default* there is the old lab (`pi_mr2749`). List yours with `sacctmgr show assoc user=$USER format=Account -P`; put the right one in `pusher.sbatch` (B3) and `BOUCHET_LAB_ACCOUNT` (D2) |
| Partitions to monitor | `gpu,gpu_devel,gpu_h100,gpu_h200,gpu_b200,gpu_rtx6000` — **verify in B1**; the pusher's `sinfo -p` fails on a name that does not exist |
| Pusher partition | `day` (1-day cap on Bouchet, same as Misha, so `pusher.sbatch` runs unchanged) |
| Bouchet-side install path | `~/project/cluster_monitor` (same convention as Misha) |

Bouchet GPU types (SLURM GRES names, from docs.ycrc.yale.edu/clusters/bouchet):
`h100`, `h200`, `b200`, `l40s`, `a40`, `a5000`, `rtx_5000_ada`,
`rtx_pro_6000_blackwell`. H100/H200/B200 and the RTX Pro 6000 each have their
own `gpu_*` partition; the rest share `gpu`. The dashboard already knows these
names (colours, partition order, the salloc helper).

---

## Phase D — Droplet

### D1 — Deploy the multi-cluster app

```bash
# laptop
git push origin main
```

```bash
# droplet
ssh root@159.223.173.141
sudo -u monitor bash -lc 'cd ~/ClusterMonitor && git pull --ff-only'
cp /home/monitor/ClusterMonitor/deploy/monitor-receive.sh /usr/local/bin/monitor-receive.sh
chmod 755 /usr/local/bin/monitor-receive.sh
```

The receive script now accepts a cluster name; with no argument it behaves
exactly as before, so Misha's existing key keeps working.

### D2 — Enable Bouchet in `.env`

Append to `/home/monitor/ClusterMonitor/.env` (the existing Misha lines stay
as they are — Misha still reads `SNAPSHOT_FILE`, `MISHA_PARTITIONS`,
`LAB_ACCOUNT`):

```bash
sudo -u monitor tee -a /home/monitor/ClusterMonitor/.env <<'EOF'

# --- Clusters (see DEPLOY_BOUCHET.md) ---
CLUSTERS=misha,bouchet
BOUCHET_HOST=bouchet.ycrc.yale.edu
BOUCHET_PARTITIONS=gpu,gpu_devel,gpu_h100,gpu_h200,gpu_b200,gpu_rtx6000
BOUCHET_SNAPSHOT_FILE=/var/lib/monitor/bouchet/snapshot.txt
BOUCHET_LAB_ACCOUNT=pi_qc88
EOF
systemctl restart misha-monitor
systemctl is-active misha-monitor
curl -s http://127.0.0.1:5111/healthz
# → {"clusters":{"bouchet":{...},"misha":{...}},"ok":true}
```

`BOUCHET_PARTITIONS` must match what the Bouchet pusher polls (B3). If B1
shows different partition names, fix both places.

`https://mishamonitor.duckdns.org/bouchet` now renders, with Misha ⇄ Bouchet
tabs in the header, and says "snapshot file missing" — expected until B5.
Check that `/` (Misha) still shows live data before moving on.

### D3 — Wait for the Bouchet key (B2), then lock it in

After B2 gives you the public key, paste it as a **second line** in
`/home/monitor/.ssh/authorized_keys`, with the Bouchet lockdown prefix:

```bash
sudo -u monitor tee -a /home/monitor/.ssh/authorized_keys <<'EOF'
command="/usr/local/bin/monitor-receive.sh bouchet",no-pty,no-X11-forwarding,no-agent-forwarding,no-port-forwarding ssh-ed25519 AAAA...PASTE_THE_BOUCHET_KEY... bouchet-monitor-pusher
EOF
wc -l /home/monitor/.ssh/authorized_keys      # → 2 (Misha line + Bouchet line)
```

The only difference from Misha's line is the `bouchet` argument. That
argument is what routes the push to `/var/lib/monitor/bouchet/snapshot.txt`
(the script creates the directory on first push).

---

## Phase B — Bouchet

One SSH session (one Duo prompt); steps B1–B5 all run inside it.

```bash
ssh xw532@bouchet.ycrc.yale.edu
```

### B1 — Confirm the partitions and the `day` cap

```bash
sinfo -s -o '%P %l' | egrep '^(gpu|day)'      # partition names + time limits
sinfo -s -o '%P %l' | egrep '^(gpu|day)' | column -t
```

Expected: `gpu`, `gpu_devel`, `gpu_h100`, `gpu_h200`, `gpu_b200`,
`gpu_rtx6000` (each 2-00:00:00 / 6:00:00) and `day` at `1-00:00:00`.
If a `gpu_*` name differs, use the real names in B3 **and** in D2.

### B2 — Pusher files + SSH key

```bash
cd /tmp && rm -rf Yale_ClusterMonitor
git clone https://github.com/xwang112358/Yale_ClusterMonitor.git
mkdir -p ~/project/cluster_monitor
cp -r Yale_ClusterMonitor/misha-side/. ~/project/cluster_monitor/
rm -rf /tmp/Yale_ClusterMonitor

mkdir -p ~/.ssh && chmod 700 ~/.ssh
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_monitor -C "bouchet-monitor-pusher"
cat ~/.ssh/id_ed25519_monitor.pub          # ← copy this whole line for D3
```

(Bouchet and Misha have separate home directories, so the key path can stay
the same as on Misha.) Do D3 on the droplet now, before B4.

### B3 — Configure the pusher

```bash
cd ~/project/cluster_monitor
sed -i 's#DROPLET_HOST:-203\.0\.113\.10#DROPLET_HOST:-159.223.173.141#' pusher.sbatch
sed -i 's#PARTITIONS:-gpu,gpu_devel}#PARTITIONS:-gpu,gpu_devel,gpu_h100,gpu_h200,gpu_b200,gpu_rtx6000}#' pusher.sbatch
sed -i 's/^#SBATCH --requeue$/#SBATCH --requeue\n#SBATCH --account=pi_qc88/' pusher.sbatch
bash -n pusher.sbatch && grep -n 'account=\|DROPLET_HOST=\|PARTITIONS=' pusher.sbatch
```

Expected:

```
#SBATCH --account=pi_qc88
DROPLET_HOST="${DROPLET_HOST:-159.223.173.141}"
PARTITIONS="${PARTITIONS:-gpu,gpu_devel,gpu_h100,gpu_h200,gpu_b200,gpu_rtx6000}"
```

The account goes in the file, not on the `sbatch` command line, because the job
resubmits itself daily from the file. Without it the job bills your *default*
account. **Get the file onto the cluster with `git clone` or `curl`, never by
pasting into an editor**: a paste that inserts blank lines breaks the `\`
line continuations, `ssh` then runs with no arguments and every push fails with
`-o: command not found` in the job log.

### B4 — One-shot manual push

Same path the job will use; a silent exit means the droplet accepted it.

```bash
P=gpu,gpu_devel,gpu_h100,gpu_h200,gpu_b200,gpu_rtx6000
{
  echo "===META==="
  echo "generated_at $(date +%s)"
  echo "node $(hostname)"
  echo "job_id manual-test"
  echo "cluster bouchet"
  echo "===SINFO==="
  sinfo -h -p $P -N -O 'Partition:25,NodeHost:30,CPUsState:20,AllocMem:14,Memory:14,Gres:50,GresUsed:80,StateLong:18'
  echo "===SQUEUE_R==="
  squeue -h -p $P -t R -O 'NodeList:60,JobID:15,UserName:15,Account:25,TimeUsed:15,TimeLimit:15,TimeLeft:15,EndTime:22,tres-alloc:120,Name:60'
  echo "===SQUEUE_PD==="
  squeue -h -p $P -t PD -O 'JobID:15,UserName:15,Account:25,Partition:15,Reason:25,TimeLimit:15,StartTime:22,tres-alloc:120,Name:60'
} | ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    -i ~/.ssh/id_ed25519_monitor monitor@159.223.173.141
```

Then on the droplet:

```bash
ls -la /var/lib/monitor/bouchet/snapshot.txt && head -5 /var/lib/monitor/bouchet/snapshot.txt
```

`https://mishamonitor.duckdns.org/bouchet` shows nodes now.

If the push prints `Permission denied (publickey)`: the D3 line is wrong or
missing. If it prints `rejected: missing META section`: a partition name in
`$P` is wrong and `sinfo` wrote an error into the snapshot — redo B1.

### B5 — Start the chain

```bash
cd ~/project/cluster_monitor
sbatch pusher.sbatch
squeue --me -n monitor_pusher
tail -f monitor_pusher.*.log       # expect "config: cluster=bouchet droplet=monitor@159.223.173.141 partitions=gpu,..."
```

Within a minute the Bouchet page reads "live" with an age under 60 s. The
job resubmits itself before its 23:55 walltime, as on Misha.

---

## Operations (Bouchet column)

| Task | Where | Command |
|---|---|---|
| Snapshot freshness | droplet | `stat -c %y /var/lib/monitor/bouchet/snapshot.txt` |
| Per-cluster health | droplet | `curl -s http://127.0.0.1:5111/healthz` |
| Check pusher chain | bouchet | `squeue --me -n monitor_pusher` |
| Tail pusher log | bouchet | `tail -f ~/project/cluster_monitor/monitor_pusher.*.log` |
| Stop the chain | bouchet | `scancel -n monitor_pusher` |
| Restart the chain | bouchet | `cd ~/project/cluster_monitor && sbatch pusher.sbatch` |
| Change partitions | both | edit `PARTITIONS` in `pusher.sbatch` (bouchet) **and** `BOUCHET_PARTITIONS` in `.env` + `systemctl restart misha-monitor` (droplet) |
| Revoke Bouchet | droplet | delete the `bouchet-monitor-pusher` line from `/home/monitor/.ssh/authorized_keys`; drop `bouchet` from `CLUSTERS`; restart |

## How the app maps clusters to URLs

`CLUSTERS=misha,bouchet` in `.env`. The first slug is the default: `/` and
`/api/cluster`. Every other slug is `/<slug>` and `/api/cluster/<slug>`.
Each slug reads `<SLUG>_HOST`, `<SLUG>_PARTITIONS`, `<SLUG>_SNAPSHOT_FILE`,
`<SLUG>_LAB_ACCOUNT` (defaults to `LAB_ACCOUNT`) and `<SLUG>_LABEL`. Misha
also accepts the original names (`MISHA_HOST`, `MISHA_PARTITIONS`,
`SNAPSHOT_FILE`), which is why the existing `.env` needed only additions.
Caches, staleness and errors are per cluster: Bouchet's pusher going down
never marks Misha stale.

A third cluster is the same recipe again: another key locked to
`monitor-receive.sh <slug>`, `<SLUG>_*` in `.env`, `<slug>` appended to
`CLUSTERS`, one more pusher.

## Policy note

Same standing as Misha (see the policy notes in `DEPLOY.md`): scheduler
metadata only, password-gated, a 1-CPU / 512 MB sleep loop in `day`. If you
sent the courtesy note to `hpc@yale.edu` for Misha, a one-line follow-up
that the same job now also runs on Bouchet keeps the paper trail complete.
