#!/bin/bash
# Wrapper invoked by SSH when the Misha pusher connects to the droplet's
# `monitor` user. The droplet's authorized_keys uses
#   command="/usr/local/bin/monitor-receive.sh"
# so the pusher's SSH key can ONLY run this script — no shell, no other
# command. The script reads stdin (the snapshot) and atomically replaces
# /var/lib/monitor/snapshot.txt.
#
# Install:
#   sudo cp deploy/monitor-receive.sh /usr/local/bin/
#   sudo chmod 755 /usr/local/bin/monitor-receive.sh
#   sudo mkdir -p /var/lib/monitor
#   sudo chown monitor:monitor /var/lib/monitor
#   sudo chmod 755 /var/lib/monitor

set -eu

DEST=/var/lib/monitor/snapshot.txt
TMP="${DEST}.$$"

# Refuse oversized input (> 8 MiB) — sanity check, real snapshots are ~50 KB
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
