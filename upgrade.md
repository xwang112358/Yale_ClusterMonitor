# Misha Monitor — Upgrade & Maintenance

Day-to-day operations after the initial deploy. Companion to
[DEPLOY.md](DEPLOY.md) (first-time setup),
[deployment_history.md](deployment_history.md) (this deployment's log),
and [DEPLOY_AZURE.md](DEPLOY_AZURE.md) (one-time rollout of the Azure
Usage `/azure` view).

All commands assume the values from the live deployment:
- Droplet: `root@159.223.173.141`, hostname `mishamonitor.duckdns.org`
- App lives at `/home/monitor/ClusterMonitor` on the droplet, owned by `monitor`
- Misha pusher lives at `~/project/cluster_monitor` on Misha as user `xw532`

---

## Dashboard users

The Flask app re-reads `users.json` on every login — none of these need a
service restart.

### Add a user

```bash
ssh root@159.223.173.141
sudo -u monitor /home/monitor/ClusterMonitor/.venv/bin/python \
    /home/monitor/ClusterMonitor/manage_users.py add <login> --display "Display Name"
# prompts for password twice
```

### Reset a user's password

```bash
sudo -u monitor /home/monitor/ClusterMonitor/.venv/bin/python \
    /home/monitor/ClusterMonitor/manage_users.py reset <login>
```

### Remove a user

```bash
sudo -u monitor /home/monitor/ClusterMonitor/.venv/bin/python \
    /home/monitor/ClusterMonitor/manage_users.py remove <login>
```

### List users

```bash
sudo -u monitor /home/monitor/ClusterMonitor/.venv/bin/python \
    /home/monitor/ClusterMonitor/manage_users.py list
```

### Force every user to re-login (rotate SECRET_KEY)

If you suspect a session cookie leak, or just want to invalidate every
login session:

```bash
NEW=$(python3 -c "import secrets; print(secrets.token_hex(32))")
sudo -u monitor sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$NEW/" /home/monitor/ClusterMonitor/.env
systemctl restart misha-monitor
```

---

## Droplet hardening (one-time)

Recommended baseline for the droplet. None of these are
deployment-critical — the dashboard works without them — but they
substantially reduce the realistic risk of port-22 brute-force, unpatched
CVEs, and stolen-credential reuse. Five minutes total, run as root on
the droplet.

### 1. Verify SSH is key-only

DigitalOcean defaults to key-only auth when you provided an SSH key at
droplet-creation time. Confirm:

```bash
grep -E '^(PermitRootLogin|PasswordAuthentication|PubkeyAuthentication)' /etc/ssh/sshd_config
```

Want:
- `PasswordAuthentication no`
- `PubkeyAuthentication yes`
- `PermitRootLogin prohibit-password` (key-only) or `no`

If anything is set to `yes` on password auth, edit `/etc/ssh/sshd_config`
then `systemctl restart ssh`. **Test with a brand-new SSH session before
closing the existing one** — if you locked yourself out, recover via
DigitalOcean's web console.

### 2. Firewall — only 22 / 80 / 443

```bash
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80,443/tcp
ufw --force enable
ufw status
```

If Caddy ever fails to provision a cert after enabling ufw, the most
likely cause is 80/tcp got dropped from the allow list — TLS-ALPN-01
challenges don't need 80, but Let's Encrypt's HTTP-01 fallback does.

### 3. Automatic security updates

```bash
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades    # answer "yes"
```

Default policy installs *security* updates only — won't pull random
feature releases. Doesn't auto-reboot (deliberate; an auto-reboot would
kill gunicorn mid-request). Skim the journal occasionally:

```bash
journalctl -u unattended-upgrades --since '7 days ago'
```

### 4. fail2ban for SSH brute-force

```bash
apt install -y fail2ban
systemctl enable --now fail2ban
fail2ban-client status sshd
```

Default jail bans an IP after 5 failed SSH attempts in 10 min. Within
hours of enabling it, the volume of `Invalid user … from x.x.x.x` lines
in `journalctl -u ssh` drops dramatically.

### 5. Account hygiene (off the droplet)

The dashboard is also gated by external accounts you control. Each is a
potential redirect path to a phishing page that mimics the login form.

- **DuckDNS** (or whatever DNS provider holds the A record): enable 2FA
  on the account you signed in with (GitHub/Google/Reddit). If that
  account is compromised, `mishamonitor.duckdns.org` can be repointed
  away from your droplet.
