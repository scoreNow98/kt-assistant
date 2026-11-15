"""Passwords, sessions and invites. Standard library only."""
import hashlib
import hmac
import secrets

SESSION_DAYS = 30
INVITE_DAYS = 7
_SCRYPT = dict(n=2**14, r=8, p=1)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


def check_password(password: str, stored: str) -> bool:
    salt, expected = stored.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT).hex()
    return hmac.compare_digest(actual, expected)


# Checked when the email is unknown, so a wrong email takes as long as a wrong password.
DUMMY_HASH = hash_password(secrets.token_hex(8))


def new_token() -> tuple[str, str]:
    """Returns (token to hand out, hash to store). Only the hash is kept in the database."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
