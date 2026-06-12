"""Symmetric encryption for reversible secrets (e.g., share link passwords).

Uses Fernet (AES-128-CBC + HMAC-SHA256).

Key derivation:
  - If the optional env var ``SHARE_ENCRYPTION_KEY`` is set, the Fernet key is
    derived from it via SHA-256. This DECOUPLES share-password encryption from
    ``JWT_SECRET``, so rotating the JWT secret no longer makes stored share
    passwords undecryptable.
  - If ``SHARE_ENCRYPTION_KEY`` is unset, we FALL BACK to the legacy derivation
    of SHA-256(JWT_SECRET) so existing encrypted passwords keep working without
    a data migration.

To adopt a dedicated key safely: set ``SHARE_ENCRYPTION_KEY`` to the CURRENT
value of ``JWT_SECRET`` first (preserving decryptability of existing data),
then rotate ``JWT_SECRET`` independently.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet

try:
    from ..config import settings
except ImportError:
    from config import settings


def _key_source() -> str:
    """Return the secret string the Fernet key is derived from.

    Prefers the dedicated SHARE_ENCRYPTION_KEY env var; falls back to JWT_SECRET
    so existing encrypted share passwords remain decryptable.
    """
    dedicated = os.environ.get("SHARE_ENCRYPTION_KEY")
    if dedicated:
        return dedicated
    return settings.jwt_secret


def _get_fernet() -> Fernet:
    # Derive a 32-byte key from the key source using SHA-256, then base64-encode for Fernet
    key_bytes = hashlib.sha256(_key_source().encode()).digest()
    key_b64 = base64.urlsafe_b64encode(key_bytes)
    return Fernet(key_b64)


def encrypt_password(password: str) -> str:
    """Encrypt a password for reversible storage."""
    f = _get_fernet()
    return f.encrypt(password.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    """Decrypt a stored password back to plaintext."""
    f = _get_fernet()
    return f.decrypt(encrypted.encode()).decode()