- **GitHub** account that owns the repo + any deploy keys: enable 2FA.
- **Dashboard passwords** in `users.json`: strong + unique. Anything
  common like `username123` is broken in one dictionary pass even with
  bcrypt-style hashing. `manage_users.py reset <user>` whenever a member
  rotates passwords.

### 6. Periodic sanity check

Once a quarter, or any time something feels off:

```bash
# Recent SSH activity
last -n 20
journalctl -u ssh -p warning --since '30 days ago' | tail -50
fail2ban-client status sshd          # currently banned IPs

# What's listening on the network?
ss -tlnp
# Expected: 22 (sshd), 80 + 443 (caddy), 127.0.0.1:5111 (gunicorn).
# Anything else on a public port is a red flag.

# Service + snapshot health
systemctl status misha-monitor caddy
stat /var/lib/monitor/snapshot.txt   # mtime should be <60s old
```

### 7. Optional: rotate the dashboard's SECRET_KEY periodically

Already documented above under "Force every user to re-login." Worth
doing if you ever suspect a session cookie leaked or a labmate's laptop
was lost.

### Threat model — quick reference

| Layer | Risk level | Mitigation |
|---|---|---|
| Droplet root SSH | High | Steps 1, 2, 4 above |
| Droplet packages (CVEs) | Medium | Step 3 above |
| `users.json` password hashes | Medium | Strong passwords (Step 5), restrict file to `0600 monitor:monitor` |
| Caddy / Flask app routes | Low | Mature stack, all routes behind `@login_required` |
| `monitor` user (receive script) | Low | `command="…"` lockdown — no shell, no sudo, can only invoke the receiver |
| Misha pusher key (`~/.ssh/id_ed25519_monitor`) | Low–medium | If stolen, attacker can only invoke the lockdown receive script. Rotate via the key-rotation procedure below |
| DuckDNS / DNS account | Medium | 2FA (Step 5) |

---

## Update the dashboard's domain name

E.g., moving from `mishamonitor.duckdns.org` to a real domain like
`misha.example.com`.

1. **Set up the new DNS A record** (whatever registrar / DNS provider you
   use) → `159.223.173.141`. Wait for it to resolve:

   ```bash
   getent hosts misha.example.com   # should print 159.223.173.141
   ```

2. **Patch the Caddyfile**:

   ```bash
   ssh root@159.223.173.141
   sed -i 's#mishamonitor\.duckdns\.org#misha.example.com#g' /etc/caddy/Caddyfile
   systemctl reload caddy
   journalctl -u caddy -n 50 --no-pager        # watch for "certificate obtained successfully"
   ```

3. Verify in the browser at the new URL.

The old name keeps working until DNS stops resolving it; you can leave
both as separate site blocks in the Caddyfile if you want a graceful
overlap.

---

## Update the droplet's IP address

E.g., DO reassigned the floating IP, or you migrated to a new droplet.

1. **Update DuckDNS** (or your DNS provider) to point the hostname at the
   new IP. Wait for `getent hosts mishamonitor.duckdns.org` to return the
   new IP.

2. **Update the Misha pusher's destination**:

   ```bash
   ssh xw532@misha.ycrc.yale.edu
   cd ~/project/cluster_monitor
   sed -i 's#DROPLET_HOST:-159\.0*\.0*\.0*#DROPLET_HOST:-NEW.IP.HERE#' pusher.sbatch
   grep DROPLET_HOST pusher.sbatch     # confirm
   scancel -n monitor_pusher           # kill the chain
   sbatch pusher.sbatch                # restart on the new IP
   ```

   (If the `sed` regex doesn't match, just open `pusher.sbatch` and edit
   the `DROPLET_HOST="${DROPLET_HOST:-...}"` line directly.)

3. Caddy doesn't care about the IP — it binds to whatever interface is
   present. Nothing to do there.

---

## Change the partitions you monitor

E.g., add `pi_chen` to the list.

The partition list lives in two places that **must agree**:

1. The pusher polls a fixed list (Misha-side):

   ```bash
   ssh xw532@misha.ycrc.yale.edu
   cd ~/project/cluster_monitor
   sed -i 's#PARTITIONS:-gpu,gpu_devel#PARTITIONS:-gpu,gpu_devel,pi_chen#' pusher.sbatch
   grep PARTITIONS pusher.sbatch
   scancel -n monitor_pusher
   sbatch pusher.sbatch
   ```

