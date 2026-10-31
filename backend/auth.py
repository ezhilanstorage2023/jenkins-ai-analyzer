"""Local accounts (interim, until an identity provider is chosen) and roles."""

import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from store.db import get_session
from store.models import User

ROLES = ("viewer", "labeler", "operator")

_N, _R, _P = 16384, 8, 1
_basic = HTTPBasic(realm="remediation", auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32
    )

    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")

        if scheme != "scrypt":
            return False

        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(expected)),
        )
    except (ValueError, TypeError):
        return False

    return hmac.compare_digest(digest.hex(), expected)


# Verified against when the username is unknown, so response time does
# not reveal which accounts exist.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _unauthorized() -> HTTPException:
    return HTTPException(
        401,
        "Authentication required.",
        headers={"WWW-Authenticate": 'Basic realm="remediation"'},
    )


def current_user(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
    session: Session = Depends(get_session),
) -> User:
    if credentials is None:
        raise _unauthorized()

    user = session.query(User).filter_by(
        username=credentials.username
    ).one_or_none()

    stored = user.password_hash if user else _DUMMY_HASH

    if not verify_password(credentials.password, stored) or user is None:
        raise _unauthorized()

    return user


def require_role(role: str):
    minimum = ROLES.index(role)

    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in ROLES or ROLES.index(user.role) < minimum:
            raise HTTPException(403, "Insufficient role.")

        return user

    return dependency
