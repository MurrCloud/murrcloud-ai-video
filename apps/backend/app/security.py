import hashlib
import hmac
import secrets
import threading
from datetime import UTC, datetime, timedelta

from pwdlib import PasswordHash

password_hasher = PasswordHash.recommended()
# Verify a dummy hash for unknown users to reduce username enumeration by timing.
DUMMY_PASSWORD_HASH = password_hasher.hash(secrets.token_urlsafe(32))
SESSION_COOKIE = "video_session"
PASSWORD_SLOTS = threading.BoundedSemaphore(1)


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 256:
        raise ValueError("Password must contain 12–256 characters")
    return password_hasher.hash(password)


def verify_password(password: str, stored: str) -> bool:
    try:
        return password_hasher.verify(password, stored)
    except Exception:
        return False


def token_hash(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def new_session(secret: str, hours: int) -> tuple[str, str, datetime]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token, secret), datetime.now(UTC) + timedelta(hours=hours)


def aware_utc(value: datetime) -> datetime:
    # SQLite's unit-test DateTime round trip removes tzinfo; PostgreSQL keeps it.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