2. The dashboard renders a fixed list (droplet-side, in `.env`):

   ```bash
   ssh root@159.223.173.141
   sudo -u monitor sed -i 's#^MISHA_PARTITIONS=.*#MISHA_PARTITIONS=gpu,gpu_devel,pi_chen#' \
       /home/monitor/ClusterMonitor/.env
   systemctl restart misha-monitor
   ```

If they diverge, you'll see partitions in one view and not the other.

---

## Change the lab account / lab netids displayed

These are pure cosmetics in `.env` on the droplet:

```bash
ssh root@159.223.173.141
sudo -u monitor nano /home/monitor/ClusterMonitor/.env
# edit LAB_ACCOUNT=  (drives the "Lab Queue" panel)
# edit LAB_NETIDS=    (comma-separated; these get highlighted blue)
systemctl restart misha-monitor
```

`LAB_ACCOUNT=` (empty) hides the Lab Queue panel entirely.

---

## Pull updates to the Flask app from GitHub

When the upstream repo has new code:

```bash
ssh root@159.223.173.141
sudo -u monitor bash -lc '
  cd ~/ClusterMonitor
  git pull --ff-only
  .venv/bin/pip install -r requirements.txt   # in case deps changed
'
systemctl restart misha-monitor
systemctl status misha-monitor --no-pager
```

If the systemd unit (`deploy/misha-monitor.service`) or the Caddyfile
(`deploy/Caddyfile`) changed upstream, you'll need to re-render those
into `/etc/...`:

```bash
sed 's/REPLACE_ME_USERNAME/monitor/g' \
    /home/monitor/ClusterMonitor/deploy/misha-monitor.service \
    > /etc/systemd/system/misha-monitor.service
sed 's#misha\.example\.com#mishamonitor.duckdns.org#g' \
    /home/monitor/ClusterMonitor/deploy/Caddyfile \
    > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl restart misha-monitor
systemctl reload caddy
```

If `monitor-receive.sh` changed:

```bash
cp /home/monitor/ClusterMonitor/deploy/monitor-receive.sh /usr/local/bin/
chmod 755 /usr/local/bin/monitor-receive.sh
# next push picks it up automatically — no service restart needed
```

---

## Pull updates to the Misha pusher

The pusher is just `~/project/cluster_monitor/pusher.sbatch`. To update:

```bash
ssh xw532@misha.ycrc.yale.edu
cd /tmp
rm -rf Yale_ClusterMonitor
git clone https://github.com/xwang112358/Yale_ClusterMonitor.git
diff ~/project/cluster_monitor/pusher.sbatch \
     Yale_ClusterMonitor/misha-side/pusher.sbatch
# review the diff. If you want the upstream version:
cp Yale_ClusterMonitor/misha-side/pusher.sbatch ~/project/cluster_monitor/
# re-apply local edits (DROPLET_HOST etc.) — see the Misha M3 step in DEPLOY.md
scancel -n monitor_pusher       # kill old chain
sbatch ~/project/cluster_monitor/pusher.sbatch
rm -rf /tmp/Yale_ClusterMonitor
```

---

## Manage the Misha pusher chain

### Status

```bash
ssh xw532@misha.ycrc.yale.edu
squeue --me -n monitor_pusher
tail -n 50 ~/project/cluster_monitor/monitor_pusher.*.log | tail -n 50
```

### Stop the chain

```bash
scancel -n monitor_pusher
```

The successor was scheduled with `--dependency=afterany:<old>` and
`--kill-on-invalid-dep=yes`, so cancelling the running job kills the
queued successor too.

### Restart the chain

```bash
cd ~/project/cluster_monitor
sbatch pusher.sbatch
```

### Replace the Misha push SSH key (key rotation)

