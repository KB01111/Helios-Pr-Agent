"""Validation helpers for values interpolated into sandbox commands.

Webhook payloads are attacker-controlled by anyone who can open a pull
request. Repository names, refs, and SHAs must never be pasted into a
shell command without validation and quoting.
"""
from __future__ import annotations

import re
import shlex

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
# Covers hex SHAs and ordinary branch/tag names. Reject strings starting
# with "-" so they can never be parsed as a git option.
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/~^@+#=%-]{0,199}$")
_LABEL_RE = re.compile(r"[^A-Za-z0-9_.@-]")


class UnsafeSandboxInput(ValueError):
    """Raised when a webhook-derived value is not safe to execute."""


def safe_repo(repo: object) -> str:
    value = str(repo or "")
    if not _REPO_RE.fullmatch(value):
        raise UnsafeSandboxInput("invalid repository name")
    owner, name = value.split("/", 1)
    if owner in (".", "..") or name in (".", ".."):
        raise UnsafeSandboxInput("invalid repository name")
    return value


def safe_git_ref(ref: object) -> str:
    value = str(ref or "")
    if not _REF_RE.fullmatch(value) or ".." in value:
        raise UnsafeSandboxInput("invalid git ref")
    return value


def shell_quote(value: object) -> str:
    """Quote one value for a POSIX shell (sandboxes run Linux)."""
    return shlex.quote(str(value))


def safe_label(value: object, fallback: str = "agent") -> str:
    """Sanitize a display label that is only ever echoed, never executed."""
    cleaned = _LABEL_RE.sub("_", str(value or ""))[:64]
    return cleaned or fallback
