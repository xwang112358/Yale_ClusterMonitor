#!/usr/bin/env python3
"""Tiny CLI for managing the users.json file used by app.py.

Usage:
  python manage_users.py add <username> [--display "Full Name"]
  python manage_users.py remove <username>
  python manage_users.py list
  python manage_users.py reset <username>
"""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

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


def cmd_list(args):
    users = load()
    if not users:
        print(f"(no users in {USERS_FILE})")
        return
    print(f"{len(users)} user(s) in {USERS_FILE}:")
    for u, rec in sorted(users.items()):
        print(f"  {u:<20}  {rec.get('display', '')}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add"); a.add_argument("username"); a.add_argument("--display", default=None)
    a.set_defaults(func=cmd_add)
    r = sub.add_parser("remove"); r.add_argument("username"); r.set_defaults(func=cmd_remove)
    rs = sub.add_parser("reset"); rs.add_argument("username"); rs.set_defaults(func=cmd_reset)
    sub.add_parser("list").set_defaults(func=cmd_list)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
