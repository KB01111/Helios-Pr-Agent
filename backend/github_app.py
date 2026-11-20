"""GitHub credentials for backend API calls: GitHub App first, PAT fallback.

A GitHub App gives the agent its own bot identity and is the only way to
create Check Runs (the merge-readiness gate). When the App is configured the
backend mints a short-lived *installation token* per repository; otherwise it
uses the classic ``GITHUB_TOKEN`` (PAT) exactly as before.

Env:
  GITHUB_APP_ID                 numeric App id
  GITHUB_APP_PRIVATE_KEY        PEM text (literal ``\\n`` sequences are accepted)
  GITHUB_APP_PRIVATE_KEY_PATH   or a path to the .pem file
  GITHUB_APP_SLUG               optional; used for @mentions and status output
  GITHUB_TOKEN                  PAT fallback

Installation tokens are cached in process memory only (never in Redis): they
are bearer credentials and expire after one hour anyway. Installation ids are
not secret and are cached in the shared store (``ghinst:{owner/repo}``) so
every worker can resolve them without an extra API call.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

try:  # cwd=backend
    import store
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import store  # type: ignore[no-redef]

log = logging.getLogger("pr-agent.github-app")

GITHUB_API = "https://api.github.com"
_INSTALL_KEY = "ghinst:{repo}"
_INSTALL_TTL_S = 7 * 24 * 3600
# Refresh installation tokens this long before GitHub's expiry.
_TOKEN_SKEW_S = 300

_token_cache: dict[int, tuple[str, float]] = {}
_token_lock = threading.Lock()


class GitHubAppError(RuntimeError):
    """The App is configured but a token could not be obtained."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def app_id() -> str:
    return (os.getenv("GITHUB_APP_ID") or "").strip()


def app_slug() -> str:
    return (os.getenv("GITHUB_APP_SLUG") or "").strip()


def _private_key() -> str:
    raw = (os.getenv("GITHUB_APP_PRIVATE_KEY") or "").strip()
    if raw:
        # .env files and CI secrets often carry the PEM on one line.
        return raw.replace("\\n", "\n")
    path = (os.getenv("GITHUB_APP_PRIVATE_KEY_PATH") or "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("github_app: private key file unreadable (%s)", type(exc).__name__)
    return ""


def app_configured() -> bool:
    return bool(app_id() and _private_key())


def pat_token() -> str:
    return (os.getenv("GITHUB_TOKEN") or "").strip()


def auth_mode() -> str:
    """``app`` | ``pat`` | ``none`` — what backend GitHub calls will use."""
    if app_configured():
        return "app"
    if pat_token():
        return "pat"
    return "none"


# ---------------------------------------------------------------------------
# App JWT + installation tokens
# ---------------------------------------------------------------------------


def app_jwt(now: float | None = None) -> str:
    """RS256 JWT identifying the App (valid ~9 minutes, clock-skew tolerant)."""
    if not app_configured():
        raise GitHubAppError("GitHub App is not configured")
    try:
        import jwt  # PyJWT[crypto]
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise GitHubAppError("PyJWT[crypto] is not installed") from exc
    issued = int(now if now is not None else time.time())
    payload = {"iat": issued - 60, "exp": issued + 9 * 60, "iss": app_id()}
    try:
        return jwt.encode(payload, _private_key(), algorithm="RS256")
    except Exception as exc:
        # Never chain: the exception text can include key material.
        raise GitHubAppError(f"could not sign App JWT ({type(exc).__name__})") from None


def _app_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {app_jwt()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def remember_installation(repo: str, installation_id: Any) -> None:
    """Cache the installation id carried by an App webhook delivery."""
    try:
        inst = int(installation_id)
    except (TypeError, ValueError):
        return
    if inst <= 0 or not repo:
        return
    try:
        store.rset(_INSTALL_KEY.format(repo=repo.lower()), str(inst), ex=_INSTALL_TTL_S)
    except Exception as exc:
        log.warning("github_app: could not cache installation for %s: %s", repo, exc)


def forget_installation(repo: str) -> None:
    try:
        store.rdelete(_INSTALL_KEY.format(repo=repo.lower()))
    except Exception as exc:
        log.warning("github_app: could not drop installation for %s: %s", repo, exc)


def cached_installation(repo: str) -> int | None:
    raw = store.rget(_INSTALL_KEY.format(repo=repo.lower()))
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


async def installation_id_for(repo: str, client: httpx.AsyncClient | None = None) -> int | None:
    """Installation id for ``owner/name``: cache first, then ``GET /repos/{repo}/installation``."""
    cached = cached_installation(repo)
    if cached:
        return cached
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(f"{GITHUB_API}/repos/{repo}/installation", headers=_app_headers())
        if resp.status_code == 404:
            return None  # App not installed on this repo
        if resp.status_code != 200:
            raise GitHubAppError(f"installation lookup HTTP {resp.status_code}")
        inst = resp.json().get("id")
        remember_installation(repo, inst)
        return int(inst) if inst else None
    finally:
        if own:
            await client.aclose()


def _cached_token(installation_id: int) -> str | None:
    with _token_lock:
        item = _token_cache.get(installation_id)
        if item and item[1] - _TOKEN_SKEW_S > time.time():
            return item[0]
        _token_cache.pop(installation_id, None)
        return None


def _parse_expiry(stamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 50 * 60  # GitHub issues 60-minute tokens


async def installation_token(installation_id: int, client: httpx.AsyncClient | None = None) -> str:
    cached = _cached_token(installation_id)
    if cached:
        return cached
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers=_app_headers(),
        )
        if resp.status_code not in (200, 201):
            raise GitHubAppError(f"installation token HTTP {resp.status_code}")
        data = resp.json()
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError("installation token missing from response")
        with _token_lock:
            _token_cache[installation_id] = (token, _parse_expiry(data.get("expires_at")))
        return token
    finally:
        if own:
            await client.aclose()


async def token_for_repo(repo: str) -> tuple[str, str]:
    """``(token, kind)`` for API calls against ``repo``; kind is ``app``/``pat``/``none``.

    App mode falls back to the PAT when the App is not installed on the repo or
    GitHub refuses a token, so enabling the App never breaks existing setups.
    """
    if app_configured() and repo:
        try:
            inst = await installation_id_for(repo)
            if inst:
                return await installation_token(inst), "app"
            log.info("github_app: not installed on %s; using PAT fallback", repo)
        except GitHubAppError as exc:
            log.warning("github_app: %s for %s; using PAT fallback", exc, repo)
        except httpx.HTTPError as exc:
            log.warning("github_app: %s for %s; using PAT fallback", type(exc).__name__, repo)
    pat = pat_token()
    return (pat, "pat") if pat else ("", "none")


def clear_token_cache() -> None:
    with _token_lock:
        _token_cache.clear()
