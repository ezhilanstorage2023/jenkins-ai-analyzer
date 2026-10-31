"""Account administration.

python -m manage create-user <username> --role viewer|labeler|operator
python -m manage set-password <username>
python -m manage requeue --status failed [--event-id N]
"""

import argparse
import getpass
import sys

from auth import ROLES, hash_password
from store.db import session_scope, utcnow
from store.models import Event, User


def _read_password() -> str:
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")

    if not first:
        sys.exit("Password must not be empty.")

    if first != second:
        sys.exit("Passwords do not match.")

    return first


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m manage")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user")
    create.add_argument("username")
    create.add_argument("--role", choices=ROLES, required=True)

    reset = commands.add_parser("set-password")
    reset.add_argument("username")

    requeue = commands.add_parser("requeue")
    requeue.add_argument("--status", choices=["failed"], default="failed")
    requeue.add_argument("--event-id", type=int)

    args = parser.parse_args(argv)

    if args.command == "requeue":
        _requeue(args.status, args.event_id)
        return

    with session_scope() as session:
        user = session.query(User).filter_by(username=args.username).one_or_none()

        if args.command == "create-user":
            if user is not None:
                sys.exit(f"User {args.username!r} already exists.")

            session.add(User(
                username=args.username,
                password_hash=hash_password(_read_password()),
                role=args.role,
            ))
            print(f"Created {args.role} {args.username!r}.")

        else:
            if user is None:
                sys.exit(f"User {args.username!r} does not exist.")

            user.password_hash = hash_password(_read_password())
            print(f"Password updated for {args.username!r}.")


def _requeue(status: str, event_id: int | None) -> None:
    """Give failed events (e.g. after a Jenkins/VPN outage) a fresh start."""

    with session_scope() as session:
        query = session.query(Event).filter(Event.status == status)

        if event_id is not None:
            query = query.filter(Event.id == event_id)

        events = query.all()

        for event in events:
            event.status = "queued"
            event.attempts = 0
            event.last_error = None
            event.claimed_at = None
            event.next_attempt_at = utcnow()

        print(f"Requeued {len(events)} event(s).")


if __name__ == "__main__":
    main()
