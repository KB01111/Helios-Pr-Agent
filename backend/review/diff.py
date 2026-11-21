"""Unified diff parser, incremental diff fetcher, and risk-prioritized diff budgeting.

Tracks exact valid GitHub Review API inline comment line numbers (`right_lines`
and `left_lines`) per file and hunk, line-to-code lookup for stable finding
fingerprints, and decider-aligned risk scoring so security/source files never
get starved by tests, docs, or lockfiles.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

try:  # cwd=backend / PYTHONPATH=backend
    from sandboxes import decider
    from sandboxes.providers_review import (
        GITHUB_API,
        _NOISY_SUFFIXES,
        _TRUNC_MARK,
        _fetch_pr_files_diff,
        _glob_matches,
    )
    from sandboxes.safety import UnsafeSandboxInput, safe_git_ref
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend.sandboxes import decider  # type: ignore[no-redef]
    from backend.sandboxes.providers_review import (  # type: ignore[no-redef]
        GITHUB_API,
        _NOISY_SUFFIXES,
        _TRUNC_MARK,
        _fetch_pr_files_diff,
        _glob_matches,
    )
    from backend.sandboxes.safety import UnsafeSandboxInput, safe_git_ref  # type: ignore[no-redef]

from .config import PathFilters

log = logging.getLogger("pr-agent.review.diff")

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:\s?(.*))?$"
)
_DIFF_GIT_RE = re.compile(
    r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$'
)
_MIN_FILE_SLICE = 120


class DiffFetchError(RuntimeError):
    """Raised when the PR diff cannot be fetched from GitHub."""


def _normalize_path(path: str) -> str:
    s = (path or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    if s.startswith("a/") or s.startswith("b/"):
        s = s[2:]
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


# ---------------------------------------------------------------------------
# Diff data structures
# ---------------------------------------------------------------------------


@dataclass
class DiffLine:
    kind: str  # "add" | "del" | "context"
    old_line: int | None
    new_line: int | None
    content: str


@dataclass
class DiffHunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section_header: str = ""
    lines: list[DiffLine] = field(default_factory=list)
    right_lines: set[int] = field(default_factory=set)
    left_lines: set[int] = field(default_factory=set)
    added_lines: set[int] = field(default_factory=set)
    deleted_lines: set[int] = field(default_factory=set)
    raw: str = ""

    def valid_lines(self, side: str = "RIGHT") -> set[int]:
        return self.left_lines if side.upper() == "LEFT" else self.right_lines

    def render_numbered(self) -> str:
        """Render hunk lines with explicit GitHub line numbers (`R<n>` / `L<n>`)."""
        out = [self.header]
        for dl in self.lines:
            if dl.kind == "add":
                out.append(f"[R{dl.new_line}] +{dl.content}")
            elif dl.kind == "del":
                out.append(f"[L{dl.old_line}] -{dl.content}")
            else:
                out.append(f"[R{dl.new_line}]  {dl.content}")
        return "\n".join(out)


@dataclass
class DiffFile:
    path: str
    old_path: str
    status: str = "modified"  # "added" | "deleted" | "renamed" | "modified"
    is_binary: bool = False
    hunks: list[DiffHunk] = field(default_factory=list)
    right_lines: set[int] = field(default_factory=set)
    left_lines: set[int] = field(default_factory=set)
    added_lines: set[int] = field(default_factory=set)
    deleted_lines: set[int] = field(default_factory=set)
    right_code: dict[int, str] = field(default_factory=dict)
    left_code: dict[int, str] = field(default_factory=dict)
    additions: int = 0
    deletions: int = 0
    raw: str = ""

    def valid_lines(self, side: str = "RIGHT") -> set[int]:
        return self.left_lines if side.upper() == "LEFT" else self.right_lines

    def code_map(self, side: str = "RIGHT") -> dict[int, str]:
        return self.left_code if side.upper() == "LEFT" else self.right_code

    def code_span(self, start_line: int, end_line: int, side: str = "RIGHT") -> str:
        cmap = self.code_map(side)
        lo, hi = min(start_line, end_line), max(start_line, end_line)
        lines = [cmap[ln] for ln in range(lo, hi + 1) if ln in cmap]
        return "\n".join(lines)

    def hunk_for_line(self, line: int, side: str = "RIGHT") -> DiffHunk | None:
        for h in self.hunks:
            if line in h.valid_lines(side):
                return h
        return None

    def nearest_valid_line(self, line: int, side: str = "RIGHT", max_distance: int = 2) -> int | None:
        valid = self.valid_lines(side)
        if not valid:
            return None
        if line in valid:
            return line
        best: int | None = None
        best_dist = max_distance + 1
        for cand in valid:
            d = abs(cand - line)
            if d < best_dist or (d == best_dist and best is not None and cand < best):
                best_dist = d
                best = cand
        return best if best_dist <= max_distance else None

    def render_numbered(self) -> str:
        """Render this file's diff with explicit line numbers for Pass B prompts."""
        header = f"diff --git a/{self.old_path} b/{self.path}\n--- a/{self.old_path}\n+++ b/{self.path}"
        if self.is_binary:
            return f"{header}\nBinary file ({self.status})\n"
        if not self.hunks:
            return self.raw.rstrip() + "\n"
        body = "\n".join(h.render_numbered() for h in self.hunks)
        return f"{header}\n{body}\n"


