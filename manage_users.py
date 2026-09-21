#!/usr/bin/env python3
"""Tiny CLI for managing the users.json file used by app.py.

Day to day, use the dashboard's admin page (/admin) instead: it issues
invite links, re-issues them as password resets, and removes users. This
CLI is for bootstrapping a fresh droplet (first admin) and for emergencies.

Usage:
  python manage_users.py invite <username> [--display "Full Name"]   # prints a one-time link
  python manage_users.py admin <username> [--off]                    # grant / revoke admin
  python manage_users.py rename <old> <new>
  python manage_users.py add <username> [--display "Full Name"]      # set a password by hand
  python manage_users.py reset <username>
  python manage_users.py remove <username>
  python manage_users.py list

Set PUBLIC_URL (e.g. https://qingyuchen-lab-monitor.org) for `invite` to print
a full link; otherwise it prints the path.
"""

import argparse
import getpass
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

INVITE_TTL = int(os.environ.get("INVITE_TTL_SECONDS", str(7 * 24 * 3600)))

from werkzeug.security import generate_password_hash

USERS_FILE = Path(os.environ.get("USERS_FILE", Path(__file__).parent / "users.json"))


def load():
    if not USERS_FILE.exists():
        return {}
    data = json.loads(USERS_FILE.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def save(users):
    USERS_FILE.write_text(json.dumps(users, indent=2) + "\n")
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


def prompt_password():
    p1 = getpass.getpass("New password: ")
    p2 = getpass.getpass("Confirm:      ")
    if p1 != p2:
        sys.exit("Passwords do not match.")
    if len(p1) < 8:
        sys.exit("Password too short (min 8 chars).")
    return p1


def cmd_add(args):
    users = load()
    if args.username in users:
        sys.exit(f"User '{args.username}' already exists. Use 'reset' to change password.")
    pwd = prompt_password()
    users[args.username] = {
        "password": generate_password_hash(pwd),
        "display": args.display or args.username,
    }
    save(users)
    print(f"Added user: {args.username}")


def cmd_remove(args):
    users = load()
    if args.username not in users:
        sys.exit(f"User '{args.username}' not found.")
    del users[args.username]
    save(users)
    print(f"Removed user: {args.username}")


def cmd_reset(args):
    users = load()
    if args.username not in users:
        sys.exit(f"User '{args.username}' not found.")
    pwd = prompt_password()
    users[args.username]["password"] = generate_password_hash(pwd)
    save(users)
    print(f"Password reset for: {args.username}")


def cmd_invite(args):
    users = load()
    token = secrets.token_urlsafe(32)
    rec = users.get(args.username)
    if rec is None:
        rec = users[args.username] = {"password": None, "display": args.display or args.username}
    elif args.display:
        rec["display"] = args.display
    rec["invite"] = {"hash": hashlib.sha256(token.encode()).hexdigest(),
                     "expires": int(time.time()) + INVITE_TTL}
    save(users)
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    print(f"Invite link for {args.username} (valid {INVITE_TTL // 86400} days, one use):")
    print(f"  {base}/invite/{token}")


def cmd_admin(args):
    users = load()
    if args.username not in users:
        sys.exit(f"User '{args.username}' not found.")
    if args.off:
        users[args.username].pop("admin", None)
    else:
        users[args.username]["admin"] = True
    save(users)
    print(f"{args.username}: admin={'off' if args.off else 'on'}")


def cmd_rename(args):
    users = load()
    if args.old not in users:
        sys.exit(f"User '{args.old}' not found.")
    if args.new in users:
        sys.exit(f"User '{args.new}' already exists.")
    users[args.new] = users.pop(args.old)
    save(users)
    print(f"Renamed {args.old} -> {args.new} (password and flags kept)")


def cmd_list(args):
    users = load()
    if not users:
        print(f"(no users in {USERS_FILE})")
        return
    now = time.time()
    print(f"{len(users)} user(s) in {USERS_FILE}:")
    for u, rec in sorted(users.items()):
        flags = []
        if rec.get("admin"):
            flags.append("admin")
        inv = rec.get("invite")
        if inv:
            flags.append("invite pending" if inv.get("expires", 0) > now else "invite expired")
        if not rec.get("password"):
            flags.append("no password")
        print(f"  {u:<20}  {rec.get('display', ''):<24}  {' '.join(f'[{f}]' for f in flags)}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add"); a.add_argument("username"); a.add_argument("--display", default=None)
    a.set_defaults(func=cmd_add)
    r = sub.add_parser("remove"); r.add_argument("username"); r.set_defaults(func=cmd_remove)
    rs = sub.add_parser("reset"); rs.add_argument("username"); rs.set_defaults(func=cmd_reset)
    sub.add_parser("list").set_defaults(func=cmd_list)
    i = sub.add_parser("invite"); i.add_argument("username"); i.add_argument("--display", default=None)
    i.set_defaults(func=cmd_invite)
    ad = sub.add_parser("admin"); ad.add_argument("username"); ad.add_argument("--off", action="store_true")
    ad.set_defaults(func=cmd_admin)
    rn = sub.add_parser("rename"); rn.add_argument("old"); rn.add_argument("new"); rn.set_defaults(func=cmd_rename)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
