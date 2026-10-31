"""Queue recent failed builds of a job for shadow classification.

python -m backfill --job <folder/name> --count 50
"""

import argparse

from clients.jenkins import JenkinsClient, build_url_for, jenkins_client_from_settings
from store.db import session_scope, utcnow
from store.models import Event


def backfill(
    jenkins: JenkinsClient,
    job: str,
    count: int,
    controller_id: str = "default",
) -> tuple[int, int]:
    """Returns (queued, skipped). Read-only against Jenkins."""

    queued = skipped = 0

    with session_scope() as session:
        for build in jenkins.list_builds(job, count):
            if build.result != "FAILURE":
                continue

            exists = session.query(Event.id).filter_by(
                controller_id=controller_id,
                job=job,
                build_number=build.number,
            ).first()

            if exists:
                skipped += 1
                continue

            now = utcnow()
            session.add(Event(
                controller_id=controller_id,
                job=job,
                build_number=build.number,
                # Built from our configured base, not Jenkins' reported root.
                build_url=build_url_for(jenkins.base_url, job, build.number),
                result=build.result,
                source="backfill",
                next_attempt_at=now,
                received_at=now,
            ))
            queued += 1

    return queued, skipped


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m backfill")
    parser.add_argument("--job", required=True, help="Jenkins job path, e.g. folder/name")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--controller", default="default")
    args = parser.parse_args(argv)

    queued, skipped = backfill(
        jenkins_client_from_settings(),
        args.job,
        args.count,
        args.controller,
    )
    print(f"Queued {queued} failed build(s); skipped {skipped} already known.")


if __name__ == "__main__":
    main()
