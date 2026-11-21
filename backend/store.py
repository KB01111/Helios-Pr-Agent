"""Shared Redis helper for the PR-Agent cloud backend.

Priority:
  1. Generic Redis via `REDIS_URL` (redis:// or rediss://), as injected by
     FastAPI Cloud's Redis integration.
  2. `upstash_redis` package (if installed AND Upstash REST env configured).
  3. Raw Upstash REST calls via httpx (sync + async variants).
  4. In-memory dict fallback (local dev without Redis env vars).

The rest of the backend (main.py, sandboxes/router.py) should import
from this module so they share one connection/singleton and one
fallback store:

    from store import rget, rset, rincr, rkeys, rdelete, rget_json, rset_json
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
import uuid

log = logging.getLogger("pr-agent.store")

_REDIS_URL = os.getenv("REDIS_URL", "").strip()
_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# In-memory fallback: {key: (value_str, expires_at|None)}
_MEM: dict[str, tuple[str | None, float | None]] = {}
_MEM_LOCK = threading.RLock()

# If Redis credentials are configured, never silently fall back to process
# memory for writes: in a multi-worker deployment that creates records only
# one worker can see and then makes reads fail. Fail closed instead.
_REDIS_CONFIGURED = bool(_REDIS_URL or (_REST_URL and _REST_TOKEN))


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
# Generic Redis client (preferred when REDIS_URL is set)
# ---------------------------------------------------------------------------
_REDIS = None

if _REDIS_URL:
    try:
        _parsed_url = urllib.parse.urlparse(_REDIS_URL)
        _scheme = _parsed_url.scheme.lower()
        _hostname = _parsed_url.hostname
    except ValueError:  # e.g. malformed IPv6 URL
        # Do not chain the parser exception: it may echo the URL (and its
        # password) into logs.
        raise RuntimeError(
            "store: REDIS_URL is set but could not be parsed; expected a "
            "redis:// or rediss:// URL (refusing to fall back to process memory)"
        ) from None
    if _scheme not in ("redis", "rediss") or not _hostname:
        # Fail closed: silently using process memory here would split state
        # between workers and make writes invisible across the deployment.
        raise RuntimeError(
            "store: REDIS_URL is set but is not a valid redis:// or rediss:// URL; "
            "refusing to fall back to process memory"
        )
    try:
        import redis as _redis_module  # type: ignore
    except ImportError:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "store: REDIS_URL is set but the 'redis' package is not installed; "
            "install redis>=5.0 (refusing to fall back to process memory)"
        ) from None
    try:
        # One module-level client/pool per process, shared by all requests.
        # redis-py clients are thread-safe. TLS is verified by default for
        # rediss://; never disable it here.
        _REDIS = _redis_module.Redis.from_url(
            _REDIS_URL,
            decode_responses=True,
            max_connections=8,  # small free-tier connection budget
            socket_timeout=5,
            socket_connect_timeout=5,
            socket_keepalive=True,
            health_check_interval=30,
        )
    except Exception:  # pragma: no cover - import-time config error
        # Do not chain the original exception: it can contain the URL and
        # therefore the Redis password.
        raise RuntimeError(
            "store: REDIS_URL is set but the Redis client could not be created; "
            "check the URL format (redis:// or rediss://) and refusing to "
            "fall back to process memory"
        ) from None
    log.info("store: using generic Redis via REDIS_URL")


# ---------------------------------------------------------------------------
# upstash_redis client (optional)
# ---------------------------------------------------------------------------
_SDK = None
try:
    if _REDIS is None and _REST_URL and _REST_TOKEN:
        from upstash_redis import Redis  # type: ignore

        _SDK = Redis(url=_REST_URL, token=_REST_TOKEN)
        log.info("store: using upstash-redis SDK")
except Exception as exc:  # pragma: no cover - import-time fallback
    log.warning("store: upstash-redis SDK unavailable (%s), using REST", exc)
    _SDK = None


def _use_rest() -> bool:
    return _REDIS is None and _SDK is None and bool(_REST_URL and _REST_TOKEN)


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
        if _REDIS is not None:
            val = _REDIS.get(key)
            return None if val is None else (val if isinstance(val, str) else str(val))
        if _SDK is not None:
            val = _SDK.get(key)
            return None if val is None else (val if isinstance(val, str) else str(val))
        if _use_rest():
            res = _rest_cmd("GET", key)
            return None if res is None else str(res)
    except Exception as exc:
        log.warning("store.rget(%s) failed: %s", key, exc)
        if _REDIS_CONFIGURED:
            raise
    return _mem_get(key)


def rset(key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
    """SET a raw string value. Returns False only when NX blocked the write."""
    try:
        if _REDIS is not None:
            res = _REDIS.set(key, value, ex=ex, nx=nx)
            # redis-py returns True on write and None when NX blocks it.
            return bool(res)
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


def rincr(key: str, by: int = 1, ex: int | None = None) -> int:
    ttl = int(ex) if ex is not None and int(ex) > 0 else None
    try:
        if _REDIS is not None:
            if ttl is not None:
                pipe = _REDIS.pipeline()
                pipe.incrby(key, by)
                pipe.expire(key, ttl)
                return int(pipe.execute()[0])
            return int(_REDIS.incrby(key, by))
        if _SDK is not None:
            val = int(_SDK.incr(key)) if by == 1 else int(_SDK.incrby(key, by))  # type: ignore[attr-defined]
            if ttl is not None:
                _SDK.expire(key, ttl)  # type: ignore[attr-defined]
            return val
        if _use_rest():
            val = (
                int(_rest_cmd("INCR", key))  # type: ignore[arg-type]
                if by == 1
                else int(_rest_cmd("INCRBY", key, str(by)))  # type: ignore[arg-type]
            )
            if ttl is not None:
                _rest_cmd("EXPIRE", key, str(ttl))
            return val
    except Exception as exc:
        log.warning("store.rincr(%s) failed: %s", key, exc)
        if _REDIS_CONFIGURED:
            raise
    with _MEM_LOCK:
        item = _MEM.get(key)
        cur = _mem_get(key)
        try:
            nxt = int(cur or 0) + by
        except ValueError:
            nxt = by
        exp = (time.time() + ttl) if ttl is not None else (item[1] if item and cur is not None else None)
        _MEM[key] = (str(nxt), exp)
        return nxt


def rdelete(key: str) -> int:
    try:
        if _REDIS is not None:
            return int(_REDIS.delete(key))
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
        if _REDIS is not None:
            # SCAN, never KEYS: safe on small free-tier instances and does
            # not block Redis while iterating.
            return list(_REDIS.scan_iter(match=pattern, count=500))
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


_MGET_CHUNK = 100


def rget_many(keys: list[str]) -> list[str | None]:
    """MGET raw values for `keys`, returned in the same order.

    Chunked to keep single commands small on free-tier Redis instances.
    """
    if not keys:
        return []
    try:
        if _REDIS is not None:
            out: list[str | None] = []
            for start in range(0, len(keys), _MGET_CHUNK):
                chunk = list(keys[start:start + _MGET_CHUNK])
                vals = _REDIS.mget(chunk)
                out.extend(
                    None if val is None else (val if isinstance(val, str) else str(val))
                    for val in vals
                )
            return out
        if _SDK is not None:
            getter = getattr(_SDK, "mget", None)
            if callable(getter):
                out = []
                for start in range(0, len(keys), _MGET_CHUNK):
                    chunk = list(keys[start:start + _MGET_CHUNK])
                    vals = getter(*chunk)
                    if not isinstance(vals, (list, tuple)):
                        vals = [vals]
                    out.extend(None if val is None else str(val) for val in vals)
                return out
            return [rget(k) for k in keys]
        if _use_rest():
            out = []
            for start in range(0, len(keys), _MGET_CHUNK):
                chunk = list(keys[start:start + _MGET_CHUNK])
                vals = _rest_cmd("MGET", *chunk)
                if isinstance(vals, list) and len(vals) == len(chunk):
                    out.extend(None if val is None else str(val) for val in vals)
                else:  # unexpected response shape: keep order with GETs
                    out.extend(rget(k) for k in chunk)
            return out
    except Exception as exc:
        log.warning("store.rget_many(%d keys) failed: %s", len(keys), exc)
        if _REDIS_CONFIGURED:
            raise
    return [_mem_get(k) for k in keys]


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


def rget_json_many(keys: list[str]) -> list[object | None]:
    """Decoded JSON values for `keys`, in the same order (None if absent)."""
    out: list[object | None] = []
    for raw in rget_many(keys):
        if raw is None:
            out.append(None)
            continue
        try:
            out.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            out.append(raw)
    return out


def rset_json(key: str, obj: object, ex: int | None = None, nx: bool = False) -> bool:
    return rset(key, json.dumps(obj, separators=(",", ":")), ex=ex, nx=nx)


# ---------------------------------------------------------------------------
# Capped lists (newest first): activity logs, finding history, digests.
# ---------------------------------------------------------------------------


def _mem_list(key: str) -> list[str]:
    raw = _mem_get(key)
    if raw is None:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(v) for v in val] if isinstance(val, list) else []


def rlpush_capped(key: str, value: str, cap: int, ex: int | None = None) -> int:
    """LPUSH ``value``, keep the newest ``cap`` items, refresh the TTL. Returns the new length."""
    cap = max(1, int(cap))
    try:
        if _REDIS is not None:
            pipe = _REDIS.pipeline()
            pipe.lpush(key, value)
            pipe.ltrim(key, 0, cap - 1)
            if ex:
                pipe.expire(key, ex)
            pipe.llen(key)
            return int(pipe.execute()[-1])
        if _SDK is not None:
            _SDK.lpush(key, value)  # type: ignore[attr-defined]
            _SDK.ltrim(key, 0, cap - 1)  # type: ignore[attr-defined]
            if ex:
                _SDK.expire(key, ex)  # type: ignore[attr-defined]
            return int(_SDK.llen(key))  # type: ignore[attr-defined]
        if _use_rest():
            _rest_cmd("LPUSH", key, value)
            _rest_cmd("LTRIM", key, "0", str(cap - 1))
            if ex:
                _rest_cmd("EXPIRE", key, str(ex))
            return int(_rest_cmd("LLEN", key))  # type: ignore[arg-type]
    except Exception as exc:
        log.warning("store.rlpush_capped(%s) failed: %s", key, exc)
        if _REDIS_CONFIGURED:
            raise
    with _MEM_LOCK:
        items = [value] + _mem_list(key)
        items = items[:cap]
        _mem_set(key, json.dumps(items), ex=ex)
        return len(items)


def rlrange(key: str, start: int = 0, stop: int = -1) -> list[str]:
    """LRANGE (inclusive ``stop``, ``-1`` = to the end), newest first."""
    try:
        if _REDIS is not None:
            return [str(v) for v in _REDIS.lrange(key, start, stop)]
        if _SDK is not None:
            return [str(v) for v in (_SDK.lrange(key, start, stop) or [])]  # type: ignore[attr-defined]
        if _use_rest():
            res = _rest_cmd("LRANGE", key, str(start), str(stop))
            return [str(v) for v in res] if isinstance(res, list) else []
    except Exception as exc:
        log.warning("store.rlrange(%s) failed: %s", key, exc)
        if _REDIS_CONFIGURED:
            raise
    items = _mem_list(key)
    end = None if stop == -1 else stop + 1
    return items[start:end]


def rlpush_json_capped(key: str, obj: object, cap: int, ex: int | None = None) -> int:
    return rlpush_capped(key, json.dumps(obj, separators=(",", ":")), cap, ex=ex)


def rlrange_json(key: str, start: int = 0, stop: int = -1) -> list[object]:
    out: list[object] = []
    for raw in rlrange(key, start, stop):
        try:
            out.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            out.append(raw)
    return out


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


async def arincr(key: str, by: int = 1, ex: int | None = None) -> int:
    return await _to_thread(rincr, key, by, ex)


async def ardelete(key: str) -> int:
    return await _to_thread(rdelete, key)


async def arkeys(pattern: str) -> list[str]:
    return await _to_thread(rkeys, pattern)


async def arget_many(keys: list[str]) -> list[str | None]:
    return await _to_thread(rget_many, keys)


async def arget_json_many(keys: list[str]) -> list[object | None]:
    return await _to_thread(rget_json_many, keys)


async def arget_json(key: str) -> object | None:
    return await _to_thread(rget_json, key)


async def arset_json(key: str, obj: object, ex: int | None = None, nx: bool = False) -> bool:
    return await _to_thread(rset_json, key, obj, ex=ex, nx=nx)


def _ping_roundtrip() -> bool:
    """SET/GET/DEL probe used when the backend has no cheap PING."""
    key = f"__store_ping__:{uuid.uuid4().hex}"
    try:
        if not rset(key, "1", ex=30):
            return False
        return rget(key) == "1"
    finally:
        try:
            rdelete(key)
        except Exception:
            pass


def ping() -> bool:
    """Liveness check for the active backend. Never raises, never leaks secrets."""
    try:
        if _REDIS is not None:
            return bool(_REDIS.ping())
        if _SDK is not None:
            pinger = getattr(_SDK, "ping", None)
            if callable(pinger):
                try:
                    return bool(pinger())
                except Exception:
                    pass  # PING unsupported: fall back to a roundtrip probe
            return _ping_roundtrip()
        if _use_rest():
            try:
                if str(_rest_cmd("PING")).upper() == "PONG":
                    return True
            except Exception:
                pass  # PING unsupported: fall back to a roundtrip probe
            return _ping_roundtrip()
        with _MEM_LOCK:
            _mem_set("__store_ping__", "1", ex=5)
            return _mem_get("__store_ping__") == "1"
    except Exception as exc:
        log.warning("store.ping failed (%s)", type(exc).__name__)
        return False


def redis_source() -> str:
    """Active Redis config source without exposing the URL/token."""
    if _REDIS is not None:
        return "REDIS_URL"
    if _SDK is not None or _use_rest():
        return "upstash"
    return ""


def redis_configured() -> bool:
    """True when a shared Redis backend is configured (not process memory)."""
    return _REDIS_CONFIGURED


def backend_mode() -> str:
    """Return which storage backend is active (for /health diagnostics)."""
    if _REDIS is not None:
        return "redis"
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
        if _REDIS is not None:
            return float(_REDIS.incrbyfloat(key, float(seconds)))
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


# ---------------------------------------------------------------------------
# PR evaluation idempotency
#
# Every path that wants to act on a pull request (cloud webhook, Cloudflare
# worker replay, desktop daemon polling, the Box cron loop) registers the
# evaluation here first. The record is keyed on (repo, pr_number, head_sha) and
# created with NX, so exactly one path wins per head commit — waking the
# desktop never produces a second review or a second sandbox. A new push has a
# new head_sha and therefore a fresh evaluation.
# ---------------------------------------------------------------------------

EVAL_TTL_S = 14 * 24 * 3600
EVAL_LOCK_LEASE_S = 60


def eval_id(repo: str, pr_number: int, head_sha: str) -> str:
    """Stable short id for one (repo, pr_number, head_sha) evaluation."""
    # GitHub repo names and SHAs are case-insensitive; every caller must hash the same key.
    raw = f"{str(repo).lower()}|{int(pr_number)}|{str(head_sha).lower()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _eval_key(eid: str) -> str:
    return f"eval:{eid}"


# Evaluations that something still has to act on. A tiny marker key per open evaluation
# lets the sweeper, the desktop's pending poll and the inbox read only those records,
# instead of scanning (and MGET-ing) 14 days of history.
EVAL_OPEN_STATES = ("pending", "deferred", "dispatching", "escalated", "failed", "external_claimed")


def _sync_open_marker(rec: dict, ex: int | None = None) -> None:
    key = f"evalopen:{rec['id']}"
    if rec.get("state") in EVAL_OPEN_STATES:
        rset(key, repr(time.time()), ex=ex or EVAL_TTL_S)
    else:
        rdelete(key)


# An open marker whose record is missing is either a registration in flight (marker is
# written first) or an orphan from a crash. Only orphans older than this are swept.
ORPHAN_MARKER_GRACE_S = 60


def _drop_orphan_marker(eid: str) -> None:
    """Delete an orphan marker with compare-and-delete on the value we aged: if a
    registration rewrote the marker after we read it, the value differs and it survives."""
    key = f"evalopen:{eid}"
    raw = rget(key)
    if raw is None:
        return
    try:
        age = time.time() - float(raw)
    except ValueError:
        age = float("inf")
    if age > ORPHAN_MARKER_GRACE_S:
        rdelete_if_equal(key, raw)


def _recent_key(rec: dict) -> str:
    """Time-ordered key so "newest N" reads N records instead of scanning all of them."""
    import datetime

    try:
        ts = datetime.datetime.fromisoformat(str(rec["created_at"])).timestamp()
    except (KeyError, ValueError):
        ts = time.time()
    return f"evalrecent:{int(ts * 1_000_000):018d}:{rec['id']}"


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def eval_register(
    repo: str, pr_number: int, head_sha: str, record: dict, ex: int | None = None
) -> tuple[bool, dict]:
    """Create the evaluation record if absent. Returns ``(created, record)``.

    When ``created`` is False the returned record is the existing one, so the
    caller can report what already happened instead of acting again.
    """
    eid = eval_id(repo, pr_number, head_sha)
    now = _now_iso()
    rec = dict(record)
    rec.update({
        "id": eid, "repo": repo, "pr_number": int(pr_number), "head_sha": head_sha,
        "created_at": now, "updated_at": now,
    })
    for _ in range(2):  # second pass covers a record that expired between calls
        # Marker first: if we crash between the two writes the leftover is a marker with no
        # record, which eval_list_open() sweeps up. The reverse (record without marker)
        # would hide an unresolved evaluation from the sweeper.
        _sync_open_marker(rec, ex)
        if rset_json(_eval_key(eid), rec, ex=ex or EVAL_TTL_S, nx=True):
            rset(_recent_key(rec), "1", ex=ex or EVAL_TTL_S)
            return True, rec
        existing = rget_json(_eval_key(eid))
        if isinstance(existing, dict):
            return False, existing
    return False, rec


class LockLostError(RuntimeError):
    """Raised when a lease-protected commit detects the lock lease was lost."""


_COMPARE_AND_DELETE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
)

_COMMIT_IF_LOCK_HELD = (
    "if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end "
    "local idx = 2 "
    "for i = 2, #KEYS do "
    "  local op = ARGV[idx] "
    "  if op == 'D' then "
    "    redis.call('del', KEYS[i]) "
    "    idx = idx + 1 "
    "  else "
    "    local val = ARGV[idx + 1] "
    "    local ttl = tonumber(ARGV[idx + 2]) "
    "    if ttl and ttl > 0 then "
    "      redis.call('set', KEYS[i], val, 'EX', ttl) "
    "    else "
    "      redis.call('set', KEYS[i], val) "
    "    end "
    "    idx = idx + 3 "
    "  end "
    "end "
    "return 1"
)


def rcommit_if_lock_held(
    lock_key: str,
    token: str,
    ops: list[tuple[str, str | None, int | None]],
) -> bool:
    """Atomically apply ``ops`` only if ``lock_key`` still holds ``token``.

    Each element in ``ops`` is ``(key, value_or_none, ex_seconds_or_none)``.
    When ``value_or_none`` is ``None``, the key is deleted; otherwise it is set.
    Returns ``True`` if the lease was still held and all writes committed, or
    ``False`` if the lease expired or was acquired by another owner.
    """
    keys = [lock_key]
    args: list[str] = [token]
    for key, val, ex in ops:
        keys.append(key)
        if val is None:
            args.append("D")
        else:
            args.append("S")
            args.append(val)
            args.append(str(int(ex)) if ex and int(ex) > 0 else "0")

    if _REDIS is not None:
        return bool(_REDIS.eval(_COMMIT_IF_LOCK_HELD, len(keys), *keys, *args))
    if _SDK is not None and hasattr(_SDK, "eval"):
        return bool(_SDK.eval(_COMMIT_IF_LOCK_HELD, keys=keys, args=args))  # type: ignore[attr-defined]
    if _use_rest():
        return bool(int(_rest_cmd("EVAL", _COMMIT_IF_LOCK_HELD, str(len(keys)), *keys, *args)))  # type: ignore[arg-type]
    with _MEM_LOCK:
        if _mem_get(lock_key) != token:
            return False
        for key, val, ex in ops:
            if val is None:
                _MEM.pop(key, None)
            else:
                exp = (time.time() + int(ex)) if ex and int(ex) > 0 else None
                _MEM[key] = (val, exp)
        return True


def rdelete_if_equal(key: str, token: str) -> bool:
    """Atomically delete ``key`` only if it still holds ``token``.

    A plain GET-then-DELETE could remove a lease that expired and was re-acquired by
    someone else between the two calls.
    """
    if _REDIS is not None:
        return bool(_REDIS.eval(_COMPARE_AND_DELETE, 1, key, token))
    if _SDK is not None and hasattr(_SDK, "eval"):
        return bool(_SDK.eval(_COMPARE_AND_DELETE, keys=[key], args=[token]))  # type: ignore[attr-defined]
    if _use_rest():
        return bool(int(_rest_cmd("EVAL", _COMPARE_AND_DELETE, "1", key, token)))  # type: ignore[arg-type]
    with _MEM_LOCK:
        if _mem_get(key) == token:
            _MEM.pop(key, None)
            return True
        return False


def eval_get(eid: str) -> dict | None:
    rec = rget_json(_eval_key(eid))
    return rec if isinstance(rec, dict) else None


def eval_update(eid: str, patch: dict, ex: int | None = None) -> dict | None:
    """Merge ``patch`` into a record. Callers hold ``eval_lock`` (or own the
    record via its registration) so this read-modify-write cannot interleave."""
    rec = eval_get(eid)
    if rec is None:
        return None
    rec.update(patch)
    rec["updated_at"] = _now_iso()
    rset_json(_eval_key(eid), rec, ex=ex or EVAL_TTL_S)
    _sync_open_marker(rec, ex)
    return rec


def eval_delete(eid: str) -> None:
    rec = eval_get(eid)
    rdelete(_eval_key(eid))
    rdelete(f"evalopen:{eid}")
    if rec is not None:
        rdelete(_recent_key(rec))  # keep the time index free of dead entries


@contextlib.contextmanager
def key_lock(lock_key: str, lease_s: int = 15, wait_s: float = 5.0):
    """Cross-worker exclusive lease on ``lock_key`` (Redis NX + compare-and-delete).

    Waits up to ``wait_s`` seconds to acquire the lease. Yields the lock token,
    or raises ``TimeoutError`` if the lock could not be acquired in time.
    """
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.0, float(wait_s))
    while not rset(lock_key, token, ex=max(1, int(lease_s)), nx=True):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"could not acquire lock {lock_key}")
        time.sleep(0.02)
    try:
        yield token
    finally:
        try:
            rdelete_if_equal(lock_key, token)
        except Exception as exc:  # the lease simply expires
            log.warning("store.key_lock(%s) release failed: %s", lock_key, exc)


@contextlib.asynccontextmanager
async def akey_lock(lock_key: str, lease_s: int = 15, wait_s: float = 5.0):
    """Async cross-worker exclusive lease on ``lock_key`` without blocking the event loop."""
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.0, float(wait_s))
    while not await arset(lock_key, token, ex=max(1, int(lease_s)), nx=True):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"could not acquire lock {lock_key}")
        await asyncio.sleep(0.02)
    try:
        yield token
    finally:
        try:
            await asyncio.to_thread(rdelete_if_equal, lock_key, token)
        except Exception as exc:  # the lease simply expires
            log.warning("store.akey_lock(%s) release failed: %s", lock_key, exc)


@contextlib.contextmanager
def eval_lock(eid: str, lease_s: int = EVAL_LOCK_LEASE_S):
    """Short exclusive lease on one evaluation; yields a token or ``None``.

    The desktop's submit, the cloud sweeper and a human clicking Approve all
    mutate the same record; whoever holds the lease re-reads state and proceeds,
    everyone else backs off.
    """
    key = f"evallock:{eid}"
    token = uuid.uuid4().hex
    if not rset(key, token, ex=lease_s, nx=True):
        yield None
        return
    try:
        yield token
    finally:
        try:
            rdelete_if_equal(key, token)
        except Exception as exc:  # the lease simply expires
            log.warning("store.eval_lock release failed: %s", exc)


def eval_reindex() -> int:
    """Rebuild missing open markers from the records themselves (run at startup; covers
    records written before the index existed or a crash between the two writes)."""
    restored = 0
    have = {k.split("evalopen:", 1)[-1] for k in rkeys("evalopen:*")}
    have_recent = {k.rsplit(":", 1)[-1] for k in rkeys("evalrecent:*")}
    for rec in rget_json_many(rkeys("eval:*")):
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        if rec["id"] not in have_recent:
            rset(_recent_key(rec), "1", ex=EVAL_TTL_S)
        if rec.get("state") in EVAL_OPEN_STATES and rec["id"] not in have:
            _sync_open_marker(rec)
            restored += 1
    return restored


def eval_list_open(states: tuple[str, ...] | None = None) -> list[dict]:
    """Records still awaiting action, oldest first. Reads only the open set."""
    ids = [k.split("evalopen:", 1)[-1] for k in rkeys("evalopen:*")]
    loaded = rget_json_many([_eval_key(i) for i in ids])
    records = []
    for eid, rec in zip(ids, loaded):
        if isinstance(rec, dict) and rec.get("state") in EVAL_OPEN_STATES:
            if rec.get("state") in (states or EVAL_OPEN_STATES):
                records.append(rec)
        elif isinstance(rec, dict):
            rdelete(f"evalopen:{eid}")  # settled: terminal states never reopen
        else:
            _drop_orphan_marker(eid)  # orphan from a crash (a fresh one may be mid-registration)
    records.sort(key=lambda r: str(r.get("created_at", "")))
    return records


def eval_list(limit: int = 100) -> list[dict]:
    # Newest first from the time-ordered key index, then read only those `limit` records.
    keys = sorted(rkeys("evalrecent:*"), reverse=True)[: max(1, limit)]
    ids = list(dict.fromkeys(k.rsplit(":", 1)[-1] for k in keys))  # no duplicate ids
    records = [r for r in rget_json_many([_eval_key(i) for i in ids]) if isinstance(r, dict)]
    records.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return records
