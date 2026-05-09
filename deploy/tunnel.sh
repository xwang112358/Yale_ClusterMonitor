#!/usr/bin/env bash
# Plain-ssh while-loop fallback for the reverse tunnel, in case autossh
# isn't installable on the Yale host. Runs the same -R forward, restarts
# on disconnect. Use the systemd unit if you can; this is the rescue.
#
# Edit the four variables below, then:
#   chmod +x deploy/tunnel.sh
#   nohup deploy/tunnel.sh >/var/log/misha-tunnel.log 2>&1 &
#
# Or wrap it in a tiny systemd unit if autossh truly isn't an option.

set -u

DROPLET_USER="tunnel"
DROPLET_HOST="137.184.145.140"
SSH_KEY="$HOME/.ssh/id_ed25519_misha_do"
LOCAL_PORT=5111

while true; do
    echo "[$(date -Is)] starting reverse tunnel"
    ssh -N -T \
        -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o StrictHostKeyChecking=accept-new \
        -i "$SSH_KEY" \
        -R "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${LOCAL_PORT}" \
        "${DROPLET_USER}@${DROPLET_HOST}"
    echo "[$(date -Is)] tunnel exited (rc=$?), reconnecting in 5s"
    sleep 5
done
