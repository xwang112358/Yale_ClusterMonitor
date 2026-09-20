#!/bin/bash
# Wrapper invoked by SSH when a cluster pusher connects to the droplet's
# `monitor` user. The droplet's authorized_keys uses
#   command="/usr/local/bin/monitor-receive.sh"           (Misha — the original)
#   command="/usr/local/bin/monitor-receive.sh bouchet"   (any other cluster)
# so a pusher's SSH key can ONLY run this script — no shell, no other
# command. The cluster name comes from that forced command, never from the
# pushed bytes, so the KEY decides which file a push may write: a Misha key
# cannot overwrite Bouchet's snapshot or vice versa.
#
# The script reads stdin (the snapshot) and atomically replaces
#   /var/lib/monitor/snapshot.txt             (no argument — Misha)
#   /var/lib/monitor/<cluster>/snapshot.txt   (named cluster)
# which must match SNAPSHOT_FILE / <SLUG>_SNAPSHOT_FILE in the app's .env.
#
# Install:
#   sudo cp deploy/monitor-receive.sh /usr/local/bin/
#   sudo chmod 755 /usr/local/bin/monitor-receive.sh
#   sudo mkdir -p /var/lib/monitor
#   sudo chown monitor:monitor /var/lib/monitor
#   sudo chmod 755 /var/lib/monitor

set -eu

BASE="${MONITOR_BASE_DIR:-/var/lib/monitor}"   # override only for local testing
CLUSTER="${1:-}"

if [ -n "$CLUSTER" ]; then
    case "$CLUSTER" in
        *[!a-z0-9_-]*) echo "rejected: bad cluster name" >&2; exit 1 ;;
    esac
    mkdir -p "$BASE/$CLUSTER"
    DEST="$BASE/$CLUSTER/snapshot.txt"
else
    DEST="$BASE/snapshot.txt"
fi
TMP="${DEST}.$$"

# Refuse oversized input (> 8 MiB) — sanity check, real snapshots are ~50-250 KB
exec 0<&0
head -c 8388608 > "$TMP"

# Reject anything that doesn't look like our format
if ! head -1 "$TMP" | grep -q '^===META==='; then
    echo "rejected: missing META section" >&2
    rm -f "$TMP"
    exit 1
fi

# Atomic replace
mv -f "$TMP" "$DEST"
exit 0