```bash
ssh xw532@misha.ycrc.yale.edu

# 1. Generate a new key
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519_monitor.new -C "misha-monitor-pusher"
cat ~/.ssh/id_ed25519_monitor.new.pub

# 2. On the droplet, append the NEW key with the same lockdown line, then
#    test one push with -i ~/.ssh/id_ed25519_monitor.new.
#    Once verified, remove the OLD key from /home/monitor/.ssh/authorized_keys.

# 3. On Misha, swap the key files in place:
mv ~/.ssh/id_ed25519_monitor     ~/.ssh/id_ed25519_monitor.old
mv ~/.ssh/id_ed25519_monitor.new ~/.ssh/id_ed25519_monitor
mv ~/.ssh/id_ed25519_monitor.new.pub ~/.ssh/id_ed25519_monitor.pub

# 4. Restart the pusher to pick up the new key:
scancel -n monitor_pusher
sbatch ~/project/cluster_monitor/pusher.sbatch

# 5. Once you've confirmed the new key works for ~5 min, delete the old:
rm ~/.ssh/id_ed25519_monitor.old
```

---

## Service control on the droplet

```bash
# Flask app
systemctl status misha-monitor       # is it up?
systemctl restart misha-monitor      # bounce it
journalctl -u misha-monitor -f       # tail live logs

# Caddy (TLS / reverse proxy)
systemctl status caddy
systemctl reload caddy               # reread Caddyfile, no downtime
journalctl -u caddy -f
tail -f /var/log/caddy/misha-monitor.log

# Snapshot freshness
stat /var/lib/monitor/snapshot.txt   # mtime should be <60s old
```

---

## Common breakages

### Dashboard says "snapshot file missing"

The pusher hasn't successfully pushed yet, **ever**. On Misha:

```bash
squeue --me -n monitor_pusher
```

- Empty? Chain isn't running. `cd ~/project/cluster_monitor && sbatch pusher.sbatch`.
- `R` (running)? Tail the log:
  ```bash
  tail -f ~/project/cluster_monitor/monitor_pusher.*.log
  cat ~/monitor_pusher_ssh.err          # SSH errors land here
  ```
  Most common cause: the public key in the droplet's `monitor`
  authorized_keys is wrong, or the lockdown prefix is malformed.

### Dashboard says "snapshot is N seconds old (limit 300s)"

Pusher hasn't pushed in 5+ min. Same diagnostic as above. If
`squeue --me -n monitor_pusher` is empty but you didn't cancel it, the
successor failed to submit (rare) — just `sbatch pusher.sbatch` again.

### Pusher SSH fails with "Permission denied (publickey)"

The lockdown line in the droplet's `authorized_keys` doesn't match the
private key on Misha. Verify on the droplet:

```bash
sudo cat /home/monitor/.ssh/authorized_keys
# Each line must start with command="/usr/local/bin/monitor-receive.sh"
# and end with the exact public key body from Misha's ~/.ssh/id_ed25519_monitor.pub
```

### Pusher SSH fails with "rejected: missing META section"

The receiver script saw bytes that didn't start with `===META===`.
Usually means a `sinfo`/`squeue` invocation failed on Misha and dumped
its error to stdout. Re-run the M4 manual test in
[DEPLOY.md](DEPLOY.md) and inspect what's actually being piped.

### Login page loops back to itself after a successful login

`SECRET_KEY` is changing across restarts. Make sure it's set explicitly
in `.env` (not relying on the auto-generated `.flask_secret`):

```bash
grep SECRET_KEY /home/monitor/ClusterMonitor/.env
```

If it's empty or commented out, set it to a long random hex string and
restart the service.

### Caddy can't get a certificate

Most likely DNS hasn't propagated, or port 80 is blocked. Check:

```bash
getent hosts mishamonitor.duckdns.org   # should be 159.223.173.141
ufw status                              # if "active", make sure 80/443 are allowed
journalctl -u caddy -n 100 --no-pager
```

---

## Rebuilding from scratch

If the droplet image is wiped (or you migrate to a new droplet):

1. Walk through [DEPLOY.md](DEPLOY.md) Phase D from D1.
2. The Misha side **is unchanged** — same key, same `pusher.sbatch`.
   Once the new droplet has the same `monitor`-user public key in its
   `authorized_keys` (Step M2's lockdown line), the existing SLURM job
   resumes pushing on the next cycle. No need to regenerate the Misha
   key.
3. If the droplet IP changed, also do "Update the droplet's IP address"
   above.
4. Locally on your laptop, run `ssh-keygen -R 159.223.173.141` (or the
   new IP) before reconnecting, so SSH accepts the new host key.

---

## Optional: stale-snapshot watchdog

If you want an alert when the snapshot goes stale, add a systemd timer
on the droplet (per the "Optional: stale-snapshot watchdog" section of
DEPLOY.md). Requires `mailutils` or any other mail-capable transport.
