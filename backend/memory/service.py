"""Repo-specific memory service: CRUD, scoped retrieval, dismissal learning,
repo bootstrap, merged-PR extraction, and Actions-lite Markdown export.

Storage layout (Redis via ``store.py``, JSON values):
- ``mem:{repo}:{id}``            one ``MemoryEntry`` (capped at 500 per repo)
- ``repo:{repo}:profile``        detected languages, frameworks, commands
- ``memdismiss:{repo}:{hash}``   dismissal counter per finding stem
- ``finding:{repo}:{pr}:{fp}``   per-PR finding state (updated on dismissal)
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import posixpath
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

try:  # cwd=backend
    import datamodel as dm
    import github_app
    import store
    from sandboxes.providers_review import _glob_matches
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend import github_app  # type: ignore[no-redef]
    from backend import store  # type: ignore[no-redef]
    from backend.sandboxes.providers_review import _glob_matches  # type: ignore[no-redef]

log = logging.getLogger("pr-agent.memory")

GITHUB_API = "https://api.github.com"

BOOTSTRAP_FILES: tuple[str, ...] = (
    "CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".editorconfig",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
)

_MANIFEST_FILES = frozenset({"package.json", "pyproject.toml", "Cargo.toml"})
_MEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_GLOB_META_RE = re.compile(r"([\[\*\?])")
_EDITORCONFIG_INT_RANGE_RE = re.compile(r"^([+-]?\d+)\.\.([+-]?\d+)$")
_MAX_EDITORCONFIG_GLOBS = 20
_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "when", "where",
    "have", "has", "had", "not", "are", "was", "were", "will", "can", "should",
    "would", "could", "all", "any", "but", "you", "your", "our", "use", "using",
    "def", "return", "import", "class", "const", "let", "var", "true", "false",
    "none", "null", "self", "else", "elif", "try", "except", "pass", "async",
    "await", "pub", "fn", "struct", "impl", "mod", "function", "export", "default",
})

_UPDATABLE_FIELDS = frozenset({"text", "scope_globs", "blocking", "status", "confidence", "kind"})

# Guidance signal patterns for bootstrap docs and merged-PR review comments.
_GUIDANCE_RE = re.compile(
    r"(?i)\b(?:"
    r"remember|always|never|must|avoid|prefer|convention|invariant|"
    r"do\s+not|don'?t|make\s+sure|be\s+sure|ensure\s+that|"
    r"please\s+(?:always|never|avoid|use|prefer|ensure|keep|add|check)|"
    r"in\s+this\s+repo(?:sitory)?|we\s+(?:always|never|prefer|use|avoid|require)|"
    r"going\s+forward|for\s+future|note\s+for\s+future|rule\s*:|"
    r"requires?\s+|forbidden|prohibited|deprecated"
    r")\b"
)
_REMEMBER_PREFIX_RE = re.compile(
    r"(?i)^(?:@\S+\s*[:, -]*\s*)?(?:remember|learn)\s*[:,-]?\s+"
)
_NOISE_COMMENT_RE = re.compile(
    r"(?i)^(?:lgtm|looks\s+good|thanks?(?:\s+you)?|done|fixed|nice|ship\s+it|\+1|approved|merged)[\s!.?]*$"
)
_MARKDOWN_LINK_ONLY_RE = re.compile(r"^\[[^\]]+\]\([^)]+\)$")

_MAX_BOOTSTRAP_DOC_FACTS = 10
_MAX_MERGED_PR_LEARNINGS = 5
_DISMISS_COUNTER_TTL_S = 180 * 24 * 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _norm_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/")


def _norm_globs(globs: Any) -> list[str]:
    if not isinstance(globs, list):
        return ["**"]
    cleaned: list[str] = []
    for g in globs:
        s = _norm_path(str(g or ""))
        if s and s not in cleaned:
            cleaned.append(s)
    return cleaned[:20] if cleaned else ["**"]


def _norm_dedupe_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _escape_glob_literal(path: str) -> str:
    """Escape glob metacharacters so a repo file path matches only itself."""
    return _GLOB_META_RE.sub(r"[\1]", path)


def valid_memory_id(mem_id: Any) -> bool:
    """Return True when ``mem_id`` is a valid memory entry identifier."""
    return isinstance(mem_id, str) and bool(_MEM_ID_RE.fullmatch(mem_id))


_valid_mem_id = valid_memory_id


def _generate_mem_id(repo: str, *, existing_ids: set[str] | None = None) -> str:
    for _ in range(8):
        candidate = uuid.uuid4().hex[:10]
        if existing_ids is not None:
            if candidate not in existing_ids:
                return candidate
        elif store.rget(dm.memory_key(repo, candidate)) is None:
            return candidate
    return uuid.uuid4().hex[:16]


def _load_all_raw(repo: str) -> list[dict[str, Any]]:
    prefix = dm.memory_prefix(repo)
    keys = store.rkeys(f"{prefix}*")
    if not keys:
        return []
    entries: list[dict[str, Any]] = []
    for item in store.rget_json_many(keys):
        if isinstance(item, dict) and item.get("id") and item.get("text"):
            entries.append(item)
    return entries


def _eviction_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Smaller tuple = evicted sooner.

    Non-blocking entries are evicted before blocking ones; archived before
    active/pending; unused entries before used ones; oldest / least recently
    used first.
    """
    is_blocking = 1 if (item.get("blocking") and item.get("status") != "archived") else 0
    status_rank = 0 if item.get("status") == "archived" else 1
    hits = int(item.get("hits") or 0)
    last_used = str(item.get("last_used") or "")
    used = 1 if (last_used or hits > 0) else 0
    created = str(item.get("created_at") or "")
    return (is_blocking, status_rank, used, last_used, hits, created, str(item.get("id") or ""))


def _prune_entries_to_cap(
    repo: str,
    entries: list[dict[str, Any]],
    *,
    protect_ids: set[str] | None = None,
    collect_errors: list[str] | None = None,
) -> int:
    """Enforce ``MEMORY_MAX_ENTRIES_PER_REPO`` over ``entries`` in a single pass."""
    cap = max(1, int(dm.MEMORY_MAX_ENTRIES_PER_REPO))
    excess = len(entries) - cap
    if excess <= 0:
        return 0
    protected = protect_ids or set()
    unprotected = [e for e in entries if str(e.get("id") or "") not in protected]
    unprotected.sort(key=_eviction_key)
    victims = unprotected[:excess]
    if len(victims) < excess:
        protected_entries = [e for e in entries if str(e.get("id") or "") in protected]
        protected_entries.sort(key=_eviction_key)
        victims.extend(protected_entries[: excess - len(victims)])
    removed = 0
    for victim in victims:
        vid = str(victim.get("id") or "")
        if not vid:
            continue
        try:
            if store.rdelete(dm.memory_key(repo, vid)) > 0:
                removed += 1
        except Exception as exc:
            if collect_errors is None:
                raise
            log.warning("memory.prune: delete %s/%s failed (%s)", repo, vid, type(exc).__name__)
            collect_errors.append(f"prune {vid}: {type(exc).__name__}")
    return removed


