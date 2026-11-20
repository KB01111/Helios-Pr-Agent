"""Shared data model for the review engine, repo memory and merge readiness.

One place for Redis key layout, TTLs, size budgets and the JSON shapes the
later phases (structured findings, repo memory, merge-readiness score) build
on. Everything is plain JSON in Redis so the free tier (~30 MB) is enough:
budgets below keep a busy repo well under 1 MB.

Key layout
----------
``repo:{repo}:profile``               detected languages/frameworks/commands
``repo:{repo}:config``                parsed ``.pragent.yaml`` (cached per blob sha)
``mem:{repo}:{id}``                   one memory entry (learning, rule, fact, ...)
``finding:{repo}:{pr}:{fingerprint}`` one finding and its lifecycle state
``merge:{repo}:{pr}``                 latest merge-readiness report
``pract:{repo}:{pr}``                 capped activity log (newest first)
``prdirty:{repo}:{pr}``               readiness must be recomputed (set by events)
``prhead:{repo}:{sha}``               head sha -> PR number (maps ``status`` events)
``delivery:{id}``                     webhook redelivery guard

Repository names are lower-cased in keys: GitHub treats them case-insensitively.
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Budgets / TTLs
# ---------------------------------------------------------------------------

ACTIVITY_CAP = 200
ACTIVITY_TTL_S = 30 * 24 * 3600
DIRTY_TTL_S = 7 * 24 * 3600
PRHEAD_TTL_S = 30 * 24 * 3600
DELIVERY_TTL_S = 24 * 3600
FINDING_TTL_S = 60 * 24 * 3600
MERGE_REPORT_TTL_S = 60 * 24 * 3600
MEMORY_MAX_ENTRIES_PER_REPO = 500
MEMORY_TEXT_MAX = 2000

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16,64}$")
_DELIVERY_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def valid_repo(repo: object) -> bool:
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        return False
    owner, name = repo.split("/", 1)
    return owner not in (".", "..") and name not in (".", "..")


def _r(repo: str) -> str:
    if not valid_repo(repo):
        raise ValueError("invalid repository name")
    return repo.lower()


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------


def repo_profile_key(repo: str) -> str:
    return f"repo:{_r(repo)}:profile"


def repo_config_key(repo: str) -> str:
    return f"repo:{_r(repo)}:config"


def memory_key(repo: str, mem_id: str) -> str:
    return f"mem:{_r(repo)}:{mem_id}"


def memory_prefix(repo: str) -> str:
    return f"mem:{_r(repo)}:"


def finding_key(repo: str, pr_number: int, fingerprint: str) -> str:
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("invalid fingerprint")
    return f"finding:{_r(repo)}:{int(pr_number)}:{fingerprint}"


def merge_report_key(repo: str, pr_number: int) -> str:
    return f"merge:{_r(repo)}:{int(pr_number)}"


def activity_key(repo: str, pr_number: int) -> str:
    return f"pract:{_r(repo)}:{int(pr_number)}"


def dirty_key(repo: str, pr_number: int) -> str:
    return f"prdirty:{_r(repo)}:{int(pr_number)}"


def prhead_key(repo: str, sha: str) -> str:
    return f"prhead:{_r(repo)}:{sha.lower()}"


def delivery_key(delivery_id: str) -> str | None:
    """Redelivery guard key, or None for a missing/odd id (then no dedupe)."""
    if not isinstance(delivery_id, str) or not _DELIVERY_RE.fullmatch(delivery_id):
        return None
    return f"delivery:{delivery_id}"


# ---------------------------------------------------------------------------
# Shapes (used from P1 on; validated at the API boundary)
# ---------------------------------------------------------------------------

Severity = Literal["critical", "major", "minor", "nit"]
Category = Literal["bug", "security", "perf", "maintainability", "style", "test", "docs"]
FindingState = Literal["open", "resolved", "dismissed", "outdated"]


class Finding(BaseModel):
    """One review finding, as produced by the engine or a tool runner."""

    file: str = Field(max_length=500)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    side: Literal["LEFT", "RIGHT"] = "RIGHT"
    severity: Severity
    category: Category
    title: str = Field(max_length=200)
    body: str = Field(default="", max_length=8000)
    suggestion_patch: str | None = Field(default=None, max_length=8000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: Literal["llm", "tool", "memory"] = "llm"
    state: FindingState = "open"
    learning_ids: list[str] = Field(default_factory=list)


def _normalize_code(span: str) -> str:
    return re.sub(r"\s+", " ", span).strip()


def _title_stem(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return " ".join(words[:8])


def finding_fingerprint(file: str, code_span: str, category: str, title: str) -> str:
    """Stable id for "the same finding": survives whitespace edits and line shifts,
    so incremental reviews never re-post it."""
    basis = "\x1f".join((file.strip(), _normalize_code(code_span), category, _title_stem(title)))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


MemoryKind = Literal["learning", "dismissed_pattern", "repo_fact", "incident"]
MemorySource = Literal["chat", "dismissal", "manual", "merged_outcome", "bootstrap"]


class MemoryEntry(BaseModel):
    """One unit of repo-specific memory (P2)."""

    id: str = ""
    kind: MemoryKind = "learning"
    text: str = Field(min_length=1, max_length=MEMORY_TEXT_MAX)
    scope_globs: list[str] = Field(default_factory=lambda: ["**"], max_length=20)
    source: MemorySource = "manual"
    author: str | None = Field(default=None, max_length=80)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    blocking: bool = False
    status: Literal["active", "pending", "archived"] = "active"
    hits: int = 0
    created_at: str = ""
    last_used: str | None = None


ReadinessBand = Literal["ready", "ready_with_nits", "needs_work", "blocked"]


class ReadinessItem(BaseModel):
    kind: Literal["blocker", "should_fix", "nice_to_have"]
    code: str = Field(max_length=60)
    text: str = Field(max_length=500)
    url: str | None = None
    done: bool = False


class ReadinessReport(BaseModel):
    """Merge-readiness result for one PR head (P4)."""

    repo: str
    pr_number: int
    head_sha: str
    score: int = Field(ge=0, le=100)
    band: ReadinessBand
    items: list[ReadinessItem] = Field(default_factory=list)
    policy_version: str = "v1"
    computed_at: str = ""