@dataclass
class ParsedDiff:
    files: list[DiffFile] = field(default_factory=list)
    files_by_path: dict[str, DiffFile] = field(default_factory=dict)
    raw: str = ""

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def total_additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    def get_file(self, path: str) -> DiffFile | None:
        if path in self.files_by_path:
            return self.files_by_path[path]
        norm = _normalize_path(path)
        return self.files_by_path.get(norm)

    def has_line(self, path: str, line: int, side: str = "RIGHT") -> bool:
        df = self.get_file(path)
        if df is None:
            return False
        return line in df.valid_lines(side)

    def changed_files_and_lines(self) -> dict[str, set[int]]:
        """Map each changed file path to the set of modified line numbers (both sides)."""
        out: dict[str, set[int]] = {}
        for df in self.files:
            # Union of added and deleted line numbers; empty set signals binary/metadata-only change.
            out[df.path] = set(df.added_lines) | set(df.deleted_lines)
        return out

    def render_numbered(self) -> str:
        return "".join(f.render_numbered() for f in self.files)


@dataclass
class FetchedDiff:
    diff: str
    is_incremental: bool = False
    incremental_from: str | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Unified diff parser
# ---------------------------------------------------------------------------


def _split_diff_chunks(diff_text: str) -> list[str]:
    if not diff_text:
        return []
    chunks: list[str] = []
    cur: list[str] = []
    saw_git_header = False
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            saw_git_header = True
            if cur:
                chunks.append("".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        chunks.append("".join(cur))
    if not saw_git_header and chunks:
        # Handle bare `--- a/...` / `+++ b/...` diffs without `diff --git`
        bare_chunks: list[str] = []
        cur = []
        for line in diff_text.splitlines(keepends=True):
            if line.startswith("--- ") and cur and any(l.startswith("@@ ") for l in cur):
                bare_chunks.append("".join(cur))
                cur = [line]
            else:
                cur.append(line)
        if cur:
            bare_chunks.append("".join(cur))
        return bare_chunks
    return chunks


def _parse_file_chunk(chunk: str) -> DiffFile | None:
    lines = chunk.splitlines()
    if not lines:
        return None

    old_path = ""
    new_path = ""
    status = "modified"
    is_binary = False
    rename_from = ""
    rename_to = ""

    idx = 0
    # Parse file headers up to first hunk
    while idx < len(lines) and not lines[idx].startswith("@@ "):
        line = lines[idx]
        if line.startswith("diff --git "):
            m = _DIFF_GIT_RE.match(line)
            if m:
                old_path = _normalize_path(m.group(1))
                new_path = _normalize_path(m.group(2))
            else:
                parts = line.rsplit(" b/", 1)
                if len(parts) == 2:
                    new_path = _normalize_path(parts[1])
                    old_part = parts[0][len("diff --git "):]
                    old_path = _normalize_path(old_part)
        elif line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "deleted"
        elif line.startswith("rename from "):
            status = "renamed"
            rename_from = _normalize_path(line[len("rename from "):])
        elif line.startswith("rename to "):
            status = "renamed"
            rename_to = _normalize_path(line[len("rename to "):])
        elif line.startswith("--- "):
            raw_old = line[4:].split("\t", 1)[0].strip()
            if raw_old == "/dev/null":
                status = "added"
            elif not old_path:
                old_path = _normalize_path(raw_old)
        elif line.startswith("+++ "):
            raw_new = line[4:].split("\t", 1)[0].strip()
            if raw_new == "/dev/null":
                status = "deleted"
            else:
                new_path = _normalize_path(raw_new)
        elif (
            line.startswith("Binary files ")
            or line.startswith("GIT binary patch")
            or line.startswith("Binary files or large patch omitted")
        ):
            is_binary = True
        idx += 1

    if rename_from:
        old_path = rename_from
    if rename_to:
        new_path = rename_to

    path = new_path or old_path
    if not path:
        return None
    if not old_path:
        old_path = path

    df = DiffFile(
        path=path,
        old_path=old_path,
        status=status,
        is_binary=is_binary,
        raw=chunk,
    )

    # Parse hunks
    cur_hunk: DiffHunk | None = None
    cur_hunk_raw: list[str] = []
    old_ln = 0
    new_ln = 0
    old_rem = 0
    new_rem = 0

    def _finish_hunk() -> None:
        nonlocal cur_hunk, cur_hunk_raw
        if cur_hunk is not None:
            cur_hunk.raw = "\n".join(cur_hunk_raw) + "\n"
            df.hunks.append(cur_hunk)
            df.right_lines.update(cur_hunk.right_lines)
            df.left_lines.update(cur_hunk.left_lines)
            df.added_lines.update(cur_hunk.added_lines)
            df.deleted_lines.update(cur_hunk.deleted_lines)
            cur_hunk = None
            cur_hunk_raw = []

    while idx < len(lines):
        line = lines[idx]
        idx += 1
        m = _HUNK_HEADER_RE.match(line)
        if m:
            _finish_hunk()
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            section = (m.group(5) or "").strip()
            cur_hunk = DiffHunk(
                header=line,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                section_header=section,
            )
            cur_hunk_raw = [line]
            old_ln = old_start
            new_ln = new_start
            old_rem = old_count
            new_rem = new_count
            continue

        if cur_hunk is None:
            continue

        if line.startswith("\\ No newline at end of file"):
            cur_hunk_raw.append(line)
            continue

        if line.startswith("+"):
            content = line[1:]
            cur_hunk_raw.append(line)
            cur_hunk.lines.append(DiffLine(kind="add", old_line=None, new_line=new_ln, content=content))
            cur_hunk.right_lines.add(new_ln)
            cur_hunk.added_lines.add(new_ln)
            df.right_code[new_ln] = content
            df.additions += 1
            new_ln += 1
            new_rem = max(0, new_rem - 1)
        elif line.startswith("-"):
            content = line[1:]
            cur_hunk_raw.append(line)
            cur_hunk.lines.append(DiffLine(kind="del", old_line=old_ln, new_line=None, content=content))
            cur_hunk.left_lines.add(old_ln)
            cur_hunk.deleted_lines.add(old_ln)
            df.left_code[old_ln] = content
            df.deletions += 1
            old_ln += 1
            old_rem = max(0, old_rem - 1)
        elif line.startswith(" ") or (line == "" and (old_rem > 0 or new_rem > 0)):
            content = line[1:] if line.startswith(" ") else ""
            cur_hunk_raw.append(line if line.startswith(" ") else " ")
            cur_hunk.lines.append(DiffLine(kind="context", old_line=old_ln, new_line=new_ln, content=content))
            cur_hunk.right_lines.add(new_ln)
            cur_hunk.left_lines.add(old_ln)
            df.right_code[new_ln] = content
            df.left_code[old_ln] = content
            old_ln += 1
            new_ln += 1
            old_rem = max(0, old_rem - 1)
            new_rem = max(0, new_rem - 1)

    _finish_hunk()
    return df


def parse_unified_diff(diff_text: str) -> ParsedDiff:
    """Parse unified diff text into exact per-file valid inline comment line sets."""
    parsed = ParsedDiff(raw=diff_text or "")
    if not diff_text or not diff_text.strip():
        return parsed
    for chunk in _split_diff_chunks(diff_text):
        df = _parse_file_chunk(chunk)
        if df is None:
            continue
        parsed.files.append(df)
        parsed.files_by_path[df.path] = df
        norm = _normalize_path(df.path)
        parsed.files_by_path[norm] = df
    return parsed


# ---------------------------------------------------------------------------
# Incremental diff fetcher
# ---------------------------------------------------------------------------


async def fetch_incremental_diff(
    client: Any,
    repo: str,
    pr_number: int,
    base_sha: str | None,
    head_sha: str | None,
    last_reviewed_sha: str | None,
    github_token: str,
) -> FetchedDiff:
    """Fetch incremental diff (`compare/{last}...{head}`) when possible,
    falling back cleanly to the full PR diff (with HTTP 406 PR-files fallback)."""
    notes: list[str] = []
    diff_headers = {
        "Accept": "application/vnd.github.diff",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    last_sha = (last_reviewed_sha or "").strip()
    cur_head = (head_sha or "").strip()

    # 1. Incremental fast-path when last_reviewed_sha & head_sha are both valid
    if last_sha and cur_head:
        try:
            safe_last = safe_git_ref(last_sha)
            safe_head = safe_git_ref(cur_head)
        except UnsafeSandboxInput:
            safe_last = ""
            safe_head = ""

        if safe_last and safe_head:
            if safe_last.lower() == safe_head.lower():
                notes.append(f"head {safe_head[:7]} matches last_reviewed_sha; no new commits")
                return FetchedDiff(diff="", is_incremental=True, incremental_from=safe_last, notes=notes)
            try:
                resp = await client.get(
                    f"{GITHUB_API}/repos/{repo}/compare/{safe_last}...{safe_head}",
                    headers=diff_headers,
                )
                if resp.status_code == 200:
                    text = resp.text or ""
                    if text.strip():
                        notes.append(f"incremental diff {safe_last[:7]}..{safe_head[:7]}")
                        return FetchedDiff(
                            diff=text,
                            is_incremental=True,
                            incremental_from=safe_last,
                            notes=notes,
                        )
                    # Force-push rollback or diverged empty compare: fall back to full PR diff
                    notes.append(
                        f"incremental compare {safe_last[:7]}..{safe_head[:7]} was empty; falling back to full PR diff"
                    )
                else:
                    notes.append(
                        f"incremental compare {safe_last[:7]}..{safe_head[:7]} returned HTTP {resp.status_code}; falling back to full PR diff"
                    )
            except Exception as exc:
                notes.append(
                    f"incremental compare failed ({type(exc).__name__}); falling back to full PR diff"
                )

    # 2. Full PR diff (with existing 406 PR-files fallback)
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/pulls/{int(pr_number)}",
        headers=diff_headers,
    )
    if resp.status_code == 406:
        notes.append("diff exceeded GitHub 406 limit; falling back to PR files patches")
        fallback = await _fetch_pr_files_diff(client, repo, int(pr_number), github_token)
        if fallback is None:
            raise DiffFetchError("PR files fallback failed after HTTP 406")
        fb_diff, fb_notes = fallback
        notes.extend(fb_notes)
        return FetchedDiff(diff=fb_diff, is_incremental=False, incremental_from=None, notes=notes)

    if resp.status_code != 200:
        raise DiffFetchError(f"diff fetch failed HTTP {resp.status_code}")

    return FetchedDiff(
        diff=resp.text or "",
        is_incremental=False,
        incremental_from=None,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Risk-prioritized diff budgeting
# ---------------------------------------------------------------------------


_KIND_PRIORITY: dict[str, int] = {
    "sensitive": 100,
    "source": 80,
    "ui": 70,
    "config": 60,
    "other": 50,
    "test": 35,
    "docs": 20,
    "lock": 0,
}


def _is_noisy_file(df: DiffFile) -> bool:
    if df.is_binary:
        return True
    if decider._is_lockfile(df.path):
        return True
    low = df.path.lower()
    if low.endswith(_NOISY_SUFFIXES):
        return True
    if "Binary files" in df.raw:
        return True
    return False


def file_risk_score(df: DiffFile) -> int:
    """Risk score (0..110) combining decider file classification and added-line safety signals."""
    if _is_noisy_file(df):
        return 0
    kind = decider._kind(df.path)
    score = _KIND_PRIORITY.get(kind, 50)
    # Boost if added lines contain secret-like tokens or curl|sh patterns
    added_text = "\n".join(df.right_code[ln] for ln in sorted(df.added_lines) if ln in df.right_code)
    if added_text:
        if any(rx.search(added_text) for rx in decider._SECRET_RES) or decider._SHELL_PIPE_RE.search(added_text):
            score = max(score, 110)
    return score


def select_budgeted_files(
    parsed_diff: ParsedDiff,
    budget_chars: int,
    *,
    path_filters: PathFilters | dict[str, Any] | None = None,
    numbered: bool = False,
) -> tuple[list[tuple[DiffFile, str]], list[str]]:
    """Select and budget diff files in descending risk priority order.

    Drops lockfiles/binaries and enforces `path_filters` and `budget_chars`.
    Returns `(selected[(DiffFile, chunk_text)], notes)`.
    """
    budget = max(200, int(budget_chars))
    notes: list[str] = []
    if not parsed_diff.files:
        return [], notes

    pf: PathFilters | None = None
    if path_filters:
        pf = path_filters if isinstance(path_filters, PathFilters) else PathFilters.model_validate(path_filters)

    kept: list[tuple[int, int, DiffFile, str]] = []
    for orig_idx, df in enumerate(parsed_diff.files):
        if _is_noisy_file(df):
            notes.append(f"omitted generated/binary file {df.path}")
            continue
        if pf is not None:
            if pf.include and not any(_glob_matches(df.path, pat) for pat in pf.include):
                notes.append(f"excluded by path_filters.include {df.path}")
                continue
            if pf.exclude and any(_glob_matches(df.path, pat) for pat in pf.exclude):
                notes.append(f"excluded by path_filters.exclude {df.path}")
                continue
        text = df.render_numbered() if numbered else df.raw
        if text and not text.endswith("\n"):
            text += "\n"
        kept.append((file_risk_score(df), orig_idx, df, text))

    if not kept:
        if pf is not None and any("path_filters" in n for n in notes):
            return [], notes
        # Lockfile/binary-only PR: return truncated raw snippet just like prepare_diff
        first = parsed_diff.files[0]
        raw_cut = (parsed_diff.raw or first.raw)[:budget]
        notes.append(f"diff contains only generated/binary files; truncated to {budget} chars")
        return [(first, raw_cut)], notes

    # Fast path: everything fits within budget -> return in priority order
    # (highest risk first, preserving original order within the same risk score)
    kept.sort(key=lambda item: (-item[0], item[1]))
    total_len = sum(len(text) for _, _, _, text in kept)
    if total_len <= budget:
        return [(df, text) for _, _, df, text in kept], notes

    # Over budget: allocate tier by tier (highest risk score first).
    # Within each tier, smallest files go first so one huge file does not starve siblings.
    tiers: dict[int, list[tuple[int, DiffFile, str]]] = {}
    for score, orig_idx, df, text in kept:
        tiers.setdefault(score, []).append((orig_idx, df, text))

    selected: list[tuple[DiffFile, str]] = []
    remaining = budget

    for score in sorted(tiers.keys(), reverse=True):
        tier_items = tiers[score]
        if remaining < _MIN_FILE_SLICE:
            for _, df, _ in tier_items:
                notes.append(f"omitted lower-priority file {df.path} (budget exhausted)")
            continue

        # Fair-share within this risk tier (smallest files first)
        tier_by_size = sorted(tier_items, key=lambda it: (len(it[2]), it[0]))
        allocated: dict[int, tuple[DiffFile, str]] = {}
        tier_n = len(tier_by_size)
        for i, (orig_idx, df, text) in enumerate(tier_by_size):
            if remaining < _MIN_FILE_SLICE:
                notes.append(f"omitted lower-priority file {df.path} (budget exhausted)")
                continue
            share = remaining // (tier_n - i)
            if len(text) <= share:
                allocated[orig_idx] = (df, text)
                remaining -= len(text)
            elif len(text) <= remaining and i == tier_n - 1:
                allocated[orig_idx] = (df, text)
                remaining -= len(text)
            else:
                cap = max(_MIN_FILE_SLICE, share)
                cap = min(cap, remaining)
                cut = max(0, cap - len(_TRUNC_MARK))
                sliced = text[:cut].rstrip() + _TRUNC_MARK
                if len(sliced) > remaining:
                    sliced = text[:remaining]
                allocated[orig_idx] = (df, sliced)
                remaining -= len(sliced)
                notes.append(f"truncated {df.path} ({len(text)} -> {len(sliced)} chars)")

        # Preserve original order within the same risk tier
        for orig_idx, _, _ in tier_items:
            if orig_idx in allocated:
                selected.append(allocated[orig_idx])

    return selected, notes


def budget_diff_by_risk(
    parsed_diff: ParsedDiff,
    budget_chars: int,
    *,
    path_filters: PathFilters | dict[str, Any] | None = None,
    numbered: bool = False,
) -> tuple[str, list[str]]:
    """Prioritize higher-risk source/security files over tests/docs, drop lockfiles/binaries,
    and enforce `budget_chars` cleanly. Returns `(diff_text, notes)`."""
    selected, notes = select_budgeted_files(
        parsed_diff,
        budget_chars,
        path_filters=path_filters,
        numbered=numbered,
    )
    text = "".join(chunk for _, chunk in selected)
    if len(text) > budget_chars:
        text = text[:budget_chars]
    return text, notes
