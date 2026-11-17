"""Shared Redis helper for the PR-Agent cloud backend.

Priority:
  1. `upstash_redis` package (if installed AND env configured).
  2. Raw Upstash REST calls via httpx (sync + async variants).
  3. In-memory dict fallback (local dev without Redis env vars).

The rest of the backend (main.py, sandboxes/router.py) should import
from this module so they share one connection/singleton and one
fallback store:

    from store import rget, rset, rincr, rkeys, rdelete, rget_json, rset_json
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse

log = logging.getLogger("pr-agent.store")

_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# In-memory fallback: {key: (value_str, expires_at|None)}
_MEM: dict[str, tuple[str | None, float | None]] = {}
_MEM_LOCK = threading.RLock()

# If Redis credentials are configured, never silently fall back to process
# memory for writes: in a multi-worker deployment that creates records only
# one worker can see and then makes reads fail. Fail closed instead.
_REDIS_CONFIGURED = bool(_REST_URL and _REST_TOKEN)


def _mem_get(key: str) -> str | None:
    with _MEM_LOCK:
        item = _MEM.get(key)
        if item is None:
            return None
        val, exp = item
        if exp is not None and time.time() > exp:
            _MEM.pop(key, None)
            return None
        return val


def _mem_set(key: str, val: str, ex: int | None = None, nx: bool = False) -> bool:
    with _MEM_LOCK:
        if nx and _mem_get(key) is not None:
            return False
        exp = (time.time() + ex) if ex else None
        _MEM[key] = (val, exp)
        return True


def _mem_keys(pattern: str) -> list[str]:
    # Minimal glob: supports trailing "*" only, plus exact match.
    with _MEM_LOCK:
        _mem_purge()
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            return [k for k in _MEM if k.startswith(prefix)]
        return [pattern] if _mem_get(pattern) is not None else []


def _mem_purge() -> None:
    with _MEM_LOCK:
        now = time.time()
        for k in [k for k, (_, exp) in _MEM.items() if exp is not None and now > exp]:
            _MEM.pop(k, None)


# ---------------------------------------------------------------------------
# upstash_redis client (optional)
# ---------------------------------------------------------------------------
_SDK = None
try:
    if _REST_URL and _REST_TOKEN:
        from upstash_redis import Redis  # type: ignore

        _SDK = Redis(url=_REST_URL, token=_REST_TOKEN)
        log.info("store: using upstash-redis SDK")
except Exception as exc:  # pragma: no cover - import-time fallback
    log.warning("store: upstash-redis SDK unavailable (%s), using REST", exc)
    _SDK = None


def _use_rest() -> bool:
    return _SDK is None and bool(_REST_URL and _REST_TOKEN)


def _rest_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_REST_TOKEN}"}


def _rest_cmd(*args: str) -> object:
    """POST a command array to the Upstash REST base URL.

    Upstash REST accepts ``POST /`` with a JSON command, e.g.
    ``["SET", "k", "v", "EX", "45", "NX"]`` and returns ``{"result": ...}``.
    """
    import httpx

    resp = httpx.post(_REST_URL, headers=_rest_headers(), json=list(args), timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", data)


# ---------------------------------------------------------------------------
# Sync API
# ---------------------------------------------------------------------------

def rget(key: str) -> str | None:
    """GET a raw string value (or None)."""
    try:
        if _SDK is not None:
            val = _SDK.get(key)
            return None if val is None else (val if isinstance(val, str) else str(val))
        if _use_rest():
            res = _rest_cmd("GET", key)
            return None if res is None else str(res)
    except Exception as exc:
        log.warning("store.rget(%s) failed: %s", key, exc)
        if _REST_URL and _REST_TOKEN:
            raise
    return _mem_get(key)


def rset(key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
    """SET a raw string value. Returns False only when NX blocked the write."""
    try:
        if _SDK is not None:
            # upstash-redis: set(key, value, ex=..., nx=...)
            kwargs: dict = {}
            if ex is not None:
                kwargs["ex"] = ex
            if nx:
                kwargs["nx"] = True
            res = _SDK.set(key, value, **kwargs)  # type: ignore[call-arg]
            # upstash-redis returns True on write and False when NX blocks it.
            # Older/alternate clients may return None; disambiguate by reading.
            if res is False:
                return False
            if res is None:
                return _SDK.get(key) == value
            return True
        if _use_rest():
            args: list[str] = ["SET", key, value]
            if ex is not None:
                args += ["EX", str(ex)]
            if nx:
                args += ["NX"]
            res = _rest_cmd(*args)
            return res == "OK"
    except Exception as exc:
        log.warning("store.rset(%s) failed: %s", key, exc)
        if _REDIS_CONFIGURED:
            raise
    return _mem_set(key, value, ex=ex, nx=nx)


def rincr(key: str, by: int = 1) -> int:
    try:
        if _SDK is not None:
            if by == 1:
                return int(_SDK.incr(key))  # type: ignore[attr-defined]
            return int(_SDK.incrby(key, by))  # type: ignore[attr-defined]
        if _use_rest():
            if by == 1:
                return int(_rest_cmd("INCR", key))  # type: ignore[arg-type]
            return int(_rest_cmd("INCRBY", key, str(by)))  # type: ignore[arg-type]
    except Exception as exc:
        log.warning("store.rincr(%s) failed: %s", key, exc)
        if _REDIS_CONFIGURED:
            raise
    with _MEM_LOCK:
        cur = _mem_get(key)
        try:
            nxt = int(cur or 0) + by
        except ValueError:
            nxt = by
        _MEM[key] = (str(nxt), None)
        return nxt


def rdelete(key: str) -> int:
    try:
        if _SDK is not None:
            return int(_SDK.delete(key))  # type: ignore[attr-defined]
        if _use_rest():
            return int(_rest_cmd("DEL", key))  # type: ignore[arg-type]
    except Exception as exc:
        log.warning("store.rdelete(%s) failed: %s", key, exc)
        if _REDIS_CONFIGURED:
            raise
    with _MEM_LOCK:
        return 1 if _MEM.pop(key, None) is not None else 0


def rkeys(pattern: str) -> list[str]:
    """List keys matching a glob pattern (supports trailing *)."""
    try:
        if _SDK is not None:
            keys = _SDK.keys(pattern)  # type: ignore[attr-defined]
            return list(keys or [])
        if _use_rest():
            import httpx

            # Dedicated keys endpoint: GET {base}/keys/<pattern>
            url = f"{_REST_URL}/keys/{urllib.parse.quote(pattern, safe='*:')}"
            resp = httpx.get(url, headers=_rest_headers(), timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            res = data.get("result", data)
            return list(res or [])
    except Exception as exc:
        log.warning("store.rkeys(%s) failed: %s", pattern, exc)
        if _REDIS_CONFIGURED:
            raise
    return _mem_keys(pattern)


# ---------------------------------------------------------------------------
# JSON convenience helpers (values stored as JSON strings)
# ---------------------------------------------------------------------------

def rget_json(key: str) -> object | None:
    raw = rget(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def rset_json(key: str, obj: object, ex: int | None = None, nx: bool = False) -> bool:
    return rset(key, json.dumps(obj, separators=(",", ":")), ex=ex, nx=nx)


# ---------------------------------------------------------------------------
# Async wrappers (run sync work in a thread so event loop stays free)
# ---------------------------------------------------------------------------

async def _to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


async def arget(key: str) -> str | None:
    return await _to_thread(rget, key)


async def arset(key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
    return await _to_thread(rset, key, value, ex=ex, nx=nx)


async def arincr(key: str, by: int = 1) -> int:
    return await _to_thread(rincr, key, by)


async def ardelete(key: str) -> int:
    return await _to_thread(rdelete, key)


async def arkeys(pattern: str) -> list[str]:
    return await _to_thread(rkeys, pattern)


async def arget_json(key: str) -> object | None:
    return await _to_thread(rget_json, key)


async def arset_json(key: str, obj: object, ex: int | None = None, nx: bool = False) -> bool:
    return await _to_thread(rset_json, key, obj, ex=ex, nx=nx)


def backend_mode() -> str:
    """Return which storage backend is active (for /health diagnostics)."""
    if _SDK is not None:
        return "upstash-sdk"
    if _use_rest():
        return "upstash-rest"
    return "memory"


def get_quota(provider: str) -> float:
    """Monthly vCPU-second usage for a sandbox provider."""
    import datetime

    ym = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    try:
        val = rget(f"quota:{provider}:{ym}")
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def incr_quota(provider: str, seconds: float) -> float:
    """Add CPU-seconds to a provider's monthly quota counter."""
    import datetime

    ym = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    key = f"quota:{provider}:{ym}"
    # In-memory mode: make the read-modify-write atomic for concurrent runs.
    if not _REDIS_CONFIGURED:
        with _MEM_LOCK:
            try:
                cur = float(_mem_get(key) or 0.0)
            except (TypeError, ValueError):
                cur = 0.0
            nxt = cur + float(seconds)
            _mem_set(key, str(nxt))
            return nxt
    # rincr is int-based; keep fractional via read-modify-write fallback.
    try:
        if _SDK is not None and hasattr(_SDK, "incrbyfloat"):
            return float(_SDK.incrbyfloat(key, seconds))  # type: ignore[attr-defined]
        if _use_rest():
            res = _rest_cmd("INCRBYFLOAT", key, str(seconds))
            return float(res)  # type: ignore[arg-type]
    except Exception as exc:
        log.warning("store.incr_quota(%s) fast-path failed: %s", provider, exc)
    cur = get_quota(provider)
    nxt = cur + float(seconds)
    # Best-effort write; never fall back to process memory when Redis is
    # configured, or different workers would see inconsistent quota counters.
    try:
        rset(key, str(nxt))
    except Exception as exc:
        log.warning("store.incr_quota(%s) write failed: %s", provider, exc)
    return nxt
