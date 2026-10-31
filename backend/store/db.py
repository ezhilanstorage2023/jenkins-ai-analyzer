from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from settings import get_settings

_engines: dict[str, Engine] = {}


def utcnow() -> datetime:
    """Naive UTC; every stored timestamp uses this."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_engine(url: str) -> Engine:
    if url == "sqlite://":
        # In-memory: one shared connection, usable across threads.
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        return create_engine(url, connect_args={"check_same_thread": False})

    return create_engine(url)


def get_engine() -> Engine:
    url = get_settings().database_url

    if url not in _engines:
        # Imported here so models can import this module without a cycle.
        from store.models import Base

        engine = _create_engine(url)

        # No migration tool until the first Postgres deployment.
        Base.metadata.create_all(engine)

        _engines[url] = engine

    return _engines[url]


def reset_engine_cache() -> None:
    for engine in _engines.values():
        engine.dispose()

    _engines.clear()


def SessionLocal() -> Session:
    return Session(get_engine(), expire_on_commit=False)


@contextmanager
def session_scope():
    session = SessionLocal()

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session():
    with session_scope() as session:
        yield session
