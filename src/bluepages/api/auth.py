"""Accounts and sessions (Layer 10).

A production belongs to whoever ingested it, so the console shows one user's
work rather than every run the database has ever seen.

Passwords are PBKDF2-HMAC-SHA256 with a per-user salt, from the standard
library. That is not a substitute for a real identity provider, and this module
deliberately does not pretend otherwise: no password reset, no email
verification, no OAuth. It is enough to scope data to a user and no more.

Sessions are opaque random tokens in a table rather than signed cookies,
because a token that can be deleted server-side is the simpler thing to reason
about when a demo needs a logout that actually logs out.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Deliberately high enough to be honest work, low enough that a demo login is
# not a visible pause.
ITERATIONS = 240_000
SESSION_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def hash_password(password: str, salt: str | None = None) -> str:
    """`pbkdf2$iterations$salt$hash`, all hex, one column."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS
    )
    return f"pbkdf2${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. A malformed hash fails rather than raising."""
    try:
        scheme, iterations, salt, expected = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


class AuthError(RuntimeError):
    """Registration or login refused. The message is safe to show a user."""


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    name: str


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def register(db: Any, email: str, password: str, name: str = "") -> Account:
    """Create an account. Raises `AuthError` if the address is already used."""
    email = normalise_email(email)
    if "@" not in email or len(email) < 5:
        raise AuthError("Enter a valid email address.")
    if len(password) < 8:
        raise AuthError("Use a password of at least 8 characters.")

    if db.one("SELECT id FROM account WHERE email = ?", (email,)):
        raise AuthError("That email already has an account.")

    account_id = secrets.token_hex(12)
    db.execute(
        "INSERT INTO account (id, email, name, password_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_id, email, name.strip(), hash_password(password), _iso(_now())),
    )
    db.commit()
    return Account(id=account_id, email=email, name=name.strip())


def login(db: Any, email: str, password: str) -> Account:
    """Check a password. The failure message never says which half was wrong."""
    row = db.one(
        "SELECT id, email, name, password_hash FROM account WHERE email = ?",
        (normalise_email(email),),
    )
    if row is None or not verify_password(password, row["password_hash"]):
        raise AuthError("Email or password is incorrect.")
    return Account(id=row["id"], email=row["email"], name=row["name"])


def start_session(db: Any, account_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    db.execute(
        "INSERT INTO session (token, account_id, created_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (token, account_id, _iso(now), _iso(now + timedelta(days=SESSION_DAYS))),
    )
    db.commit()
    return token


def end_session(db: Any, token: str) -> None:
    db.execute("DELETE FROM session WHERE token = ?", (token,))
    db.commit()


def account_for(db: Any, token: str | None) -> Account | None:
    """The account behind a session token, or None if absent or expired."""
    if not token:
        return None
    row = db.one(
        "SELECT a.id, a.email, a.name, s.expires_at FROM session s "
        "JOIN account a ON a.id = s.account_id WHERE s.token = ?",
        (token,),
    )
    if row is None:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) < _now():
            db.execute("DELETE FROM session WHERE token = ?", (token,))
            db.commit()
            return None
    except (ValueError, TypeError):
        return None
    return Account(id=row["id"], email=row["email"], name=row["name"])


def claim_production(db: Any, production_id: str, account_id: str) -> None:
    """Record ownership. Idempotent, so re-running a production is harmless."""
    if db.one(
        "SELECT 1 FROM production_owner WHERE production_id = ? AND account_id = ?",
        (production_id, account_id),
    ):
        return
    db.execute(
        "INSERT INTO production_owner (production_id, account_id, role, created_at) "
        "VALUES (?, ?, 'owner', ?)",
        (production_id, account_id, _iso(_now())),
    )
    db.commit()


def owns(db: Any, production_id: str, account_id: str) -> bool:
    return (
        db.one(
            "SELECT 1 FROM production_owner WHERE production_id = ? AND account_id = ?",
            (production_id, account_id),
        )
        is not None
    )


def owned_production_ids(db: Any, account_id: str) -> list[str]:
    rows = db.query(
        "SELECT production_id FROM production_owner WHERE account_id = ?",
        (account_id,),
    )
    return [row["production_id"] for row in rows]