def _prune_to_cap(repo: str, *, protect_id: str | None = None) -> int:
    """Enforce ``MEMORY_MAX_ENTRIES_PER_REPO`` by LRU-pruning oldest unused non-blocking entries."""
    entries = _load_all_raw(repo)
    protect_ids = {protect_id} if protect_id else None
    return _prune_entries_to_cap(repo, entries, protect_ids=protect_ids)


def _prepare_memory_entry(
    repo: str,
    entry: dm.MemoryEntry | dict[str, Any],
    *,
    existing_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Normalize and validate a memory entry without writing it to storage."""
    dm.memory_prefix(repo)
    if isinstance(entry, dm.MemoryEntry):
        data = entry.model_dump()
    elif isinstance(entry, dict):
        data = dict(entry)
    else:
        raise TypeError("entry must be a MemoryEntry or dict")

    if isinstance(data.get("text"), str):
        data["text"] = data["text"].strip()
    if "scope_globs" in data:
        data["scope_globs"] = _norm_globs(data["scope_globs"])
    else:
        data["scope_globs"] = ["**"]

    supplied_id = str(data.get("id") or "").strip()
    if supplied_id:
        if not valid_memory_id(supplied_id):
            raise ValueError("invalid memory id")
        mem_id = supplied_id
    else:
        mem_id = _generate_mem_id(repo, existing_ids=existing_ids)
    data["id"] = mem_id

    if not data.get("created_at"):
        data["created_at"] = _now_iso()

    validated = dm.MemoryEntry.model_validate(data).model_dump()
    validated["scope_globs"] = _norm_globs(validated["scope_globs"])
    return validated


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_memories(
    repo: str,
    *,
    status: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """List repo memories, optionally filtered by ``status`` and/or ``kind``."""
    entries = _load_all_raw(repo)
    if status is not None:
        entries = [e for e in entries if e.get("status") == status]
    if kind is not None:
        entries = [e for e in entries if e.get("kind") == kind]
    entries.sort(key=lambda e: (str(e.get("created_at") or ""), str(e.get("id") or "")), reverse=True)
    return entries


def get_memory(repo: str, mem_id: str) -> dict[str, Any] | None:
    """Fetch a single memory entry by id, or ``None`` if absent."""
    # Always validate repo first.
    dm.memory_prefix(repo)
    if not valid_memory_id(mem_id):
        return None
    raw = store.rget_json(dm.memory_key(repo, mem_id))
    return raw if isinstance(raw, dict) else None


def add_memory(repo: str, entry: dm.MemoryEntry | dict[str, Any]) -> dict[str, Any]:
    """Create and persist a memory entry, enforcing the per-repo entry cap."""
    validated = _prepare_memory_entry(repo, entry)
    mem_id = validated["id"]
    store.rset_json(dm.memory_key(repo, mem_id), validated)
    _prune_to_cap(repo, protect_id=mem_id)
    return validated


def update_memory(repo: str, mem_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """Safely update mutable fields (text, scope_globs, blocking, status, confidence, kind)."""
    dm.memory_prefix(repo)
    if not valid_memory_id(mem_id):
        return None
    if not isinstance(patch, dict):
        raise TypeError("patch must be a dict")
    key = dm.memory_key(repo, mem_id)
    with store.key_lock(f"memlock:{repo.lower()}:{mem_id}"):
        existing = store.rget_json(key)
        if not isinstance(existing, dict):
            return None
        merged = dict(existing)
        for field_key, value in patch.items():
            if field_key not in _UPDATABLE_FIELDS:
                continue
            if field_key == "text" and isinstance(value, str):
                merged[field_key] = value.strip()
            elif field_key == "scope_globs":
                merged[field_key] = _norm_globs(value)
            else:
                merged[field_key] = value
        validated = dm.MemoryEntry.model_validate(merged).model_dump()
        validated["scope_globs"] = _norm_globs(validated["scope_globs"])
        store.rset_json(key, validated)
        return validated


def delete_memory(repo: str, mem_id: str) -> bool:
    """Delete one memory entry. Returns ``True`` if a record was removed."""
    dm.memory_prefix(repo)
    if not valid_memory_id(mem_id):
        return False
    with store.key_lock(f"memlock:{repo.lower()}:{mem_id}"):
        return store.rdelete(dm.memory_key(repo, mem_id)) > 0


def approve_memory(repo: str, mem_id: str) -> dict[str, Any] | None:
    """Mark a pending/archived memory entry as ``active``."""
    return update_memory(repo, mem_id, {"status": "active"})


def export_memories(repo: str) -> list[dict[str, Any]]:
    """Export all memory entries for ``repo`` as plain JSON-serializable dicts."""
    return list_memories(repo)


def import_memories(repo: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Import memory entries for ``repo``, skipping duplicates/invalid items and
    reporting storage/pruning failures separately."""
    dm.memory_prefix(repo)
    with store.key_lock(f"memrepolock:{repo.lower()}"):
        existing = _load_all_raw(repo)
        all_entries = list(existing)
        seen_ids = {str(e.get("id") or "") for e in existing}
        seen_sigs = {
            (
                str(e.get("kind") or "learning"),
                _norm_dedupe_text(str(e.get("text") or "")),
                tuple(sorted(_norm_globs(e.get("scope_globs")))),
            )
            for e in existing
        }
        imported = 0
        skipped = 0
        failed = 0
        errors: list[str] = []
        imported_ids: set[str] = set()

        for idx, raw in enumerate(items or []):
            if not isinstance(raw, dict):
                skipped += 1
                continue
            raw_id = str(raw.get("id") or "").strip()
            if raw_id and raw_id in seen_ids:
                skipped += 1
                continue
            text = str(raw.get("text") or "").strip()
            kind = str(raw.get("kind") or "learning")
            globs = _norm_globs(raw.get("scope_globs"))
            sig = (kind, _norm_dedupe_text(text), tuple(sorted(globs)))
            if not text or sig in seen_sigs:
                skipped += 1
                continue
            try:
                prepared = _prepare_memory_entry(
                    repo,
                    {**raw, "text": text, "scope_globs": globs},
                    existing_ids=seen_ids,
                )
            except (ValueError, TypeError, ValidationError):
                skipped += 1
                continue
            try:
                store.rset_json(dm.memory_key(repo, prepared["id"]), prepared)
            except Exception as exc:
                log.warning("memory.import: write %s item %d failed (%s)", repo, idx, type(exc).__name__)
                failed += 1
                errors.append(f"item {idx}: {type(exc).__name__}")
                continue
            seen_ids.add(prepared["id"])
            seen_sigs.add(sig)
            all_entries.append(prepared)
            imported_ids.add(prepared["id"])
            imported += 1

        if imported > 0:
            prune_errors: list[str] = []
            _prune_entries_to_cap(
                repo,
                all_entries,
                protect_ids=imported_ids,
                collect_errors=prune_errors,
            )
            if prune_errors:
                failed += len(prune_errors)
                errors.extend(prune_errors)

    return {"imported": imported, "skipped": skipped, "failed": failed, "errors": errors}


def get_repo_profile(repo: str) -> dict[str, Any] | None:
    """Fetch the cached repository profile, or ``None`` if not bootstrapped."""
    raw = store.rget_json(dm.repo_profile_key(repo))
    return raw if isinstance(raw, dict) else None


def render_memory_markdown(repo: str) -> str:
    """Render active learnings, repo facts, dismissed patterns, and profile into
    ``.pr-agent/memory.md`` format for zero-host GitHub Actions-lite mode."""
    dm.memory_prefix(repo)
    active = list_memories(repo, status="active")
    profile = get_repo_profile(repo)

    lines: list[str] = [
        f"# PR Agent Repo Memory — `{repo}`",
        "",
        "<!-- Generated by PR Agent Control for Actions-lite mode (.pr-agent/memory.md). -->",
        "",
    ]

    if profile:
        prof_lines: list[str] = []
        langs = profile.get("languages") or []
        fws = profile.get("frameworks") or []
        tests = profile.get("test_commands") or []
        builds = profile.get("build_commands") or []
        lints = profile.get("lint_commands") or []
        if langs:
            prof_lines.append(f"- **Languages:** {', '.join(str(x) for x in langs)}")
        if fws:
            prof_lines.append(f"- **Frameworks:** {', '.join(str(x) for x in fws)}")
        if tests:
            prof_lines.append("- **Test commands:** " + ", ".join(f"`{c}`" for c in tests))
        if builds:
            prof_lines.append("- **Build commands:** " + ", ".join(f"`{c}`" for c in builds))
        if lints:
            prof_lines.append("- **Lint commands:** " + ", ".join(f"`{c}`" for c in lints))
        if prof_lines:
            lines.append("## Repository Profile")
            lines.extend(prof_lines)
            lines.append("")

    blocking = [m for m in active if m.get("blocking") and m.get("kind") != "dismissed_pattern"]
    learnings = [
        m for m in active
        if not m.get("blocking") and m.get("kind") in ("learning", "incident")
    ]
    facts = [m for m in active if not m.get("blocking") and m.get("kind") == "repo_fact"]
    dismissed = [m for m in active if m.get("kind") == "dismissed_pattern"]

    def _fmt(entry: dict[str, Any]) -> str:
        scope = ", ".join(f"`{g}`" for g in _norm_globs(entry.get("scope_globs")))
        tag = f"[{entry['id']}]"
        return f"- {tag} ({scope}) {entry['text']}"

    if blocking:
        lines.append("## Blocking Rules (Must Enforce)")
        lines.extend(_fmt(m) for m in blocking)
        lines.append("")

    if learnings:
        lines.append("## Repository Learnings & Conventions")
        lines.extend(_fmt(m) for m in learnings)
        lines.append("")

    if facts:
        lines.append("## Repository Facts")
        lines.extend(_fmt(m) for m in facts)
        lines.append("")

    if dismissed:
        lines.append("## Known False Positives (Do Not Flag)")
        lines.extend(_fmt(m) for m in dismissed)
        lines.append("")

    if not (blocking or learnings or facts or dismissed or profile):
        lines.append("_No active repo memories recorded yet._")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Scoped retrieval
# ---------------------------------------------------------------------------


def _extract_tokens(text: str) -> set[str]:
    if not text:
        return set()
    # Split camelCase too so `getUserToken` yields `user`, `token`.
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    raw = _TOKEN_RE.findall(spaced.lower())
    out: set[str] = set()
    for tok in raw:
        parts = [tok] + [p for p in tok.split("_") if len(p) >= 3]
        for p in parts:
            if len(p) >= 3 and p not in _STOPWORDS:
                out.add(p)
    return out


def _entry_matches_paths(entry: dict[str, Any], norm_paths: list[str]) -> tuple[bool, bool]:
    """Return ``(matched, specific_match)`` where ``specific_match`` is True when
    a non-wildcard glob (not ``**`` / ``*``) matched."""
    globs = _norm_globs(entry.get("scope_globs"))
    matched = False
    specific = False
    for g in globs:
        for p in norm_paths:
            if _glob_matches(p, g):
                matched = True
                if g not in ("**", "*"):
                    specific = True
                    return True, True
    return matched, specific


def _entry_char_cost(entry: dict[str, Any]) -> int:
    globs = ",".join(_norm_globs(entry.get("scope_globs")))
    return len(str(entry.get("text") or "")) + len(str(entry.get("id") or "")) + len(globs) + 24


def retrieve_for_diff(
    repo: str,
    changed_paths: list[str],
    diff_text: str = "",
    *,
    max_chars: int = 6000,
) -> dict[str, Any]:
    """Select active memories matching ``changed_paths``, ranked by keyword
    overlap with ``diff_text``, confidence, blocking priority, and recency.

    Enforces ``max_chars`` budget and increments ``hits`` + updates ``last_used``
    on all returned entries.
    """
    dm.memory_prefix(repo)
    norm_paths = [_norm_path(p) for p in (changed_paths or []) if _norm_path(p)]
    if not norm_paths or max_chars <= 0:
        return {"rules": [], "dismissed": [], "used_ids": []}

    active = list_memories(repo, status="active")
    if not active:
        return {"rules": [], "dismissed": [], "used_ids": []}

    diff_tokens = _extract_tokens(diff_text) | _extract_tokens(" ".join(norm_paths))
    now = datetime.now(timezone.utc)

    scored_rules: list[tuple[tuple[float, float, str, str], dict[str, Any]]] = []
    scored_dismissed: list[tuple[tuple[float, float, str, str], dict[str, Any]]] = []

    for entry in active:
        matched, specific = _entry_matches_paths(entry, norm_paths)
        if not matched:
            continue
        mem_tokens = _extract_tokens(str(entry.get("text") or ""))
        if mem_tokens and diff_tokens:
            overlap = len(mem_tokens & diff_tokens) / len(mem_tokens)
        else:
            overlap = 0.0
        confidence = float(entry.get("confidence", 0.7))
        blocking_bonus = 3.0 if entry.get("blocking") else 0.0
        specificity_bonus = 0.5 if specific else 0.0
        ref_dt = _parse_iso(entry.get("last_used")) or _parse_iso(entry.get("created_at"))
        if ref_dt is not None:
            age_days = max(0.0, (now - ref_dt).total_seconds() / 86400.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
        else:
            recency = 0.25

        score = blocking_bonus + (overlap * 4.0) + (confidence * 2.0) + recency + specificity_bonus
        sort_key = (score, confidence, str(entry.get("created_at") or ""), str(entry.get("id") or ""))
        if entry.get("kind") == "dismissed_pattern":
            scored_dismissed.append((sort_key, entry))
        else:
            scored_rules.append((sort_key, entry))

    scored_rules.sort(key=lambda item: item[0], reverse=True)
    scored_dismissed.sort(key=lambda item: item[0], reverse=True)

    selected_rules: list[dict[str, Any]] = []
    selected_dismissed: list[dict[str, Any]] = []
    skipped_dismissed: list[dict[str, Any]] = []
    used_chars = 0

    # Reserve up to 25% of budget for dismissed patterns when rules also exist so
    # false-positive suppressions are never starved by a large rule list.
    if scored_dismissed:
        dismissed_cap = max_chars // 4 if scored_rules else max_chars
        dismissed_used = 0
        for _, entry in scored_dismissed:
            cost = _entry_char_cost(entry)
            if dismissed_used + cost <= dismissed_cap and used_chars + cost <= max_chars:
                selected_dismissed.append(entry)
                dismissed_used += cost
                used_chars += cost
            else:
                skipped_dismissed.append(entry)

    for _, entry in scored_rules:
        cost = _entry_char_cost(entry)
        if used_chars + cost <= max_chars:
            selected_rules.append(entry)
            used_chars += cost

    # Fill any leftover budget with remaining dismissed patterns.
    for entry in skipped_dismissed:
        cost = _entry_char_cost(entry)
        if used_chars + cost <= max_chars:
            selected_dismissed.append(entry)
            used_chars += cost

    now_iso = _now_iso()
    used_ids: list[str] = []
    persisted_rules = _record_retrieval_hits(repo, selected_rules, now_iso, used_ids)
    persisted_dismissed = _record_retrieval_hits(repo, selected_dismissed, now_iso, used_ids)

    return {
        "rules": persisted_rules,
        "dismissed": persisted_dismissed,
        "used_ids": used_ids,
    }


def _record_retrieval_hits(
    repo: str,
    entries: list[dict[str, Any]],
    now_iso: str,
    used_ids: list[str],
) -> list[dict[str, Any]]:
    """Persist ``hits`` + ``last_used`` on the latest active stored records only."""
    kept: list[dict[str, Any]] = []
    for entry in entries:
        mem_id = str(entry.get("id") or "")
        if not valid_memory_id(mem_id):
            continue
        key = dm.memory_key(repo, mem_id)
        with store.key_lock(f"memlock:{repo.lower()}:{mem_id}"):
            latest = store.rget_json(key)
            if not isinstance(latest, dict) or latest.get("status") != "active":
                continue
            updated = dict(latest)
            updated["hits"] = int(updated.get("hits") or 0) + 1
            updated["last_used"] = now_iso
            store.rset_json(key, updated)
            kept.append(updated)
            used_ids.append(mem_id)
    return kept


# ---------------------------------------------------------------------------
# Implicit dismissal learning
# ---------------------------------------------------------------------------


def _dismissal_scope_globs(file_path: str) -> list[str]:
    clean = _norm_path(file_path)
    if not clean:
        return ["**"]
    _, ext = posixpath.splitext(clean)
    if ext and len(ext) <= 12 and ext[1:].isalnum():
        return [f"**/*{ext}"]
    return ["**"]


def _read_dismiss_count(counter_key: str) -> int:
    raw = store.rget_json(counter_key)
    if isinstance(raw, dict):
        try:
            return max(0, int(raw.get("count") or 0))
        except (TypeError, ValueError):
            return 0
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(0, raw)
    if isinstance(raw, str):
        try:
            return max(0, int(raw))
        except ValueError:
            return 0
    return 0


def _increment_dismiss_count(counter_key: str) -> int:
    """Atomically increment the dismissal counter with TTL.

    Migrates legacy JSON-object counter values (``{"count": N, ...}``) to
    integer strings before incrementing.
    """
    raw = store.rget(counter_key)
    if raw is not None and raw.lstrip().startswith("{"):
        with store.key_lock(f"memdismissmig:{counter_key}"):
            rechecked = store.rget(counter_key)
            if rechecked is not None and rechecked.lstrip().startswith("{"):
                legacy_count = _read_dismiss_count(counter_key)
                store.rset(counter_key, str(legacy_count), ex=_DISMISS_COUNTER_TTL_S)
    return store.rincr(counter_key, 1, ex=_DISMISS_COUNTER_TTL_S)


def record_finding_dismissal(
    repo: str,
    pr_number: int,
    fingerprint: str,
    *,
    finding: dict[str, Any] | None = None,
    reason: str = "",
    author: str = "",
) -> dict[str, Any]:
    """Mark a finding dismissed in Redis, atomically increment its stem counter
    (with TTL, idempotent per finding, retryable after partial failure), and
    create/boost a ``dismissed_pattern`` memory (immediately with an explicit
    ``reason``, or on the 2nd dismissal of the same stem)."""
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")
    pr_num = int(pr_number)
    if pr_num <= 0:
        raise ValueError("invalid pr_number")

    now_iso = _now_iso()
    clean_reason = str(reason or "").strip()
    clean_author = str(author or "").strip()
    has_valid_fp = bool(fingerprint and dm._FINGERPRINT_RE.fullmatch(fingerprint))

    claim_cm = (
        store.key_lock(f"memdismissclaim:{repo.lower()}:{pr_num}:{fingerprint}")
        if has_valid_fp
        else contextlib.nullcontext()
    )

    with claim_cm:
        finding_updated = False
        already_dismissed = False
        stored_finding: dict[str, Any] = {}
        fkey = ""

        if has_valid_fp:
            fkey = dm.finding_key(repo, pr_num, fingerprint)
            raw = store.rget_json(fkey)
            if isinstance(raw, dict):
                already_dismissed = raw.get("state") == "dismissed"
                stored_finding = dict(raw)
                stored_finding["state"] = "dismissed"
                if not already_dismissed or not stored_finding.get("dismissed_at"):
                    stored_finding["dismissed_at"] = now_iso
                if clean_reason:
                    stored_finding["dismissed_reason"] = clean_reason
                if clean_author:
                    stored_finding["dismissed_by"] = clean_author
                store.rset_json(fkey, stored_finding, ex=dm.FINDING_TTL_S)
                if not already_dismissed:
                    store.rset(dm.dirty_key(repo, pr_num), now_iso, ex=dm.DIRTY_TTL_S)
                finding_updated = True

        merged_finding: dict[str, Any] = {**stored_finding, **(finding if isinstance(finding, dict) else {})}
        category = str(merged_finding.get("category") or "").strip().lower()
        title = str(merged_finding.get("title") or "").strip()
        file_path = str(merged_finding.get("file") or merged_finding.get("path") or "").strip()

        title_stem = dm._title_stem(title) if title else ""
        if category and title_stem:
            stem = f"{category}:{title_stem}"
        elif title_stem:
            stem = title_stem
        elif category:
            stem = f"{category}:{fingerprint or dm._title_stem(clean_reason)}"
        else:
            stem = str(fingerprint or dm._title_stem(clean_reason) or "unknown")

        stem_hash = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
        counter_key = f"memdismiss:{repo.lower()}:{stem_hash}"
        counted_key = (
            f"memdismisscounted:{repo.lower()}:{pr_num}:{fingerprint}:{stem_hash}"
            if has_valid_fp
            else ""
        )
        done_key = (
            f"memdismissdone:{repo.lower()}:{pr_num}:{fingerprint}:{stem_hash}"
            if has_valid_fp
            else ""
        )

        if has_valid_fp:
            if stored_finding.get("dismissal_counted"):
                count = _read_dismiss_count(counter_key)
            else:
                claimed = store.rset(counted_key, "1", ex=_DISMISS_COUNTER_TTL_S, nx=True)
                if claimed:
                    try:
                        count = _increment_dismiss_count(counter_key)
                    except Exception:
                        with contextlib.suppress(Exception):
                            store.rdelete(counted_key)
                        raise
                    if finding_updated:
                        stored_finding["dismissal_counted"] = True
                        with contextlib.suppress(Exception):
                            store.rset_json(fkey, stored_finding, ex=dm.FINDING_TTL_S)
                else:
                    count = _read_dismiss_count(counter_key)
            already_completed = bool(stored_finding.get("dismissal_completed") or store.rget(done_key))
            needs_pattern_step = not already_completed
        else:
            count = _increment_dismiss_count(counter_key)
            needs_pattern_step = True

        mem_id = f"dp_{stem_hash[:12]}"
        pattern_created = False
        pattern_boosted = False
        memory_entry: dict[str, Any] | None = None

        with store.key_lock(f"memdismisspattern:{repo.lower()}:{stem_hash}"):
            existing = get_memory(repo, mem_id)
            memory_entry = existing

            if existing is not None:
                new_text = str(existing.get("text") or "")
                has_new_reason = bool(clean_reason and clean_reason.lower() not in new_text.lower())
                if needs_pattern_step or has_new_reason:
                    new_conf = min(1.0, round(float(existing.get("confidence", 0.7)) + 0.1, 3))
                    if has_new_reason:
                        candidate = f"{new_text}; {clean_reason}"
                        if len(candidate) <= dm.MEMORY_TEXT_MAX:
                            new_text = candidate
                    merged_globs = _norm_globs(
                        list(existing.get("scope_globs") or []) + _dismissal_scope_globs(file_path)
                    )
                    # Preserve existing status (e.g. keep archived patterns archived until explicitly approved).
                    memory_entry = update_memory(
                        repo,
                        mem_id,
                        {"text": new_text, "scope_globs": merged_globs, "confidence": new_conf},
                    )
                    pattern_boosted = True
            elif clean_reason or (needs_pattern_step and count >= 2):
                if category and title:
                    base_text = f"Do not flag [{category}]: {title}"
                elif title:
                    base_text = f"Do not flag: {title}"
                else:
                    base_text = f"Do not flag finding pattern ({stem})"
                if clean_reason:
                    full_text = f"{base_text} — Reason: {clean_reason}"
                else:
                    full_text = f"{base_text} (dismissed {count} times)"
                full_text = full_text[:dm.MEMORY_TEXT_MAX]
                conf = 0.8 if clean_reason else min(1.0, round(0.7 + 0.1 * max(0, count - 2), 3))
                memory_entry = add_memory(
                    repo,
                    {
                        "id": mem_id,
                        "kind": "dismissed_pattern",
                        "text": full_text,
                        "scope_globs": _dismissal_scope_globs(file_path),
                        "source": "dismissal",
                        "author": clean_author[:80] or None,
                        "confidence": conf,
                        "blocking": False,
                        "status": "active",
                    },
                )
                pattern_created = True

        if has_valid_fp:
            store.rset(done_key, now_iso, ex=_DISMISS_COUNTER_TTL_S)
            if finding_updated:
                stored_finding["dismissal_counted"] = True
                stored_finding["dismissal_completed"] = True
                store.rset_json(fkey, stored_finding, ex=dm.FINDING_TTL_S)

    return {
        "repo": repo.lower(),
        "pr_number": pr_num,
        "fingerprint": fingerprint,
        "finding_updated": finding_updated,
        "already_dismissed": already_dismissed,
        "stem": stem,
        "stem_hash": stem_hash,
        "dismissal_count": count,
        "pattern_created": pattern_created,
        "pattern_boosted": pattern_boosted,
        "memory": memory_entry,
    }


# ---------------------------------------------------------------------------
# Repo bootstrap from documentation & manifests
# ---------------------------------------------------------------------------


def _decode_github_content(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    raw_content = payload.get("content")
    if not isinstance(raw_content, str):
        return None
    encoding = str(payload.get("encoding") or "").lower()
    if encoding == "base64":
        try:
            return base64.b64decode(raw_content).decode("utf-8", errors="replace")
        except Exception:
            return None
    return raw_content


def _parse_toml_safe(text: str) -> dict[str, Any]:
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _add_unique(target: list[str], value: str) -> None:
    v = value.strip()
    if v and v not in target:
        target.append(v)


def _parse_package_json(
    text: str,
    languages: list[str],
    frameworks: list[str],
    test_cmds: list[str],
    build_cmds: list[str],
    lint_cmds: list[str],
) -> None:
    try:
        pkg = json.loads(text)
    except Exception:
        return
    if not isinstance(pkg, dict):
        return

    deps: dict[str, Any] = {}
    for bucket in ("dependencies", "devDependencies", "peerDependencies"):
        sub = pkg.get(bucket)
        if isinstance(sub, dict):
            deps.update(sub)
    dep_names = {k.lower() for k in deps}

    if "typescript" in dep_names:
        _add_unique(languages, "TypeScript")
    _add_unique(languages, "JavaScript")

    fw_map = (
        ("react", "React"),
        ("next", "Next.js"),
        ("vue", "Vue"),
        ("svelte", "Svelte"),
        ("@angular/core", "Angular"),
        ("express", "Express"),
        ("fastify", "Fastify"),
        ("@nestjs/core", "NestJS"),
        ("vite", "Vite"),
        ("tailwindcss", "Tailwind CSS"),
        ("@tauri-apps/api", "Tauri"),
        ("@tauri-apps/cli", "Tauri"),
        ("vitest", "Vitest"),
        ("jest", "Jest"),
        ("playwright", "Playwright"),
        ("@playwright/test", "Playwright"),
        ("prisma", "Prisma"),
        ("drizzle-orm", "Drizzle"),
        ("eslint", "ESLint"),
        ("prettier", "Prettier"),
    )
    for dep_key, label in fw_map:
        if dep_key in dep_names:
            _add_unique(frameworks, label)

    pm_raw = str(pkg.get("packageManager") or "").lower()
    if pm_raw.startswith("pnpm"):
        pm = "pnpm"
    elif pm_raw.startswith("yarn"):
        pm = "yarn"
    elif pm_raw.startswith("bun"):
        pm = "bun"
    else:
        pm = "npm"

    scripts = pkg.get("scripts")
    if isinstance(scripts, dict):
        if "test" in scripts and str(scripts["test"]).strip():
            _add_unique(test_cmds, f"{pm} test")
        if "build" in scripts and str(scripts["build"]).strip():
            _add_unique(build_cmds, f"{pm} run build")
        for lint_key in ("lint", "typecheck", "check"):
            if lint_key in scripts and str(scripts[lint_key]).strip():
                _add_unique(lint_cmds, f"{pm} run {lint_key}")


def _parse_pyproject_toml(
    text: str,
    languages: list[str],
    frameworks: list[str],
    test_cmds: list[str],
    build_cmds: list[str],
    lint_cmds: list[str],
) -> None:
    _add_unique(languages, "Python")
    data = _parse_toml_safe(text)
    lower = text.lower()

    dep_blob = [lower]
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    for dep in project.get("dependencies") or []:
        dep_blob.append(str(dep).lower())
    opt = project.get("optional-dependencies")
    if isinstance(opt, dict):
        for group in opt.values():
            if isinstance(group, list):
                dep_blob.extend(str(d).lower() for d in group)
    joined = "\n".join(dep_blob)

    py_fws = (
        ("fastapi", "FastAPI"),
        ("django", "Django"),
        ("flask", "Flask"),
        ("starlette", "Starlette"),
        ("pydantic", "Pydantic"),
        ("sqlalchemy", "SQLAlchemy"),
        ("pytest", "pytest"),
        ("ruff", "Ruff"),
        ("mypy", "mypy"),
        ("celery", "Celery"),
        ("httpx", "httpx"),
        ("typer", "Typer"),
    )
    for token, label in py_fws:
        if re.search(rf"\b{re.escape(token)}\b", joined):
            _add_unique(frameworks, label)

    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    if "pytest" in tool or "pytest" in joined:
        _add_unique(test_cmds, "pytest")
    if "ruff" in tool or re.search(r"\bruff\b", joined):
        _add_unique(lint_cmds, "ruff check")
    if "mypy" in tool or re.search(r"\bmypy\b", joined):
        _add_unique(lint_cmds, "mypy")

    build_sys = data.get("build-system") if isinstance(data.get("build-system"), dict) else {}
    if build_sys.get("build-backend"):
        _add_unique(build_cmds, "python -m build")


def _parse_cargo_toml(
    text: str,
    languages: list[str],
    frameworks: list[str],
    test_cmds: list[str],
    build_cmds: list[str],
    lint_cmds: list[str],
) -> None:
    _add_unique(languages, "Rust")
    data = _parse_toml_safe(text)

    dep_keys: set[str] = set()
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        sub = data.get(section)
        if isinstance(sub, dict):
            dep_keys.update(k.lower() for k in sub)
    ws = data.get("workspace")
    if isinstance(ws, dict) and isinstance(ws.get("dependencies"), dict):
        dep_keys.update(k.lower() for k in ws["dependencies"])

    lower = text.lower()
    rust_fws = (
        ("tauri", "Tauri"),
        ("tokio", "Tokio"),
        ("axum", "Axum"),
        ("actix-web", "Actix Web"),
        ("rocket", "Rocket"),
        ("serde", "Serde"),
        ("reqwest", "reqwest"),
        ("clap", "Clap"),
        ("sqlx", "SQLx"),
    )
    for key, label in rust_fws:
        if key in dep_keys or re.search(rf"(?m)^\s*{re.escape(key)}\s*=", lower):
            _add_unique(frameworks, label)

    _add_unique(test_cmds, "cargo test")
    _add_unique(build_cmds, "cargo build")
    _add_unique(lint_cmds, "cargo clippy")


def _expand_editorconfig_brace_inner(inner: str) -> list[str] | None:
    """Expand an EditorConfig ``{...}`` body into literal alternatives.

    Supports comma-separated alternatives and bounded integer ranges
    (e.g. ``{1..3}``). Returns ``None`` for invalid, reversed, or overly large
    ranges so callers skip unsupported sections instead of emitting broken globs.
    """
    stripped = inner.strip()
    if not stripped:
        return None
    if "," not in stripped and ".." in stripped:
        m = _EDITORCONFIG_INT_RANGE_RE.fullmatch(stripped)
        if not m:
            return None
        start, end = int(m.group(1)), int(m.group(2))
        if start > end or (end - start + 1) > _MAX_EDITORCONFIG_GLOBS:
            return None
        return [str(n) for n in range(start, end + 1)]

    out: list[str] = []
    for raw_part in stripped.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if ".." in part:
            m = _EDITORCONFIG_INT_RANGE_RE.fullmatch(part)
            if not m:
                return None
            start, end = int(m.group(1)), int(m.group(2))
            if start > end or (len(out) + (end - start + 1)) > _MAX_EDITORCONFIG_GLOBS:
                return None
            out.extend(str(n) for n in range(start, end + 1))
        else:
            out.append(part)
    return out or None


def _editorconfig_section_to_globs(section: str) -> list[str]:
    s = section.strip()
    if not s:
        return []
    if s in ("*", "**"):
        return ["**"]
    expanded = [s]
    while True:
        next_expanded: list[str] = []
        changed = False
        for pat in expanded:
            brace = re.search(r"\{([^{}]+)\}", pat)
            if not brace:
                next_expanded.append(pat)
                continue
            changed = True
            alts = _expand_editorconfig_brace_inner(brace.group(1))
            if not alts:
                return []
            pre = pat[:brace.start()]
            post = pat[brace.end():]
            for alt in alts:
                next_expanded.append(f"{pre}{alt}{post}")
                if len(next_expanded) > _MAX_EDITORCONFIG_GLOBS:
                    return []
        expanded = next_expanded
        if not changed:
            break
    return [g if "/" in g else f"**/{g}" for g in expanded]


def _extract_editorconfig_facts(text: str) -> list[tuple[str, list[str]]]:
    facts: list[tuple[str, list[str]]] = []
    cur_section: str | None = None
    cur_props: list[str] = []
    interesting = frozenset({
        "indent_style", "indent_size", "tab_width", "end_of_line",
        "max_line_length", "insert_final_newline", "trim_trailing_whitespace",
    })

    def _flush() -> None:
        if cur_section and cur_props:
            globs = _editorconfig_section_to_globs(cur_section)
            if not globs:
                return
            fact = f"EditorConfig [{cur_section}]: {', '.join(cur_props)}"
            facts.append((fact, globs))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            _flush()
            cur_section = line[1:-1].strip()
            cur_props = []
            continue
        if "=" in line and cur_section is not None:
            k, v = [p.strip() for p in line.split("=", 1)]
            if k.lower() in interesting and v:
                cur_props.append(f"{k.lower()}={v}")
    _flush()
    return facts


def _extract_doc_facts(filename: str, text: str) -> list[tuple[str, list[str]]]:
    """Extract concise guidance bullet points/rules from Markdown docs."""
    facts: list[tuple[str, list[str]]] = []
    in_code_fence = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence or not stripped or stripped.startswith("#"):
            continue
        bullet = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", stripped).strip()
        if not bullet or len(bullet) < 18 or _MARKDOWN_LINK_ONLY_RE.fullmatch(bullet):
            continue
        is_bullet = bullet != stripped
        if not _GUIDANCE_RE.search(bullet) and not (is_bullet and len(bullet) <= 240):
            continue
        # Only keep non-bullet lines if they explicitly state a rule/convention.
        if not is_bullet and not _GUIDANCE_RE.search(bullet):
            continue
        cleaned = re.sub(r"\s+", " ", bullet)[:300]
        if len(cleaned) < 18:
            continue
        facts.append((f"[{filename}] {cleaned}", ["**"]))
        if len(facts) >= _MAX_BOOTSTRAP_DOC_FACTS:
            break
    return facts


def _bootstrap_fact_origin(entry: dict[str, Any]) -> str | None:
    """Return the bootstrap source identifier for a bootstrap-owned ``repo_fact``."""
    if entry.get("source") != "bootstrap" or entry.get("kind") != "repo_fact":
        return None
    author = str(entry.get("author") or "").strip()
    if author.startswith("bootstrap:") and len(author) > len("bootstrap:"):
        return author.split(":", 1)[1]
    text = str(entry.get("text") or "").strip()
    for doc_name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"):
        if text.startswith(f"[{doc_name}] "):
            return doc_name
    if text.startswith("EditorConfig ["):
        return ".editorconfig"
    if text.startswith("Repository tech stack — ") or text.startswith("Repository commands — "):
        return "manifests"
    return None


async def bootstrap_repo(
    repo: str,
    github_token: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Bootstrap repo profile and deduplicated ``repo_fact`` memories from
    standard repository docs & manifests via the GitHub Contents API.

    Distinguishes verified file absence (HTTP 404) from fetch failures so a
    transient GitHub outage never wipes the last valid profile or stale-prunes
    facts from unreadable sources. For sources that were read (or verified
    absent), stale bootstrap-owned facts no longer produced by that source are
    removed while manual memories are preserved.
    """
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")

    if github_token is None:
        token, _ = await github_app.token_for_repo(repo)
    else:
        token = github_token

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    fetched: dict[str, str] = {}
    absent: set[str] = set()
    fetch_errors: dict[str, str] = {}
    client_cm = contextlib.nullcontext(client) if client is not None else httpx.AsyncClient(timeout=20.0)
    async with client_cm as http:
        for path in BOOTSTRAP_FILES:
            try:
                resp = await http.get(
                    f"{GITHUB_API}/repos/{repo}/contents/{path}",
                    headers=headers,
                )
            except Exception as exc:
                log.warning("memory.bootstrap: fetch %s/%s failed (%s)", repo, path, type(exc).__name__)
                fetch_errors[path] = type(exc).__name__
                continue
            if resp.status_code == 404:
                absent.add(path)
                continue
            if resp.status_code != 200:
                log.warning("memory.bootstrap: fetch %s/%s returned HTTP %s", repo, path, resp.status_code)
                fetch_errors[path] = f"http_{resp.status_code}"
                continue
            try:
                decoded = _decode_github_content(resp.json())
            except Exception as exc:
                log.warning("memory.bootstrap: decode %s/%s failed (%s)", repo, path, type(exc).__name__)
                fetch_errors[path] = "decode_error"
                continue
            if decoded is None:
                fetch_errors[path] = "decode_error"
                continue
            fetched[path] = decoded

    languages: list[str] = []
    frameworks: list[str] = []
    test_cmds: list[str] = []
    build_cmds: list[str] = []
    lint_cmds: list[str] = []
    # (origin, text, scope_globs)
    candidate_facts: list[tuple[str, str, list[str]]] = []

    if "package.json" in fetched:
        _parse_package_json(fetched["package.json"], languages, frameworks, test_cmds, build_cmds, lint_cmds)
    if "pyproject.toml" in fetched:
        _parse_pyproject_toml(fetched["pyproject.toml"], languages, frameworks, test_cmds, build_cmds, lint_cmds)
    if "Cargo.toml" in fetched:
        _parse_cargo_toml(fetched["Cargo.toml"], languages, frameworks, test_cmds, build_cmds, lint_cmds)
    if ".editorconfig" in fetched:
        for fact_text, globs in _extract_editorconfig_facts(fetched[".editorconfig"]):
            candidate_facts.append((".editorconfig", fact_text, globs))
    for doc_name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"):
        if doc_name in fetched:
            for fact_text, globs in _extract_doc_facts(doc_name, fetched[doc_name]):
                candidate_facts.append((doc_name, fact_text, globs))

    existing_profile = get_repo_profile(repo)
    manifest_errors = [p for p in _MANIFEST_FILES if p in fetch_errors]
    profile_preserved = False
    profile: dict[str, Any] | None

    if manifest_errors:
        if existing_profile is not None:
            profile = existing_profile
            profile_preserved = True
        elif any(p in fetched for p in _MANIFEST_FILES):
            profile = {
                "repo": repo.lower(),
                "languages": languages,
                "frameworks": frameworks,
                "test_commands": test_cmds,
                "build_commands": build_cmds,
                "lint_commands": lint_cmds,
                "files_indexed": sorted(fetched),
                "bootstrapped_at": _now_iso(),
            }
            store.rset_json(dm.repo_profile_key(repo), profile)
        else:
            profile = None
    else:
        profile = {
            "repo": repo.lower(),
            "languages": languages,
            "frameworks": frameworks,
            "test_commands": test_cmds,
            "build_commands": build_cmds,
            "lint_commands": lint_cmds,
            "files_indexed": sorted(fetched),
            "bootstrapped_at": _now_iso(),
        }
        store.rset_json(dm.repo_profile_key(repo), profile)

    # Derive manifest summary facts only when manifests are authoritative or no prior profile existed.
    if not profile_preserved:
        if languages or frameworks:
            stack_parts: list[str] = []
            if languages:
                stack_parts.append("Languages: " + ", ".join(languages))
            if frameworks:
                stack_parts.append("Frameworks: " + ", ".join(frameworks))
            candidate_facts.insert(0, ("manifests", "Repository tech stack — " + "; ".join(stack_parts), ["**"]))

        if test_cmds or build_cmds or lint_cmds:
            cmd_parts: list[str] = []
            if test_cmds:
                cmd_parts.append("test: " + ", ".join(test_cmds))
            if build_cmds:
                cmd_parts.append("build: " + ", ".join(build_cmds))
            if lint_cmds:
                cmd_parts.append("lint: " + ", ".join(lint_cmds))
            candidate_facts.insert(
                1 if (languages or frameworks) else 0,
                ("manifests", "Repository commands — " + "; ".join(cmd_parts), ["**"]),
            )

    # Sources that were read (200) or verified absent (404) are authoritative:
    # obsolete bootstrap-owned facts from those sources should be removed.
    authoritative_sources: set[str] = set()
    for src_file in (".editorconfig", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"):
        if src_file in fetched or src_file in absent:
            authoritative_sources.add(src_file)
    if not manifest_errors:
        authoritative_sources.add("manifests")

    desired_by_origin: dict[str, set[tuple[str, tuple[str, ...]]]] = {}
    normalized_candidates: list[tuple[str, str, list[str], str]] = []
    for origin, raw_text, raw_globs in candidate_facts:
        bounded_text = raw_text[:dm.MEMORY_TEXT_MAX]
        norm_text = _norm_dedupe_text(bounded_text)
        norm_globs = _norm_globs(raw_globs)
        if not norm_text:
            continue
        desired_by_origin.setdefault(origin, set()).add((norm_text, tuple(sorted(norm_globs))))
        normalized_candidates.append((origin, bounded_text, norm_globs, norm_text))

    existing_entries = _load_all_raw(repo)
    removed = 0
    surviving_entries: list[dict[str, Any]] = []
    for entry in existing_entries:
        origin = _bootstrap_fact_origin(entry)
        if origin is not None and origin in authoritative_sources:
            sig = (
                _norm_dedupe_text(str(entry.get("text") or "")),
                tuple(sorted(_norm_globs(entry.get("scope_globs")))),
            )
            if sig not in desired_by_origin.get(origin, set()):
                eid = str(entry.get("id") or "")
                if eid and delete_memory(repo, eid):
                    removed += 1
                    continue
        surviving_entries.append(entry)

    existing_texts = {_norm_dedupe_text(str(e.get("text") or "")) for e in surviving_entries}
    created: list[dict[str, Any]] = []
    skipped = 0

    for origin, bounded_text, norm_globs, norm_text in normalized_candidates:
        if norm_text in existing_texts:
            skipped += 1
            continue
        existing_texts.add(norm_text)
        mem = add_memory(
            repo,
            {
                "kind": "repo_fact",
                "text": bounded_text,
                "scope_globs": norm_globs,
                "source": "bootstrap",
                "author": f"bootstrap:{origin}"[:80],
                "confidence": 0.85,
                "blocking": False,
                "status": "active",
            },
        )
        created.append(mem)

    return {
        "profile": profile,
        "created": created,
        "skipped": skipped,
        "removed": removed,
        "fetch_errors": fetch_errors,
        "partial": bool(fetch_errors),
        "ok": not bool(fetch_errors),
        "profile_preserved": profile_preserved,
    }


# ---------------------------------------------------------------------------
# Merged-PR candidate extraction
# ---------------------------------------------------------------------------


def _is_bot_item(item: dict[str, Any]) -> bool:
    if item.get("sender_is_bot"):
        return True
    user = item.get("user")
    if isinstance(user, dict):
        if str(user.get("type") or "").lower() == "bot":
            return True
        if str(user.get("login") or "").lower().endswith("[bot]"):
            return True
    author = str(item.get("author") or item.get("by") or "").lower()
    return author.endswith("[bot]")


def _clean_comment_guidance(raw_body: str) -> str:
    # Strip quoted reply lines and fenced code blocks before testing guidance patterns.
    lines: list[str] = []
    in_fence = False
    for line in str(raw_body or "").splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not s or s.startswith(">"):
            continue
        lines.append(s)
    joined = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not joined or _NOISE_COMMENT_RE.fullmatch(joined):
        return ""
    # Strip explicit `@pr-agent remember/learn` prefix if present.
    stripped = _REMEMBER_PREFIX_RE.sub("", joined).strip()
    if stripped != joined and len(stripped) >= 10:
        return stripped[:400]
    if not _GUIDANCE_RE.search(joined):
        return ""
    # Avoid capturing bare questions with no actionable rule.
    if joined.endswith("?") and "?" not in joined[:-1] and not re.search(r"(?i)\b(?:always|never|must|remember|do not|don'?t)\b", joined):
        return ""
    if len(joined) < 15:
        return ""
    return joined[:400]


def extract_learnings_from_merged_pr(
    repo: str,
    pr_number: int,
    activity_or_comments: list[dict[str, Any]],
    *,
    auto_approve: bool = False,
) -> list[dict[str, Any]]:
    """Extract deduplicated candidate learnings from human review guidance in a
    merged PR. Saved as ``status="active"`` if ``auto_approve`` else ``"pending"``."""
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")
    if int(pr_number) <= 0:
        raise ValueError("invalid pr_number")

    existing_texts = {_norm_dedupe_text(str(e.get("text") or "")) for e in _load_all_raw(repo)}
    created: list[dict[str, Any]] = []
    status = "active" if auto_approve else "pending"

    for item in activity_or_comments or []:
        if len(created) >= _MAX_MERGED_PR_LEARNINGS:
            break
        if not isinstance(item, dict) or _is_bot_item(item):
            continue
        raw_body = str(item.get("body") or item.get("text") or item.get("comment") or "").strip()
        if not raw_body:
            continue
        cleaned = _clean_comment_guidance(raw_body)
        if not cleaned:
            continue
        norm = _norm_dedupe_text(cleaned)
        if norm in existing_texts:
            continue
        existing_texts.add(norm)

        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        author = (str(item.get("author") or user.get("login") or item.get("by") or "").strip()[:80]) or None
        path = _norm_path(str(item.get("path") or item.get("file") or ""))
        scope_globs = [_escape_glob_literal(path)] if path else ["**"]

        mem = add_memory(
            repo,
            {
                "kind": "learning",
                "text": cleaned[:dm.MEMORY_TEXT_MAX],
                "scope_globs": scope_globs,
                "source": "merged_outcome",
                "author": author,
                "confidence": 0.75,
                "blocking": False,
                "status": status,
            },
        )
        created.append(mem)

    return created
