# Misha-side setup (the pusher)

These steps run on **Misha** (you'll need to be on Yale VPN once to do
the initial setup; after that the pusher runs autonomously and you
never need VPN again to use the dashboard).

The pusher is a SLURM job in the `day` partition that wakes every
~60 s, runs `sinfo`/`squeue`, and pipes the output over outbound SSH
to your DigitalOcean droplet. Just before its 24-hour walltime, it
submits its own successor via `--dependency=afterany`, so the chain
runs forever without manual intervention.

Why `day` instead of `week`: short-walltime partitions schedule jobs
much faster, so the predecessor→successor handoff is usually under a
minute. With the dashboard's 5-minute staleness budget, that gap is
invisible to users. (You can override to `week` if you'd rather have
fewer chain transitions; see the SBATCH header in `pusher.sbatch`.)

## What you need

- SSH access to Misha (your normal `<netid>@misha.ycrc.yale.edu` login).
- The droplet IP and the `monitor` user's authorized_keys set up on it
  (see `DEPLOY.md` step D2).

## Step M1 — Copy this directory to Misha

From your laptop (on VPN):

```bash
scp -r misha-side <your-netid>@misha.ycrc.yale.edu:~/cluster_monitor
```

(Or `git clone` the whole repo on Misha and just keep the `misha-side`
directory.)

## Step M2 — Generate an SSH key for pushing to the droplet

On Misha:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_monitor -C "misha-monitor-pusher"
cat ~/.ssh/id_ed25519_monitor.pub
```

Copy that public key. You'll paste it into the **droplet's**
`monitor` user authorized_keys with a `command="..."` lockdown so the
key can only write the snapshot file (see DEPLOY.md step D2 for the
exact line).

## Step M3 — Configure the pusher

Edit `~/cluster_monitor/pusher.sbatch`. The five values to check at
the top:

```bash
DROPLET_USER=monitor
DROPLET_HOST=<your droplet IP>      # ← change this
SSH_KEY=$HOME/.ssh/id_ed25519_monitor
PARTITIONS=gpu,gpu_devel            # ← add bigmem,day,etc. if you want
INTERVAL=60                         # seconds between polls
```

## Step M4 — Test the push manually (foreground, on the login node)

Before submitting as a SLURM job, sanity-check the push works:

```bash
cd ~/cluster_monitor
{
  echo "===META==="
  echo "generated_at $(date +%s)"
  echo "===SINFO==="
  sinfo -h -p gpu,gpu_devel -N -O 'Partition:25,NodeHost:30,CPUsState:20,AllocMem:14,Memory:14,Gres:50,GresUsed:80,StateLong:18'
  echo "===SQUEUE_R==="
  squeue -h -p gpu,gpu_devel -t R -O 'NodeList:60,JobID:15,UserName:15,Account:25,TimeUsed:15,TimeLimit:15,TimeLeft:15,EndTime:22,tres-alloc:120,Name:60'
  echo "===SQUEUE_PD==="
  squeue -h -p gpu,gpu_devel -t PD -O 'JobID:15,UserName:15,Account:25,Partition:15,Reason:25,TimeLimit:15,StartTime:22,tres-alloc:120,Name:60'
} | ssh -o BatchMode=yes -i ~/.ssh/id_ed25519_monitor monitor@<DROPLET_IP>
```

If that succeeds (no error and no output), confirm on the **droplet**:

```bash
ls -la /var/lib/monitor/snapshot.txt
head -5 /var/lib/monitor/snapshot.txt
```

You should see the `===META===` line and a recent mtime. Now you're
ready to run it in a SLURM job.

## Step M5 — Submit the pusher

```bash
cd ~/cluster_monitor
sbatch pusher.sbatch
squeue --me -n monitor_pusher
```

Watch the log:

```bash
tail -f monitor_pusher.<jobid>.log
```

You should see lines like:

```
[2026-05-08T14:32:10-04:00] pusher started — job 12345 on r4519u01n01
[2026-05-08T14:32:10-04:00] config: droplet=monitor@... partitions=gpu,gpu_devel interval=60s
[2026-05-08T14:32:11-04:00] 30 pushes ok, 0 failed; uptime 30s
```

Hit Misha's URL on the droplet from any browser — data should appear.

## How the chain stays alive

- The job runs ≤ 23 h 30 m, then exits cleanly (walltime is 23:55:00,
  with a 25-minute buffer for the trap to run sbatch).
- The `trap on_exit EXIT` runs `sbatch --dependency=afterany:$JOBID self`
  before exiting, scheduling the successor regardless of exit code.
- SLURM starts the successor when the current one ends — usually
  within a minute on the `day` partition.
- **`--requeue`** is set, so if a node fails, SLURM re-queues
  automatically.

## Stopping the pusher

```bash
scancel -n monitor_pusher    # kills current and any queued successors
```

(Wait — if a successor was already submitted via the trap, you may need
to scancel by name twice within a minute.)

## What if the chain breaks?

If you don't see fresh data on the dashboard for >5 minutes, log in to
Misha and check:

```bash
squeue --me -n monitor_pusher
```

If empty, the chain broke (rare: usually because the trap's `sbatch`
failed). Just resubmit:

```bash
cd ~/cluster_monitor && sbatch pusher.sbatch
```

To detect this automatically, add a watchdog on the droplet that pings
you when the snapshot file is older than 5 minutes — see DEPLOY.md.

## Watching disk usage

The pusher logs to `~/monitor_pusher.<jobid>.log`. Old logs accumulate.
Add a cron-like cleanup (or just delete them periodically):

```bash
find ~/ -maxdepth 1 -name 'monitor_pusher.*.log' -mtime +14 -delete
```

You can run that ad-hoc, or schedule it inside the pusher.sbatch
trap if you want it automatic.

## Why `day` partition + 23h 30m exit?

Misha caps the `day` partition at 24 hours. We set walltime to 23:55:00
and exit at 23h 30m so the EXIT trap has a comfortable 25-minute window
to run `sbatch` for the successor before SIGKILL fires. (The trap
cannot fire on SIGKILL.) The `day` partition has fast-turnaround
scheduling, so the successor typically starts within a minute of the
predecessor exiting — well inside the dashboard's 5-minute staleness
budget.
