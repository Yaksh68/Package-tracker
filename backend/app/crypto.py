"""Encryption at rest for API keys and access tokens.

Values are encrypted with Fernet (AES-128-CBC plus an HMAC-SHA256 tag), under
a key derived with HKDF-SHA256 from APP_SECRET_KEY. Stored values look like
`enc:v1:<token>`. A value without that prefix is legacy plain text: it's
still readable, and re-encrypted the next time it's written (see
storage.encrypt_plaintext_tokens).

Key rotation: put the new key in APP_SECRET_KEY and the old one in
APP_SECRET_KEY_PREVIOUS. Values encrypted with either key decrypt, and new
writes use the new key.

The public mode requires APP_SECRET_KEY. A local install without one gets a
random key, generated once into LOCAL_SECRET_KEY_FILE (default
data/secret.key, which git ignores). Losing that file makes stored secrets
unreadable, and they'd have to be entered again.
"""
from __future__ import annotations

import base64
import os
import secrets as _secrets
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import get_settings

PREFIX = "enc:v1:"


class SecretsUnavailable(RuntimeError):
    pass


def _local_key(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _secrets.token_urlsafe(48)
    # create it readable by the owner only (no effect on Windows ACLs)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def _fernet(material: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"packages-tracker",
               info=b"secrets v1").derive(material.encode())
    return Fernet(base64.urlsafe_b64encode(key))


@lru_cache
def _box() -> MultiFernet:
    s = get_settings()
    materials = [k for k in (s.app_secret_key, s.app_secret_key_previous) if k]
    if not s.app_secret_key:
        if s.public_mode():
            raise SecretsUnavailable("APP_SECRET_KEY is required in the public mode")
        materials.insert(0, _local_key(Path(s.local_secret_key_file)))
    return MultiFernet([_fernet(m) for m in materials])


def reset() -> None:
    """Forget the cached keys (tests, and after changing settings)."""
    _box.cache_clear()


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)


def encrypt(plain: str) -> str:
    return PREFIX + _box().encrypt(plain.encode()).decode()


def decrypt(value: str) -> str:
    """Plain text for a stored value. Legacy plain-text values pass through."""
    if not is_encrypted(value):
        return value
    try:
        return _box().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise SecretsUnavailable(
            "a stored secret can't be decrypted: APP_SECRET_KEY changed without "
            "APP_SECRET_KEY_PREVIOUS?") from e


def hint(plain: str) -> str:
    """What the UI may show of a secret: its last four characters, and only
    when the secret is long enough that this reveals little."""
    return f"…{plain[-4:]}" if len(plain) >= 12 else "…"
