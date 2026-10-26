"""Provider API keys a live server run must keep across a restart
(docs/features/backend_authority.md §4.3, "API keys at rest").

A run's request is persisted so that a restart can resume it, and the
request carries the provider key the tab sent. Stored as it came, every run
ever made kept its key in plain text in metadata.db (and in every backup of
it). Now a live run's key is sealed with a key file beside the database —
readable only by the server's user — and a finished run keeps none.
"""
import os
import threading
from typing import Optional

import server_db

_lock = threading.Lock()
_cache: dict = {}


def key_path() -> str:
    """Beside the database, so a test's database gets its own key file."""
    return os.path.join(os.path.dirname(os.path.abspath(server_db.DB_PATH)), "run_secret.key")


def _fernet():
    from cryptography.fernet import Fernet
    path = key_path()
    with _lock:
        if path in _cache:
            return _cache[path]
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        with open(path, "rb") as f:
            fernet = Fernet(f.read().strip())
        _cache[path] = fernet
        return fernet


def seal(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def unseal(token: str) -> Optional[str]:
    """The secret, or None when the key file changed or the token is damaged."""
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
