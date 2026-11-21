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

import asyncio
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
_discovered_slug: str = ""


class GitHubAppError(RuntimeError):
    """The App is configured but a token could not be obtained."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def app_id() -> str:
    return (os.getenv("GITHUB_APP_ID") or "").strip()


def app_slug() -> str:
    env_slug = (os.getenv("GITHUB_APP_SLUG") or "").strip()
    if env_slug:
        return env_slug
    with _token_lock:
        return _discovered_slug


def remember_app_slug(slug: Any) -> None:
    """Cache the live App slug discovered from ``GET /app`` when ``GITHUB_APP_SLUG`` is unset."""
    global _discovered_slug
    clean = str(slug or "").strip()
    if clean:
        with _token_lock:
            _discovered_slug = clean


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
    cached = await asyncio.to_thread(cached_installation, repo)
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
        await asyncio.to_thread(remember_installation, repo, inst)
        return int(inst) if inst else None
    finally:
        if own and hasattr(client, "aclose"):
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
        except Exception as exc:  # network errors, or the installation cache (Redis) failing
            log.warning("github_app: %s for %s; using PAT fallback", type(exc).__name__, repo)
    pat = pat_token()
    return (pat, "pat") if pat else ("", "none")


def clear_token_cache() -> None:
    global _discovered_slug
    with _token_lock:
        _token_cache.clear()
        _discovered_slug = ""


# ---------------------------------------------------------------------------
# Health: is the App set up the way the agent needs it?
# ---------------------------------------------------------------------------

# Mirrors src-tauri/src/github_app.rs (build_manifest / APP_EVENTS).
REQUIRED_PERMISSIONS: dict[str, str] = {
    "metadata": "read",
    "contents": "read",
    "pull_requests": "write",
    "issues": "write",
    "checks": "write",
    "statuses": "write",
}
REQUIRED_EVENTS: tuple[str, ...] = (
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request_review_thread",
    "issue_comment",
    "check_suite",
    "check_run",
    "status",
    "push",
)
_PERM_RANK = {"read": 1, "write": 2, "admin": 3}


def public_https_url(url: str) -> str:
    """Normalised ``https://host[/path]`` that GitHub can reach, or ``ValueError``."""
    import ipaddress
    from urllib.parse import urlsplit

    raw = (url or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("the webhook base URL must be https://")
    if parts.username or parts.password or parts.query or parts.fragment or any(c.isspace() for c in raw):
        raise ValueError("the webhook base URL must not contain credentials, a query, a fragment or spaces")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("GitHub cannot deliver webhooks to localhost")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return raw
    if not ip.is_global:
        raise ValueError("GitHub cannot deliver webhooks to a private address")
    return raw


def _permission_gaps(granted: dict[str, Any]) -> list[str]:
    gaps = []
    for name, need in REQUIRED_PERMISSIONS.items():
        have = str((granted or {}).get(name) or "")
        if _PERM_RANK.get(have, 0) < _PERM_RANK[need]:
            gaps.append(f"permission {name}: needs {need}, has {have or 'none'}")
    return gaps


def _check_app_info(data: dict[str, Any], out: dict[str, Any], problems: list[str]) -> None:
    slug = data.get("slug")
    if isinstance(slug, str) and slug.strip():
        remember_app_slug(slug)
    out.update(slug=slug, name=data.get("name"), html_url=data.get("html_url"))
    problems.extend(_permission_gaps(data.get("permissions") or {}))
    events = set(data.get("events") or [])
    missing = [e for e in REQUIRED_EVENTS if e not in events]
    if missing:
        problems.append("not subscribed to events: " + ", ".join(missing))


def _check_hook(resp: Any, out: dict[str, Any], problems: list[str]) -> None:
    if resp.status_code != 200:
        problems.append(f"could not read the App webhook config (HTTP {resp.status_code})")
        return
    hook_url = str((resp.json() or {}).get("url") or "")
    out["webhook_url"] = hook_url
    if not hook_url or "example.com" in hook_url:
        problems.append("the App's webhook URL is a placeholder; use 'Fix webhook URL'")
    if not (os.getenv("GITHUB_APP_WEBHOOK_SECRET") or "").strip():
        problems.append("GITHUB_APP_WEBHOOK_SECRET is not set on the backend, so deliveries cannot be verified")


def _check_installations(resp: Any, out: dict[str, Any], problems: list[str]) -> None:
    if resp.status_code != 200:
        problems.append(f"could not list installations (HTTP {resp.status_code})")
        return
    installs = [
        {
            "id": i.get("id"),
            "account": (i.get("account") or {}).get("login"),
            "repository_selection": i.get("repository_selection"),
            "suspended": bool(i.get("suspended_at")),
        }
        for i in (resp.json() or [])
        if isinstance(i, dict)
    ]
    out["installations"] = installs
    if not installs:
        problems.append("the App is not installed on any account or repository yet")
    if any(i["suspended"] for i in installs):
        problems.append("an installation is suspended")


async def health(client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Live check of the App: auth, permissions, events, webhook URL, installations.

    Never raises; ``problems`` lists everything the user should fix and ``ok``
    is true only when that list is empty.
    """
    if not app_configured():
        return {"configured": False, "ok": False, "problems": ["GitHub App credentials are not set on the backend"]}
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    out: dict[str, Any] = {"configured": True, "ok": False, "problems": []}
    problems: list[str] = out["problems"]
    try:
        headers = _app_headers()
        resp = await client.get(f"{GITHUB_API}/app", headers=headers)
        if resp.status_code != 200:
            problems.append(f"GitHub rejected the App credentials (HTTP {resp.status_code})")
            return out
        _check_app_info(resp.json() or {}, out, problems)
        _check_hook(await client.get(f"{GITHUB_API}/app/hook/config", headers=headers), out, problems)
        _check_installations(
            await client.get(f"{GITHUB_API}/app/installations", headers=headers, params={"per_page": 100}),
            out,
            problems,
        )
    except GitHubAppError as exc:
        problems.append(str(exc))
    except Exception as exc:
        problems.append(f"GitHub API unreachable ({type(exc).__name__})")
    finally:
        if own:
            await client.aclose()
    out["ok"] = not problems
    return out


async def set_webhook_url(base_url: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Point the App's webhook at ``<base_url>/webhooks/github`` (JSON, secret kept in sync)."""
    base = public_https_url(base_url)
    url = f"{base}/webhooks/github"
    body: dict[str, Any] = {"url": url, "content_type": "json", "insecure_ssl": "0"}
    secret = (os.getenv("GITHUB_APP_WEBHOOK_SECRET") or "").strip()
    if secret:
        body["secret"] = secret
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.patch(f"{GITHUB_API}/app/hook/config", headers=_app_headers(), json=body)
        if resp.status_code != 200:
            raise GitHubAppError(f"GitHub refused the webhook update (HTTP {resp.status_code})")
        return {"url": url, "secret_synced": bool(secret)}
    finally:
        if own:
            await client.aclose()
