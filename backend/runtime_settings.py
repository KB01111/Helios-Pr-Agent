"""Cloud-side runtime settings, editable from the desktop app.

The desktop app used to write provider/model/keys only to a local
``backend/.env``, which a *deployed* FastAPI backend never sees. This module
keeps those settings in the shared store (Redis) so the app can configure the
cloud backend directly:

* Non-secret settings (``LLM_PROVIDER``, ``LLM_MODEL``, ...) are stored as-is.
* Secrets (API keys, the GitHub App private key, webhook secrets) are encrypted
  with Fernet before they touch Redis. The key is derived from
  ``SETTINGS_ENCRYPTION_KEY`` or, failing that, ``BACKEND_API_TOKEN``. With
  neither set the backend refuses to store secrets.
* Stored values are overlaid onto ``os.environ`` so every existing
  ``os.getenv("OPENROUTER_API_KEY")`` style read picks them up without changes.
  Stored values win over process env (that is the point: the UI must be able
  to change a value that a deploy template also set). Deleting a stored value
  restores whatever the process env had.

Other workers pick up changes through :func:`refresh`, which re-reads the
shared document at most every ``_REFRESH_TTL_S`` seconds.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

try:  # cwd=backend
    import store
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import store  # type: ignore[no-redef]

log = logging.getLogger("pr-agent.runtime-settings")

SETTINGS_KEY = "settings:runtime:v1"
_REFRESH_TTL_S = 10.0

# Non-secret: safe to show back to the desktop app.
PLAIN_KEYS: frozenset[str] = frozenset(
    {"LLM_PROVIDER", "LLM_MODEL", "VERCEL_TEAM_ID", "GITHUB_APP_ID", "GITHUB_APP_SLUG"}
)
# Encrypted at rest, never returned (only whether they are set).
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "OPENROUTER_API_KEY",
        "COMMANDCODE_API_KEY",
        "OPENCODE_GO_API_KEY",
        "GITHUB_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_WEBHOOK_SECRET",
        "UPSTASH_BOX_API_KEY",
        "VERCEL_TOKEN",
    }
)
ALL_KEYS: frozenset[str] = PLAIN_KEYS | SECRET_KEYS

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$")
_PEM_HEADER = "-----BEGIN"

_lock = threading.RLock()
_refresh_lock = threading.Lock()  # one read+apply at a time
_originals: dict[str, str | None] = {}  # process-env value before we overlaid a key
_applied: dict[str, str] = {}  # key -> value we put into os.environ
_errors: list[str] = []
_undecryptable: set[str] = set()
_last_refresh = 0.0


class SettingsError(ValueError):
    """The submitted settings are invalid."""


class SettingsNotReady(SettingsError):
    """The backend cannot store the settings yet (no encryption secret, or the store refused)."""


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


def _encryption_secret() -> str:
    return (os.getenv("SETTINGS_ENCRYPTION_KEY") or os.getenv("BACKEND_API_TOKEN") or "").strip()


def encryption_available() -> bool:
    return bool(_encryption_secret())


def _fernet() -> Any:
    from cryptography.fernet import Fernet  # PyJWT[crypto] already pulls it in

    secret = _encryption_secret()
    if not secret:
        raise SettingsNotReady(
            "set BACKEND_API_TOKEN (or SETTINGS_ENCRYPTION_KEY) on the backend before saving secrets to it"
        )
    digest = hashlib.sha256(("pr-agent-control/settings:" + secret).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _clean_value(key: str, raw: str) -> str:
    value = raw.strip()
    if key == "GITHUB_APP_PRIVATE_KEY":
        if len(value) > 10_000:
            raise SettingsError(f"{key} is too long")
        # Accept real newlines or literal \n; store the one-line form github_app expects.
        value = value.replace("\r\n", "\n").replace("\n", "\\n")
        if not value.startswith(_PEM_HEADER) or "PRIVATE KEY" not in value:
            raise SettingsError("GITHUB_APP_PRIVATE_KEY must be a PEM private key")
        # NUL and other control characters cannot live in an environment variable.
        if any(ord(ch) < 32 and ch != "\n" for ch in value.replace("\\n", "")):
            raise SettingsError("GITHUB_APP_PRIVATE_KEY contains control characters")
        return value
    if len(value) > 4096:
        raise SettingsError(f"{key} is too long")
    if any(ch in value for ch in "\r\n\x00") or any(ch.isspace() for ch in value):
        raise SettingsError(f"{key} must be a single token without whitespace")
    if key == "LLM_PROVIDER":
        providers = _llm_providers()
        value = value.lower()
        if value not in providers:
            raise SettingsError(f"LLM_PROVIDER must be one of: {', '.join(sorted(providers))}")
    elif key == "LLM_MODEL" and not _MODEL_RE.match(value):
        raise SettingsError("LLM_MODEL is not a valid model id")
    elif key == "GITHUB_APP_ID" and not value.isdigit():
        raise SettingsError("GITHUB_APP_ID must be numeric")
    elif key == "GITHUB_APP_SLUG" and not _SLUG_RE.match(value):
        raise SettingsError("GITHUB_APP_SLUG is not a valid App slug")
    return value


def _llm_providers() -> dict[str, Any]:
    try:
        from sandboxes.providers_review import LLM_PROVIDERS
    except ImportError:  # pragma: no cover
        from backend.sandboxes.providers_review import LLM_PROVIDERS  # type: ignore[no-redef]
    return LLM_PROVIDERS


# ---------------------------------------------------------------------------
# Persistence + overlay
# ---------------------------------------------------------------------------


def _load_doc() -> dict[str, Any]:
    doc = store.rget_json(SETTINGS_KEY)
    if isinstance(doc, dict) and isinstance(doc.get("values"), dict):
        return doc
    return {"values": {}}


def _decode(doc: dict[str, Any]) -> tuple[dict[str, str], list[str], set[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    broken: set[str] = set()
    for key, item in (doc.get("values") or {}).items():
        if key not in ALL_KEYS or not isinstance(item, dict):
            continue
        try:
            if "enc" in item:
                values[key] = _decrypt(str(item["enc"]))
            elif "v" in item and key in PLAIN_KEYS:
                values[key] = str(item["v"])
        except Exception:  # wrong/rotated key or corrupt token; never log the token
            broken.add(key)
            errors.append(f"{key}: stored value cannot be decrypted (was BACKEND_API_TOKEN / SETTINGS_ENCRYPTION_KEY rotated?)")
    return values, errors, broken


def _apply(values: dict[str, str]) -> None:
    """Make ``os.environ`` reflect ``values`` (stored wins), undoing keys that were removed."""
    with _lock:
        for key in list(_applied):
            if key not in values:
                original = _originals.pop(key, None)
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original
                _applied.pop(key, None)
        for key, value in values.items():
            if key not in _applied:
                _originals[key] = os.environ.get(key)
            os.environ[key] = value
            _applied[key] = value


def refresh(force: bool = False) -> None:
    """Re-read the shared document (rate limited). Never raises: a Redis blip must not break reviews."""
    global _last_refresh, _errors, _undecryptable
    now = time.monotonic()
    with _lock:
        if not force and now - _last_refresh < _REFRESH_TTL_S:
            return
    # Read, decode and apply as one step: an older read must not be applied after a
    # newer one (e.g. a slow refresh finishing after update() has returned).
    with _refresh_lock:
        if not force:
            with _lock:
                if time.monotonic() - _last_refresh < _REFRESH_TTL_S:
                    return
        try:
            values, errors, broken = _decode(_load_doc())
        except Exception as exc:
            log.warning("runtime settings: could not load (%s); keeping the last applied values", type(exc).__name__)
            with _lock:
                _last_refresh = time.monotonic()
            return
        _apply(values)
        with _lock:
            _errors = errors
            _undecryptable = broken
            _last_refresh = time.monotonic()


def webhook_secret_locked() -> bool:
    """True when a webhook secret is stored but unreadable (rotated key).

    Callers must then fail closed: treating "no secret" as dev mode would accept
    unsigned deliveries.
    """
    with _lock:
        return bool(_undecryptable & {"GITHUB_WEBHOOK_SECRET", "GITHUB_APP_WEBHOOK_SECRET"})


_LOCK_KEY = "settings:runtime:lock"


@contextlib.contextmanager
def _write_lock(wait_s: float = 5.0, lease_s: int = 15):
    """Serialise read-modify-write across workers (Redis NX lease)."""
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait_s
    while not store.rset(_LOCK_KEY, token, ex=lease_s, nx=True):
        if time.monotonic() >= deadline:
            raise SettingsNotReady("another settings update is in progress; try again")
        time.sleep(0.05)
    try:
        yield
    finally:
        try:
            store.rdelete_if_equal(_LOCK_KEY, token)
        except Exception as exc:  # the lease expires on its own
            log.warning("runtime settings: could not release lock (%s)", type(exc).__name__)


def update(changes: dict[str, str | None]) -> dict[str, Any]:
    """Apply ``{KEY: value}``; ``None``/empty deletes a key. Returns :func:`describe`."""
    if not isinstance(changes, dict) or not changes:
        raise SettingsError("no settings supplied")
    unknown = sorted(k for k in changes if k not in ALL_KEYS)
    if unknown:
        raise SettingsError("unknown or read-only setting(s): " + ", ".join(unknown))
    cleaned: dict[str, str | None] = {}
    for key, raw in changes.items():
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            cleaned[key] = None
        elif isinstance(raw, str):
            cleaned[key] = _clean_value(key, raw)
        else:
            raise SettingsError(f"{key} must be a string")
    if any(v is not None and k in SECRET_KEYS for k, v in cleaned.items()) and not encryption_available():
        _fernet()  # raises the actionable error

    with _write_lock():
        doc = _load_doc()
        stored = dict(doc.get("values") or {})
        for key, value in cleaned.items():
            if value is None:
                stored.pop(key, None)
            elif key in SECRET_KEYS:
                stored[key] = {"enc": _encrypt(value)}
            else:
                stored[key] = {"v": value}
        doc = {"values": stored, "updated_at": datetime.now(timezone.utc).isoformat()}
        if not store.rset_json(SETTINGS_KEY, doc):
            raise SettingsNotReady("could not write to the shared store")
    refresh(force=True)
    return describe()


def describe() -> dict[str, Any]:
    """Non-secret view for the desktop app: plain values, and only *whether* secrets are set."""
    with _lock:
        applied = dict(_applied)
        errors = list(_errors)
    plain: dict[str, str] = {}
    for key in sorted(PLAIN_KEYS):
        value = applied.get(key) or (os.getenv(key) or "").strip()
        if value:
            plain[key] = value
    secrets: dict[str, dict[str, bool]] = {}
    for key in sorted(SECRET_KEYS):
        secrets[key] = {"set": bool((os.getenv(key) or "").strip()), "cloud": key in applied}
    return {
        "persistent": store.redis_configured(),
        "encryption": encryption_available(),
        "values": plain,
        "cloud_keys": sorted(applied),
        "secrets": secrets,
        "errors": errors,
    }


def reset_for_tests() -> None:
    """Undo every overlay and forget cached state (tests only)."""
    global _last_refresh, _errors, _undecryptable
    with _lock:
        for key in list(_applied):
            original = _originals.pop(key, None)
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        _applied.clear()
        _originals.clear()
        _errors = []
        _undecryptable = set()
        _last_refresh = 0.0
