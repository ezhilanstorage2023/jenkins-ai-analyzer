import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import manage
from auth import hash_password, require_role, verify_password
from store.db import session_scope
from store.models import User

app = FastAPI()


@app.get("/v")
def viewer_route(user: User = Depends(require_role("viewer"))):
    return {"user": user.username}


@app.get("/l")
def labeler_route(user: User = Depends(require_role("labeler"))):
    return {"user": user.username}


client = TestClient(app)


def test_hash_roundtrip_and_salted():
    first, second = hash_password("pw"), hash_password("pw")

    assert first != second
    assert first.startswith("scrypt$16384$8$1$")
    assert verify_password("pw", first)
    assert not verify_password("nope", first)


def test_verify_rejects_malformed_hash():
    assert verify_password("pw", "not-a-hash") is False


def test_unauthenticated_gets_basic_challenge():
    response = client.get("/v")

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_wrong_password_401(users):
    assert client.get("/v", auth=("viewer1", "bad")).status_code == 401


def test_unknown_user_401(users):
    assert client.get("/v", auth=("ghost", "pw")).status_code == 401


def test_role_hierarchy(users):
    assert client.get("/v", auth=("labeler1", "pw")).json() == {"user": "labeler1"}
    assert client.get("/l", auth=("operator1", "pw")).status_code == 200
    assert client.get("/l", auth=("viewer1", "pw")).status_code == 403


def test_manage_create_user(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda *_: "pw")

    manage.main(["create-user", "alice", "--role", "labeler"])

    with session_scope() as session:
        user = session.query(User).filter_by(username="alice").one()
        assert user.role == "labeler"
        assert verify_password("pw", user.password_hash)


def test_manage_rejects_duplicate_and_mismatch(monkeypatch, users):
    monkeypatch.setattr("getpass.getpass", lambda *_: "pw")

    with pytest.raises(SystemExit):
        manage.main(["create-user", "viewer1", "--role", "viewer"])

    answers = iter(["a", "b"])
    monkeypatch.setattr("getpass.getpass", lambda *_: next(answers))

    with pytest.raises(SystemExit):
        manage.main(["create-user", "bob", "--role", "viewer"])


def test_manage_set_password(monkeypatch, users):
    monkeypatch.setattr("getpass.getpass", lambda *_: "new-pw")

    manage.main(["set-password", "viewer1"])

    with session_scope() as session:
        user = session.query(User).filter_by(username="viewer1").one()
        assert verify_password("new-pw", user.password_hash)


def test_manage_requeue_failed_events():
    from store.db import utcnow
    from store.models import Event

    with session_scope() as session:
        session.add(Event(
            controller_id="default", job="j", build_number=1,
            build_url="https://ci.example.com/job/j/1/", source="webhook",
            status="failed", attempts=3, last_error="JenkinsError: HTTP 503",
            next_attempt_at=utcnow(), received_at=utcnow(),
        ))

    manage.main(["requeue", "--status", "failed"])

    with session_scope() as session:
        event = session.query(Event).one()
        assert (event.status, event.attempts, event.last_error) == ("queued", 0, None)
