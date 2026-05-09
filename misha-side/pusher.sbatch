#!/bin/bash
#SBATCH --partition=day
#SBATCH --time=23:55:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=512M
#SBATCH --job-name=monitor_pusher
#SBATCH --output=monitor_pusher.%j.log
#SBATCH --requeue
#
# Polls sinfo/squeue on Misha and pushes the raw output to a DigitalOcean
# droplet over outbound SSH every $INTERVAL seconds. The droplet's Flask
# app reads the dropped file and renders the dashboard.
#
# Self-perpetuating: just before walltime expires, this script submits
# its own successor via --dependency=afterany so the chain never stops.
# Uses the `day` partition (24h max) — short-walltime slots schedule
# fast, so the predecessor→successor handoff is usually <1 min, well
# inside the dashboard's SNAPSHOT_MAX_AGE=300s freshness budget.
#
# First launch (one-time):  sbatch ~/cluster_monitor/pusher.sbatch
# After that the chain takes over. To stop:  scancel -n monitor_pusher
#
# To use a different partition (e.g. week): override at submission:
#   sbatch --partition=week --time=6-23:00:00 pusher.sbatch
# and bump EXIT_BEFORE_SECS below to match.

set -u

# ---------- CONFIG (edit these) ----------
DROPLET_USER="${DROPLET_USER:-monitor}"
DROPLET_HOST="${DROPLET_HOST:-203.0.113.10}"     # ← put your droplet IP here
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_monitor}"
PARTITIONS="${PARTITIONS:-gpu,gpu_devel}"
INTERVAL="${INTERVAL:-60}"                       # seconds between polls
EXIT_BEFORE_SECS=$((23 * 3600 + 30 * 60))        # 23h 30m — exit before 23:55 walltime
# -----------------------------------------

# Resolve absolute path to this script for the resubmit chain
SELF="$(realpath "$0")"

# Always submit a successor before exiting, regardless of how this job ends.
# afterany = run successor whether we succeed, fail, or get killed.
on_exit() {
    echo "[$(date -Is)] submitting successor (dependency: afterany:${SLURM_JOB_ID})"
    sbatch --dependency="afterany:${SLURM_JOB_ID}" \
           --kill-on-invalid-dep=yes \
           "$SELF" || echo "[$(date -Is)] WARN: successor sbatch failed (rc=$?)"
}
trap on_exit EXIT

echo "[$(date -Is)] pusher started — job ${SLURM_JOB_ID} on $(hostname)"
echo "[$(date -Is)] config: droplet=${DROPLET_USER}@${DROPLET_HOST} partitions=${PARTITIONS} interval=${INTERVAL}s"

SINFO_FMT='Partition:25,NodeHost:30,CPUsState:20,AllocMem:14,Memory:14,Gres:50,GresUsed:80,StateLong:18'
SQUEUE_R_FMT='NodeList:60,JobID:15,UserName:15,Account:25,TimeUsed:15,TimeLimit:15,TimeLeft:15,EndTime:22,tres-alloc:120,Name:60'
SQUEUE_PD_FMT='JobID:15,UserName:15,Account:25,Partition:15,Reason:25,TimeLimit:15,StartTime:22,tres-alloc:120,Name:60'

push_count=0
fail_count=0

while [ "$SECONDS" -lt "$EXIT_BEFORE_SECS" ]; do
    snapshot=$(
        echo "===META==="
        echo "generated_at $(date +%s)"
        echo "node $(hostname)"
        echo "job_id ${SLURM_JOB_ID}"
        echo "===SINFO==="
        sinfo -h -p "$PARTITIONS" -N -O "$SINFO_FMT" 2>&1
        echo "===SQUEUE_R==="
        squeue -h -p "$PARTITIONS" -t R -O "$SQUEUE_R_FMT" 2>&1
        echo "===SQUEUE_PD==="
        squeue -h -p "$PARTITIONS" -t PD -O "$SQUEUE_PD_FMT" 2>&1
    )

    if printf '%s\n' "$snapshot" | ssh \
        -o BatchMode=yes \
        -o ConnectTimeout=15 \
        -o ServerAliveInterval=15 \
        -o StrictHostKeyChecking=accept-new \
        -i "$SSH_KEY" \
        "${DROPLET_USER}@${DROPLET_HOST}" 2>>"$HOME/monitor_pusher_ssh.err"
    then
        push_count=$((push_count + 1))
    else
        fail_count=$((fail_count + 1))
        echo "[$(date -Is)] push failed (rc=$?) — total fails: $fail_count"
    fi

    if [ $((push_count % 30)) -eq 0 ] && [ "$push_count" -gt 0 ]; then
        echo "[$(date -Is)] $push_count pushes ok, $fail_count failed; uptime ${SECONDS}s"
    fi

    sleep "$INTERVAL"
done

echo "[$(date -Is)] approaching walltime — exiting cleanly so successor can take over"
echo "[$(date -Is)] final stats: pushed=$push_count failed=$fail_count"
exit 0
