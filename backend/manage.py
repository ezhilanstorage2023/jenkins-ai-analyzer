"""Account administration.

python -m manage create-user <username> --role viewer|labeler|operator
python -m manage set-password <username>
"""

import argparse
import getpass
import sys

from auth import ROLES, hash_password
from store.db import session_scope
from store.models import User


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

    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    main()
