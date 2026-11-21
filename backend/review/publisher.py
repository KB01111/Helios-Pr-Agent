"""Structured PR review publisher, Redis finding lifecycle store, and top-level orchestrator.

Persists findings under `finding:{repo}:{pr}:{fingerprint}` (TTL 60d) with a
per-PR index at `findings:{repo}:{pr}` and incremental review bookmark at
`prrev:{repo}:{pr}`. Publishes ONE GitHub PR review with inline committable
suggestion blocks (with HTTP 422 fallback into the sticky summary comment) plus
ONE upserted sticky summary comment carrying walkthrough, effort badge, Mermaid
diagram, findings table, and applied repo learnings.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import httpx

try:  # cwd=backend / PYTHONPATH=backend
    import datamodel as dm
    import github_app
    import store
    from sandboxes.providers_review import (
        GITHUB_API,
        _find_previous_review,
        _head_moved,
        diff_budget,
        review_marker,
    )
    from sandboxes.safety import UnsafeSandboxInput, safe_repo
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]
    from backend import github_app  # type: ignore[no-redef]
    from backend import store  # type: ignore[no-redef]
    from backend.sandboxes.providers_review import (  # type: ignore[no-redef]
        GITHUB_API,
        _find_previous_review,
        _head_moved,
        diff_budget,
        review_marker,
    )
    from backend.sandboxes.safety import UnsafeSandboxInput, safe_repo  # type: ignore[no-redef]

from .config import (
    RepoConfig,
    load_repo_config,
    merge_agent_and_repo_config,
    should_auto_review,
)
from .diff import (
    DiffFetchError,
    ParsedDiff,
    budget_diff_by_risk,
    fetch_incremental_diff,
    parse_unified_diff,
    select_budgeted_files,
)
from .pipeline import (
    LLMCallError,
    LLMCallable,
    generate_findings,
    generate_walkthrough,
    resolve_llm_runtime,
    verify_and_filter_findings,
)

log = logging.getLogger("pr-agent.review.publisher")

VALID_FINDING_STATES = frozenset({"open", "resolved", "dismissed", "outdated"})
_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "nit": 3}
_SEVERITY_BADGE = {
    "critical": "\U0001f534 **Critical**",
    "major": "\U0001f7e0 **Major**",
    "minor": "\U0001f7e1 **Minor**",
    "nit": "\U0001f535 **Nit**",
}
_EFFORT_LABEL = {
    1: "\U0001f7e2 1/5 (Trivial)",
    2: "\U0001f7e2 2/5 (Simple)",
    3: "\U0001f7e1 3/5 (Moderate)",
    4: "\U0001f7e0 4/5 (Complex)",
    5: "\U0001f534 5/5 (High-risk)",
}
_FINDING_MARKER_RE = re.compile(r"<!--\s*pr-agent-finding:([0-9a-f]{16,64})\s*-->")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_repo(repo: str) -> str:
    if not dm.valid_repo(repo):
        raise ValueError("invalid repository name")
    return repo.lower()


# ---------------------------------------------------------------------------
# Redis finding & incremental review persistence (TTL 60d)
# ---------------------------------------------------------------------------


def findings_index_key(repo: str, pr_number: int) -> str:
    return f"findings:{_norm_repo(repo)}:{int(pr_number)}"


def pr_last_reviewed_key(repo: str, pr_number: int) -> str:
    return f"prrev:{_norm_repo(repo)}:{int(pr_number)}"


def get_last_reviewed_sha(repo: str, pr_number: int) -> str | None:
    if not dm.valid_repo(repo) or int(pr_number) <= 0:
        return None
    raw = store.rget(pr_last_reviewed_key(repo, pr_number))
    if not raw:
        return None
    val = raw.strip()
    return val or None


_UNSET = object()


def _pr_bookmark_lock_key(repo: str, pr_number: int) -> str:
    return f"prrevlock:{_norm_repo(repo)}:{int(pr_number)}"


def set_last_reviewed_sha(
    repo: str,
    pr_number: int,
    head_sha: str,
    *,
    expected_sha: Any = _UNSET,
) -> bool:
    sha = (head_sha or "").strip()
    if not sha or not dm.valid_repo(repo) or int(pr_number) <= 0:
        return False
    with store.key_lock(_pr_bookmark_lock_key(repo, pr_number)):
        if expected_sha is not _UNSET:
            cur = get_last_reviewed_sha(repo, pr_number)
            if (cur or "").lower() != str(expected_sha or "").strip().lower():
                return False
        store.rset(pr_last_reviewed_key(repo, pr_number), sha, ex=dm.FINDING_TTL_S)
        return True


def _read_index_fps(repo: str, pr_number: int) -> list[str]:
    raw = store.rget_json(findings_index_key(repo, pr_number))
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for fp in raw:
        s = str(fp or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _write_index_fps(repo: str, pr_number: int, fps: Sequence[str]) -> None:
    deduped = list(dict.fromkeys(str(fp).strip() for fp in fps if str(fp).strip()))
    store.rset_json(findings_index_key(repo, pr_number), deduped, ex=dm.FINDING_TTL_S)


def _finding_lock_key(repo: str, pr_number: int, fingerprint: str) -> str:
    return f"findinglock:{_norm_repo(repo)}:{int(pr_number)}:{fingerprint}"


def save_pr_findings(
    repo: str,
    pr_number: int,
    findings: Sequence[dict[str, Any]],
    *,
    head_sha: str = "",
) -> list[dict[str, Any]]:
    """Persist findings under `datamodel.finding_key` and update `findings:{repo}:{pr}` (TTL 60d)."""
    if not findings:
        return []
    existing_fps = _read_index_fps(repo, pr_number)
    now = _now_iso()
    saved: list[dict[str, Any]] = []

    for item in findings:
        if not isinstance(item, dict):
            continue
        fp = str(item.get("fingerprint") or "").strip()
        if not fp:
            fp = dm.finding_fingerprint(
                str(item.get("file") or ""),
                "",
                str(item.get("category") or "maintainability"),
                str(item.get("title") or ""),
            )
        fkey = dm.finding_key(repo, pr_number, fp)
        with store.key_lock(_finding_lock_key(repo, pr_number, fp)):
            prev = store.rget_json(fkey)
            prev_dict = prev if isinstance(prev, dict) else {}
            prev_state = str(prev_dict.get("state") or "").lower()
            item_state = str(item.get("state") or "").lower()
            # Never silently resurrect a concurrently dismissed/resolved finding via a generic save.
            if prev_state in ("dismissed", "resolved") and item_state in ("", "open"):
                effective_state = prev_state
            else:
                effective_state = item_state or prev_state or "open"
            rec = {
                **prev_dict,
                **item,
                "fingerprint": fp,
                "repo": repo,
                "pr_number": int(pr_number),
                "state": effective_state,
                "head_sha": head_sha or item.get("head_sha") or prev_dict.get("head_sha") or "",
                "created_at": prev_dict.get("created_at") or item.get("created_at") or now,
                "updated_at": now,
            }
            store.rset_json(fkey, rec, ex=dm.FINDING_TTL_S)
        if fp not in existing_fps:
            existing_fps.append(fp)
        saved.append(rec)

    _write_index_fps(repo, pr_number, existing_fps)
    return saved


def _attach_finding_comment_meta(
    repo: str,
    pr_number: int,
    fingerprint: str,
    *,
    comment_id: int | None = None,
    url: str | None = None,
) -> dict[str, Any] | None:
    """Atomically attach `comment_id` and `url` to an existing finding without touching `state`."""
    if not fingerprint or not dm.valid_repo(repo) or int(pr_number) <= 0:
        return None
    fkey = dm.finding_key(repo, pr_number, fingerprint)
    with store.key_lock(_finding_lock_key(repo, pr_number, fingerprint)):
        existing = store.rget_json(fkey)
        if not isinstance(existing, dict):
            return None
        rec = dict(existing)
        if comment_id is not None:
            rec["comment_id"] = int(comment_id)
        if url:
            rec["url"] = str(url)
        rec["delivered"] = True
        rec["updated_at"] = _now_iso()
        store.rset_json(fkey, rec, ex=dm.FINDING_TTL_S)
        return rec


def list_pr_findings(
    repo: str,
    pr_number: int,
    *,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored findings for `(repo, pr_number)` from Redis (optionally filtered by state)."""
    if not dm.valid_repo(repo) or int(pr_number) <= 0:
        return []
    norm = _norm_repo(repo)
    pr_int = int(pr_number)

    keys_set: list[str] = []
    seen_keys: set[str] = set()
    for fp in _read_index_fps(repo, pr_int):
        try:
            k = dm.finding_key(repo, pr_int, fp)
        except ValueError:
            continue
        if k not in seen_keys:
            seen_keys.add(k)
            keys_set.append(k)

    # Also merge any findings stored directly at finding:{repo}:{pr}:* without index update
    prefix = f"finding:{norm}:{pr_int}:"
    for k in store.rkeys(f"{prefix}*"):
        if k not in seen_keys:
            seen_keys.add(k)
            keys_set.append(k)

    if not keys_set:
        return []

    rows = store.rget_json_many(keys_set)
    out: list[dict[str, Any]] = []
    for key, val in zip(keys_set, rows):
        if not isinstance(val, dict):
            continue
        rec = dict(val)
        if not rec.get("fingerprint"):
            rec["fingerprint"] = key.rsplit(":", 1)[-1]
        if state is not None and str(rec.get("state") or "open").lower() != state.lower():
            continue
        out.append(rec)

    out.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(str(f.get("severity") or "minor"), 9),
            str(f.get("file") or ""),
            int(f.get("start_line") or 0),
        )
    )
    return out


def set_finding_state(
    repo: str,
    pr_number: int,
    fingerprint: str,
    state: str,
    *,
    reason: str | None = None,
    by: str | None = None,
    expected_states: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """Atomically update lifecycle state (`open` | `resolved` | `dismissed` | `outdated`) of one finding.

    When ``expected_states`` is supplied, the transition is applied only if the
    latest stored state is in ``expected_states`` (returning ``None`` otherwise),
    preserving concurrent dismissals or outdated transitions.
    """
    norm_state = (state or "").strip().lower()
    if norm_state not in VALID_FINDING_STATES:
        raise ValueError(f"invalid finding state: {state!r}")
    allowed = {s.strip().lower() for s in expected_states} if expected_states is not None else None
    fkey = dm.finding_key(repo, pr_number, fingerprint)
    with store.key_lock(_finding_lock_key(repo, pr_number, fingerprint)):
        existing = store.rget_json(fkey)
        if not isinstance(existing, dict):
            return None
        cur_state = str(existing.get("state") or "open").strip().lower()
        if allowed is not None and cur_state not in allowed:
            return None
        rec = dict(existing)
        rec["fingerprint"] = fingerprint
        rec["state"] = norm_state
        rec["updated_at"] = _now_iso()
        if reason is not None:
            rec["state_reason"] = str(reason)[:500]
        if by is not None:
            rec["state_by"] = str(by)[:80]
        store.rset_json(fkey, rec, ex=dm.FINDING_TTL_S)
        return rec


def stage_finding_updates(
    existing_findings: Sequence[dict[str, Any]],
    delta_diff: ParsedDiff | None,
    *,
    pr_diff: ParsedDiff | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, dict[str, Any]]]:
    """Compute finding invalidations and surviving line-shift/rename remappings in memory.

    Returns ``(staged_findings, outdated_fps, remapped_spans)`` without mutating
    Redis so a discarded/failed review never prematurely marks findings outdated.
    """
    if (delta_diff is None or not delta_diff.files) and pr_diff is None:
        return [dict(f) for f in existing_findings], [], {}

    staged: list[dict[str, Any]] = []
    outdated_fps: list[str] = []
    remapped_spans: dict[str, dict[str, Any]] = {}

    for finding in existing_findings:
        rec = dict(finding)
        if str(rec.get("state") or "open").lower() != "open":
            staged.append(rec)
            continue
        fpath = str(rec.get("file") or "")
        try:
            lo = int(rec.get("start_line") or 1)
            hi = int(rec.get("end_line") or lo)
        except (TypeError, ValueError):
            lo, hi = 1, 1
        side = str(rec.get("side") or "RIGHT").upper()

        df = delta_diff.get_file(fpath, exact=True) if delta_diff is not None else None
        renamed = False
        if df is None and delta_diff is not None:
            df = delta_diff.get_file_by_old_path(fpath)
            renamed = df is not None

        if side == "LEFT":
            new_file = df.path if renamed and df is not None else fpath
            if pr_diff is not None:
                pr_df = pr_diff.get_file(new_file, exact=True)
                if pr_df is None:
                    pr_df = pr_diff.get_file_by_old_path(fpath)
                if pr_df is None:
                    # File no longer differs from merge base -> deleted line was restored.
                    was_mod, new_lo, new_hi = True, lo, hi
                else:
                    was_mod, new_lo, new_hi = pr_df.span_modified_or_remapped(
                        lo, hi, "LEFT", pr_file=pr_df
                    )
                    if not renamed and pr_df.path != fpath:
                        new_file = pr_df.path
            else:
                was_mod, new_lo, new_hi = False, lo, hi
        else:
            if df is None:
                staged.append(rec)
                continue
            was_mod, new_lo, new_hi = df.span_modified_or_remapped(lo, hi, side)
            new_file = df.path if renamed else fpath

        fp = str(rec.get("fingerprint") or "").strip()
        if was_mod:
            rec["state"] = "outdated"
            if fp:
                outdated_fps.append(fp)
        elif (new_file, new_lo, new_hi) != (fpath, lo, hi):
            rec["file"] = new_file
            rec["start_line"] = new_lo
            rec["end_line"] = new_hi
            if fp:
                remapped_spans[fp] = {
                    "file": new_file,
                    "start_line": new_lo,
                    "end_line": new_hi,
                    "expected_file": fpath,
                    "expected_start_line": lo,
                    "expected_end_line": hi,
                    "expected_head_sha": str(rec.get("head_sha") or ""),
                }
        staged.append(rec)

    return staged, outdated_fps, remapped_spans


def commit_staged_finding_updates(
    repo: str,
    pr_number: int,
    outdated_fps: Sequence[str],
    remapped_spans: dict[str, Any],
    *,
    head_sha: str = "",
) -> list[str]:
    """Commit staged outdated transitions and line-shift/rename remappings to Redis."""
    committed_outdated: list[str] = []
    for fp in outdated_fps:
        if set_finding_state(repo, pr_number, fp, "outdated", expected_states=("open",)) is not None:
            committed_outdated.append(fp)
    for fp, remap_info in remapped_spans.items():
        if not fp or not dm.valid_repo(repo) or int(pr_number) <= 0:
            continue
        fkey = dm.finding_key(repo, pr_number, fp)
        with store.key_lock(_finding_lock_key(repo, pr_number, fp)):
            existing = store.rget_json(fkey)
            if not isinstance(existing, dict) or str(existing.get("state") or "open").lower() != "open":
                continue
            rec = dict(existing)
            if isinstance(remap_info, dict):
                if (
                    str(existing.get("file") or "") != str(remap_info.get("expected_file") or "")
                    or int(existing.get("start_line") or 1) != int(remap_info.get("expected_start_line") or 1)
                    or int(existing.get("end_line") or 1) != int(remap_info.get("expected_end_line") or 1)
                    or str(existing.get("head_sha") or "").lower()
                    != str(remap_info.get("expected_head_sha") or "").lower()
                ):
                    continue
                rec["file"] = str(remap_info["file"])
                rec["start_line"] = int(remap_info["start_line"])
                rec["end_line"] = int(remap_info["end_line"])
            else:
                new_lo, new_hi = remap_info
                rec["start_line"] = int(new_lo)
                rec["end_line"] = int(new_hi)
            if head_sha:
                rec["head_sha"] = head_sha
            rec["updated_at"] = _now_iso()
            store.rset_json(fkey, rec, ex=dm.FINDING_TTL_S)
    return committed_outdated


def mark_outdated_findings(
    repo: str,
    pr_number: int,
    changed_files_and_lines: ParsedDiff | dict[str, set[int] | list[int]] | Iterable[str],
) -> list[str]:
    """Mark previously open findings `outdated` when their lines were modified in a new push.

    When passed a ``ParsedDiff``, also remaps surviving open findings whose line
    numbers shifted due to insertions/deletions above them.
    """
    if not changed_files_and_lines:
        return []

    if isinstance(changed_files_and_lines, ParsedDiff):
        _, outdated_fps, remapped = stage_finding_updates(
            list_pr_findings(repo, pr_number, state="open"), changed_files_and_lines
        )
        return commit_staged_finding_updates(repo, pr_number, outdated_fps, remapped)

    if isinstance(changed_files_and_lines, dict):
        changed_map: dict[str, set[int]] = {
            str(path).lstrip("./"): {int(ln) for ln in (lines or ())}
            for path, lines in changed_files_and_lines.items()
        }
    else:
        changed_map = {str(path).lstrip("./"): set() for path in changed_files_and_lines}

    outdated_fps: list[str] = []
    for finding in list_pr_findings(repo, pr_number, state="open"):
        fpath = str(finding.get("file") or "").lstrip("./")
        if fpath not in changed_map:
            continue
        touched_lines = changed_map[fpath]
        try:
            lo = int(finding.get("start_line") or 1)
            hi = int(finding.get("end_line") or lo)
        except (TypeError, ValueError):
            lo, hi = 1, 1
        span = set(range(min(lo, hi), max(lo, hi) + 1))
        # Empty line set signals whole-file/deleted/binary change -> mark outdated
        if not touched_lines or bool(span & touched_lines):
            fp = str(finding.get("fingerprint") or "")
            if fp and set_finding_state(repo, pr_number, fp, "outdated", expected_states=("open",)) is not None:
                outdated_fps.append(fp)
    return outdated_fps


def find_finding_by_comment_id(
    repo: str,
    pr_number: int,
    comment_id: int,
) -> dict[str, Any] | None:
    """Resolve a stored finding from its GitHub inline review comment ID."""
    target = int(comment_id or 0)
    if target <= 0:
        return None
    for finding in list_pr_findings(repo, pr_number):
        try:
            if int(finding.get("comment_id") or 0) == target:
                return finding
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Comment & review Markdown rendering
# ---------------------------------------------------------------------------


def _suggestion_fence(patch: str) -> str:
    # Use 4 backticks if the replacement code itself contains triple backticks
    fence = "````" if "```" in patch else "```"
    return f"{fence}suggestion\n{patch}\n{fence}"


def format_inline_comment(finding: dict[str, Any]) -> str:
    """Format one inline GitHub review comment with severity badge, committable
    suggestion block, and repo learning citations."""
    sev = str(finding.get("severity") or "minor").lower()
    badge = _SEVERITY_BADGE.get(sev, "**Minor**")
    category = str(finding.get("category") or "maintainability")
    title = str(finding.get("title") or "Review finding").strip()
    body = str(finding.get("body") or "").strip()
    patch = finding.get("suggestion_patch")
    learning_ids = [str(lid).strip().lstrip("#") for lid in (finding.get("learning_ids") or []) if str(lid).strip()]
    fp = str(finding.get("fingerprint") or "").strip()

    parts: list[str] = [f"{badge} · `{category}` — **{title}**"]
    if body:
        parts.append(body)
    if isinstance(patch, str) and patch.strip():
        parts.append(_suggestion_fence(patch))
    if learning_ids:
        citations = " · ".join(f"_Based on repo learning #{lid}_" for lid in learning_ids)
        parts.append(citations)
    if fp:
        parts.append(f"<!-- pr-agent-finding:{fp} -->")
    parts.append("<!-- pr-agent-control:finding -->")
    return "\n\n".join(parts)


def _escape_md_cell(value: str) -> str:
    return " ".join(str(value or "").replace("|", "\\|").split())


def render_summary_comment(
    walkthrough: dict[str, Any],
    findings: Sequence[dict[str, Any]],
    *,
    agent_key: str,
    agent_name: str,
    head_sha: str = "",
    incremental_from: str | None = None,
    applied_learnings: Sequence[dict[str, Any] | str] | None = None,
    fallback_findings: Sequence[dict[str, Any]] | None = None,
    cut_notes: Sequence[str] | None = None,
) -> str:
    """Render the sticky PR summary issue comment (with marker, walkthrough, file table,
    effort badge, Mermaid diagram, findings table, applied learnings, and 422 fallback block)."""
    marker = review_marker(agent_key)
    effort_val = int(walkthrough.get("effort") or 2)
    effort_badge = _EFFORT_LABEL.get(max(1, min(5, effort_val)), _EFFORT_LABEL[2])

    counts = {"critical": 0, "major": 0, "minor": 0, "nit": 0}
    for f in findings:
        sev = str(f.get("severity") or "minor").lower()
        if sev in counts:
            counts[sev] += 1

    meta_bits = [f"**Effort:** {effort_badge}"]
    if incremental_from and head_sha:
        meta_bits.append(f"**Scope:** incremental `{incremental_from[:7]}..{head_sha[:7]}`")
    elif head_sha:
        meta_bits.append(f"**Commit:** `{head_sha[:7]}`")
    meta_bits.append(
        f"**Findings:** {len(findings)} "
        f"({counts['critical']} critical, {counts['major']} major, {counts['minor']} minor, {counts['nit']} nit)"
    )

    sections: list[str] = [
        marker,
        f"## \U0001f916 {agent_name} — Code Review",
        " · ".join(meta_bits),
    ]

    summary = str(walkthrough.get("summary") or "").strip()
    if summary:
        sections.append(f"### Walkthrough\n{summary}")

    file_table = walkthrough.get("file_table") or []
    if isinstance(file_table, list) and file_table:
        rows = ["| File | Summary |", "|---|---|"]
        for row in file_table[:40]:
            if not isinstance(row, dict):
                continue
            fp = _escape_md_cell(str(row.get("file") or ""))
            cs = _escape_md_cell(str(row.get("change_summary") or ""))
            if fp:
                rows.append(f"| `{fp}` | {cs} |")
        if len(rows) > 2:
            sections.append("### Changes by file\n" + "\n".join(rows))

    diagram = walkthrough.get("sequence_diagram")
    if isinstance(diagram, str) and diagram.strip():
        sections.append(f"### Sequence diagram\n```mermaid\n{diagram.strip()}\n```")

    if findings:
        rows = ["| Severity | Category | Location | Issue |", "|---|---|---|---|"]
        for f in findings:
            sev = str(f.get("severity") or "minor").lower()
            badge = _SEVERITY_BADGE.get(sev, sev)
            cat = f"`{_escape_md_cell(str(f.get('category') or 'maintainability'))}`"
            s_ln = int(f.get("start_line") or 1)
            e_ln = int(f.get("end_line") or s_ln)
            loc_range = f"{s_ln}-{e_ln}" if e_ln > s_ln else f"{s_ln}"
            loc = f"`{_escape_md_cell(str(f.get('file') or ''))}:{loc_range}`"
            title = _escape_md_cell(str(f.get("title") or ""))
            url = str(f.get("url") or "").strip()
            title_cell = f"[{title}]({url})" if url else title
            rows.append(f"| {badge} | {cat} | {loc} | {title_cell} |")
        sections.append("### Findings\n" + "\n".join(rows))
    else:
        sections.append("### Findings\nNo actionable issues found in this review.")

    if fallback_findings:
        fb_blocks: list[str] = [
            "### Unplaced inline findings",
            "_GitHub could not attach these findings inline on the diff; included here in full so nothing is missed._",
        ]
        for f in fallback_findings:
            s_ln = int(f.get("start_line") or 1)
            e_ln = int(f.get("end_line") or s_ln)
            loc_range = f"{s_ln}-{e_ln}" if e_ln > s_ln else f"{s_ln}"
            header = f"#### `{f.get('file')}:{loc_range}` ({f.get('side', 'RIGHT')})"
            fb_blocks.append(f"{header}\n\n{format_inline_comment(f)}")
        sections.append("\n\n".join(fb_blocks))

    if applied_learnings:
        mem_lines: list[str] = []
        for entry in applied_learnings:
            if isinstance(entry, dict):
                mid = str(entry.get("id") or "").strip().lstrip("#")
                text = str(entry.get("text") or "").strip()
                if text:
                    mem_lines.append(f"- **#{mid}**: {text}" if mid else f"- {text}")
            elif isinstance(entry, str) and entry.strip():
                mem_lines.append(f"- {entry.strip()}")
        if mem_lines:
            sections.append("### Applied repo learnings\n" + "\n".join(mem_lines))

    if cut_notes:
        note_lines = "\n".join(f"- {n}" for n in cut_notes[:15])
        sections.append(
            "<details>\n<summary>Diff budget & scope notes</summary>\n\n"
            f"{note_lines}\n</details>"
        )

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# GitHub publisher (PR review + inline comments + sticky summary comment)
# ---------------------------------------------------------------------------


def _build_review_comment_payload(
    finding: dict[str, Any],
    *,
    allow_multiline: bool = True,
) -> dict[str, Any]:
    start_line = int(finding.get("start_line") or 1)
    end_line = int(finding.get("end_line") or start_line)
    side = str(finding.get("side") or "RIGHT").upper()
    if side not in ("LEFT", "RIGHT"):
        side = "RIGHT"
    # If a multiline span is collapsed to a single-line retry, drop its suggestion
    # patch so applying it cannot partially replace only end_line.
    render_target = (
        {**finding, "suggestion_patch": None}
        if (not allow_multiline and start_line < end_line and finding.get("suggestion_patch"))
        else finding
    )
    payload: dict[str, Any] = {
        "path": str(finding["file"]),
        "line": end_line,
        "side": side,
        "body": format_inline_comment(render_target),
    }
    if allow_multiline and start_line < end_line:
        payload["start_line"] = start_line
        payload["start_side"] = side
    return payload


def _match_posted_comments_to_findings(
    repo: str,
    pr_number: int,
    findings: list[dict[str, Any]],
    posted_comments: Sequence[dict[str, Any]],
    *,
    persist: bool = True,
) -> None:
    """Attach GitHub `comment_id` and `url` to findings without overwriting lifecycle `state`."""
    by_fp = {str(f.get("fingerprint") or ""): f for f in findings if f.get("fingerprint")}
    by_loc: dict[tuple[str, int, str], dict[str, Any]] = {}
    for f in findings:
        key = (str(f.get("file") or ""), int(f.get("end_line") or f.get("start_line") or 0), str(f.get("side") or "RIGHT"))
        by_loc.setdefault(key, f)

    updated: list[dict[str, Any]] = []
    for c in posted_comments:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid is None:
            continue
        body = str(c.get("body") or "")
        target: dict[str, Any] | None = None
        m = _FINDING_MARKER_RE.search(body)
        if m and m.group(1) in by_fp:
            target = by_fp[m.group(1)]
        else:
            cpath = str(c.get("path") or "")
            cline = int(c.get("line") or c.get("original_line") or 0)
            cside = str(c.get("side") or "RIGHT").upper()
            target = by_loc.get((cpath, cline, cside))
        if target is None:
            continue
        target["comment_id"] = int(cid)
        if c.get("html_url"):
            target["url"] = str(c["html_url"])
        updated.append(target)

    if persist and updated and dm.valid_repo(repo) and int(pr_number) > 0:
        for item in updated:
            fp = str(item.get("fingerprint") or "").strip()
            if not fp:
                continue
            try:
                _attach_finding_comment_meta(
                    repo,
                    pr_number,
                    fp,
                    comment_id=item.get("comment_id"),
                    url=item.get("url"),
                )
            except Exception as exc:
                log.warning("review.publisher: failed saving comment_id for %s#%s (%s)", repo, pr_number, exc)


async def publish_review(
    repo: str,
    pr_number: int,
    head_sha: str,
    walkthrough: dict[str, Any],
    findings: Sequence[dict[str, Any]],
    *,
    agent_key: str,
    agent_name: str,
    github_token: str,
    incremental_from: str | None = None,
    applied_learnings: Sequence[dict[str, Any] | str] | None = None,
    cut_notes: Sequence[str] | None = None,
    all_open_findings: Sequence[dict[str, Any]] | None = None,
    persist_comment_ids: bool = True,
    allow_inline: bool = True,
    client: Any = None,
) -> dict[str, Any]:
    """Publish ONE GitHub PR review with inline comments + ONE upserted sticky summary comment.

    If GitHub rejects inline comments with HTTP 422:
    1. If any comment used a multi-line span (`start_line < end_line`), retries once
       collapsing comments to single-line positions.
    2. If GitHub still returns HTTP 422, moves unplaceable findings into the sticky
       summary comment so reviews never fail silently.
    """
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    pr_int = int(pr_number)
    findings_list = [dict(f) for f in findings]
    review_posted = False
    review_id: int | None = None
    inline_count = 0
    fallback_findings: list[dict[str, Any]] = []
    pr_comment_posted = False
    summary_comment_id: int | None = None
    summary_error: str | None = None

    own = client is None
    http_client = client or httpx.AsyncClient(timeout=30.0)
    try:
        # 1. Post ONE PR review with inline comments when there are new findings
        if findings_list and not allow_inline:
            fallback_findings = list(findings_list)
        elif findings_list:
            reviews_url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_int}/reviews"
            has_multiline = any(
                int(f.get("start_line") or 1) < int(f.get("end_line") or 1) for f in findings_list
            )
            inline_comments = [
                _build_review_comment_payload(f, allow_multiline=True) for f in findings_list
            ]
            review_payload: dict[str, Any] = {
                "event": "COMMENT",
                "body": f"\U0001f916 {agent_name}: {len(inline_comments)} inline review comment(s).",
                "comments": inline_comments,
            }
            if head_sha:
                review_payload["commit_id"] = head_sha

            resp = await http_client.post(reviews_url, headers=headers, json=review_payload)

            # Retry once as single-line comments if multi-line triggered 422
            if resp.status_code == 422 and has_multiline:
                log.info("review.publisher: HTTP 422 on multi-line review for %s#%s; retrying single-line", repo, pr_int)
                single_comments = [
                    _build_review_comment_payload(f, allow_multiline=False) for f in findings_list
                ]
                retry_payload = {**review_payload, "comments": single_comments}
                resp = await http_client.post(reviews_url, headers=headers, json=retry_payload)

            if resp.status_code in (200, 201):
                review_posted = True
                inline_count = len(findings_list)
                data = resp.json() if hasattr(resp, "json") else {}
                if isinstance(data, dict):
                    if data.get("id") is not None:
                        review_id = int(data["id"])
                    # Capture inline comment IDs directly from response or review comments endpoint
                    posted_comments = data.get("comments") if isinstance(data.get("comments"), list) else None
                    if posted_comments is None and review_id is not None:
                        try:
                            cresp = await http_client.get(
                                f"{reviews_url}/{review_id}/comments",
                                headers=headers,
                                params={"per_page": 100},
                            )
                            if cresp.status_code == 200:
                                parsed_c = cresp.json()
                                if isinstance(parsed_c, list):
                                    posted_comments = parsed_c
                        except Exception as exc:
                            log.warning("review.publisher: failed fetching inline comment IDs (%s)", type(exc).__name__)
                    if posted_comments:
                        _match_posted_comments_to_findings(
                            repo, pr_int, findings_list, posted_comments, persist=persist_comment_ids
                        )
            elif resp.status_code == 422:
                log.warning(
                    "review.publisher: GitHub rejected inline comments with HTTP 422 for %s#%s; moving %d finding(s) to sticky summary",
                    repo,
                    pr_int,
                    len(findings_list),
                )
                fallback_findings = list(findings_list)
            else:
                log.warning(
                    "review.publisher: PR review post returned HTTP %s for %s#%s; moving findings to sticky summary",
                    resp.status_code,
                    repo,
                    pr_int,
                )
                fallback_findings = list(findings_list)

        # 2. Upsert ONE sticky summary issue comment
        summary_findings = list(all_open_findings) if all_open_findings is not None else findings_list
        # Propagate any updated urls/comment_ids from findings_list into summary_findings
        if all_open_findings is not None and findings_list:
            updated_map = {str(f.get("fingerprint") or ""): f for f in findings_list if f.get("fingerprint")}
            summary_findings = [
                {**sf, **updated_map[str(sf.get("fingerprint") or "")]}
                if str(sf.get("fingerprint") or "") in updated_map
                else dict(sf)
                for sf in summary_findings
            ]

        summary_body = render_summary_comment(
            walkthrough,
            summary_findings,
            agent_key=agent_key,
            agent_name=agent_name,
            head_sha=head_sha,
            incremental_from=incremental_from,
            applied_learnings=applied_learnings,
            fallback_findings=fallback_findings,
            cut_notes=cut_notes,
        )
        marker = review_marker(agent_key)
        comments_url = f"{GITHUB_API}/repos/{repo}/issues/{pr_int}/comments"
        try:
            existing_id = await _find_previous_review(http_client, comments_url, headers, marker)
            if existing_id:
                sresp = await http_client.patch(
                    f"{GITHUB_API}/repos/{repo}/issues/comments/{existing_id}",
                    headers=headers,
                    json={"body": summary_body},
                )
                summary_comment_id = existing_id
            else:
                sresp = await http_client.post(
                    comments_url,
                    headers=headers,
                    json={"body": summary_body},
                )
                if sresp.status_code in (200, 201):
                    sdata = sresp.json() if hasattr(sresp, "json") else {}
                    if isinstance(sdata, dict) and sdata.get("id") is not None:
                        summary_comment_id = int(sdata["id"])
            pr_comment_posted = sresp.status_code in (200, 201)
        except Exception as exc:
            if not review_posted:
                raise
            summary_error = type(exc).__name__
            pr_comment_posted = False
            log.warning(
                "review.publisher: sticky summary upsert raised %s after inline review was accepted for %s#%s",
                summary_error,
                repo,
                pr_int,
            )
    finally:
        if own and hasattr(http_client, "aclose"):
            await http_client.aclose()

    result = {
        "review_posted": review_posted,
        "review_id": review_id,
        "pr_comment_posted": pr_comment_posted,
        "comment_id": summary_comment_id,
        "inline_count": inline_count,
        "fallback_count": len(fallback_findings),
        "inline_fallback": bool(fallback_findings),
        "findings": findings_list,
    }
    if summary_error is not None:
        result["summary_error"] = summary_error
    return result


# ---------------------------------------------------------------------------
# Top-level structured review orchestrator
# ---------------------------------------------------------------------------


def _review_result(
    *,
    status: str,
    logs: list[str],
    diff: str = "",
    pr_comment_posted: bool = False,
    review_posted: bool = False,
    walkthrough: dict[str, Any] | None = None,
    findings: list[dict[str, Any]] | None = None,
    new_findings: list[dict[str, Any]] | None = None,
    last_reviewed_sha: str | None = None,
    merged_config: dict[str, Any] | None = None,
    stale_head: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "provider": "review_only",
        "status": status,
        "logs": logs,
        "diff": diff,
        "cpu_seconds": 0.0,
        "pr_comment_posted": pr_comment_posted,
        "review_posted": review_posted,
        "walkthrough": walkthrough,
        "findings": findings if findings is not None else [],
        "new_findings": new_findings if new_findings is not None else [],
        "last_reviewed_sha": last_reviewed_sha,
    }
    if merged_config is not None:
        out["merged_config"] = merged_config
    if stale_head:
        out["stale_head"] = True
    return out


async def _verify_pr_head(
    client: Any,
    repo: str,
    pr_number: int,
    expected_sha: str,
    github_token: str,
) -> str:
    """Confirm the PR's current head SHA on GitHub before publishing.

    Returns ``"match"`` only when GitHub confirms ``head.sha == expected_sha``,
    ``"moved"`` when GitHub confirms a different head SHA, or ``"unknown"`` on
    lookup failure or missing SHA (so we never publish or advance bookmarks on
    an unverified head).
    """
    exp = (expected_sha or "").strip().lower()
    if not exp:
        return "unknown"
    try:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{int(pr_number)}",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if resp.status_code != 200:
            return "unknown"
        data = resp.json()
        if not isinstance(data, dict):
            return "unknown"
        head = data.get("head") if isinstance(data.get("head"), dict) else {}
        actual = str(head.get("sha") or "").strip().lower()
        if not actual:
            return "unknown"
        return "match" if actual == exp else "moved"
    except Exception:
        return "unknown"


def _collect_applied_learnings(
    memories: dict[str, Any] | None,
    new_findings: Sequence[dict[str, Any]],
) -> list[dict[str, Any] | str]:
    if not isinstance(memories, dict):
        return []
    rules = memories.get("rules") or []
    if not rules:
        return []
    cited_ids: set[str] = set()
    for f in new_findings:
        for lid in f.get("learning_ids") or []:
            cited_ids.add(str(lid).strip().lstrip("#"))

    if not cited_ids:
        return []
    applied: list[dict[str, Any] | str] = []
    seen: set[str] = set()
    for rule in rules:
        if isinstance(rule, dict):
            rid = str(rule.get("id") or "").strip().lstrip("#")
            if rid and rid in cited_ids and rid not in seen:
                seen.add(rid)
                applied.append(rule)
    return applied


async def _fetch_pr_metadata(
    client: Any,
    repo: str,
    pr_number: int,
    github_token: str,
) -> dict[str, Any]:
    try:
        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/pulls/{int(pr_number)}",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def run_structured_review(
    agent_config: dict[str, Any],
    pr_context: dict[str, Any],
    *,
    memories: dict[str, Any] | None = None,
    memories_provider: Any = None,
    force_full: bool = False,
    github_client: Any = None,
    llm_client: Any = None,
    llm_call: LLMCallable | None = None,
) -> dict[str, Any]:
    """End-to-end Phase 1 structured PR review orchestrator.

    1. Validates repo/PR/auth/LLM credentials.
    2. Loads `.pragent.yaml` from the PR's base branch only & merges with AgentConfig v2.
    3. Fetches incremental diff (`compare/{last}...{head}`) or full diff (with 406 fallback).
    4. Marks outdated findings on changed lines; budgets diff by risk & path_filters.
    5. Runs Pass A (walkthrough), Pass B (findings), Pass C (deterministic verification/dedupe).
    6. Persists findings in Redis, posts ONE inline PR review (with 422 fallback) +
       ONE sticky summary comment, and updates `last_reviewed_sha`.
    """
    logs: list[str] = []
    agent_name = str(agent_config.get("name") or agent_config.get("id") or "pr-agent")
    agent_key = str(agent_config.get("id") or agent_name)
    raw_repo = pr_context.get("repo", "")
    raw_pr = pr_context.get("pr_number")

    if not raw_repo or raw_pr is None:
        logs.append("review_only: pr_context missing repo/pr_number.")
        return _review_result(status="failed", logs=logs)
    try:
        repo = safe_repo(raw_repo)
        pr_number = int(raw_pr)
        if pr_number <= 0:
            raise ValueError
    except (UnsafeSandboxInput, ValueError, TypeError):
        logs.append("review_only: pr_context has an invalid repo/pr_number.")
        return _review_result(status="failed", logs=logs)

    # Check LLM key upfront unless a custom llm_call is injected (e.g. unit tests)
    if llm_call is None:
        _, _, llm_key, llm_key_env = resolve_llm_runtime()
        if not llm_key:
            logs.append(f"review_only: {llm_key_env} missing — cannot call LLM.")
            return _review_result(status="failed", logs=logs)

    # Resolve GitHub credentials
    github_token = str(pr_context.get("github_token") or "").strip()
    auth_kind = "token"
    if not github_token:
        if github_app.auth_mode() == "none":
            logs.append("review_only: GITHUB_TOKEN (or a GitHub App) missing — cannot fetch diff.")
            return _review_result(status="failed", logs=logs)
        github_token, auth_kind = await github_app.token_for_repo(repo)
        if not github_token:
            logs.append("review_only: no GitHub credentials for this repo — cannot fetch diff.")
            return _review_result(status="failed", logs=logs)
    logs.append(f"review_only: GitHub auth via {auth_kind}.")

    base_ref = str(pr_context.get("base_ref") or "").strip()
    base_sha = str(pr_context.get("base_sha") or "").strip()
    head_sha = str(pr_context.get("sha") or pr_context.get("head_sha") or "").strip()
    title = str(pr_context.get("title") or "").strip()
    description = str(pr_context.get("body") or pr_context.get("description") or "").strip()
    draft = bool(pr_context.get("draft", False))
    action = str(pr_context.get("action") or "").strip()

    own_gh = github_client is None
    gh = github_client or httpx.AsyncClient(timeout=30.0)
    try:
        # Best-effort metadata hydration if base_ref or head_sha missing
        if not base_ref or not head_sha:
            pr_meta_api = await _fetch_pr_metadata(gh, repo, pr_number, github_token)
            if pr_meta_api:
                base_obj = pr_meta_api.get("base") if isinstance(pr_meta_api.get("base"), dict) else {}
                head_obj = pr_meta_api.get("head") if isinstance(pr_meta_api.get("head"), dict) else {}
                base_ref = base_ref or str(base_obj.get("ref") or "").strip()
                base_sha = base_sha or str(base_obj.get("sha") or "").strip()
                head_sha = head_sha or str(head_obj.get("sha") or "").strip()
                title = title or str(pr_meta_api.get("title") or "").strip()
                description = description or str(pr_meta_api.get("body") or "").strip()
                if "draft" not in pr_context:
                    draft = bool(pr_meta_api.get("draft", False))

        if not head_sha:
            logs.append("review_only: missing PR head SHA after metadata hydration; aborting.")
            return _review_result(
                status="failed",
                logs=logs,
                last_reviewed_sha=get_last_reviewed_sha(repo, pr_number),
            )

        # 1. Load .pragent.yaml from base branch only & merge with AgentConfig v2
        if base_ref:
            repo_cfg = await load_repo_config(repo, base_ref, github_token, client=gh)
            logs.append(f"review_only: loaded repo config from base '{base_ref}' (profile={repo_cfg.profile}).")
        else:
            repo_cfg = RepoConfig()
        merged_cfg = merge_agent_and_repo_config(agent_config, repo_cfg)

        pr_meta = {
            "repo": repo,
            "pr_number": pr_number,
            "sha": head_sha,
            "base_ref": base_ref,
            "base_sha": base_sha,
            "title": title,
            "body": description,
            "draft": draft,
            "action": action,
        }

        if not force_full and action != "manual":
            allow, skip_reason = should_auto_review(pr_meta, merged_cfg)
            if not allow:
                logs.append(f"review_only: skipped ({skip_reason}).")
                return _review_result(
                    status="skipped",
                    logs=logs,
                    findings=list_pr_findings(repo, pr_number, state="open"),
                    last_reviewed_sha=get_last_reviewed_sha(repo, pr_number),
                )

        # 2. Fetch incremental or full diff
        prior_reviewed_sha = get_last_reviewed_sha(repo, pr_number)
        prev_sha = None if force_full else prior_reviewed_sha
        try:
            fetched = await fetch_incremental_diff(
                gh,
                repo,
                pr_number,
                base_sha,
                head_sha,
                prev_sha,
                github_token,
            )
        except DiffFetchError as exc:
            logs.append(f"review_only: {exc}.")
            return _review_result(status="failed", logs=logs, last_reviewed_sha=prior_reviewed_sha)
        except Exception as exc:
            logs.append(f"review_only: diff fetch error: {type(exc).__name__}.")
            return _review_result(status="failed", logs=logs, last_reviewed_sha=prior_reviewed_sha)

        logs.append(
            f"review_only: fetched {'incremental' if fetched.is_incremental else 'full'} diff ({len(fetched.diff)} chars)."
        )
        for note in fetched.notes:
            logs.append(f"review_only: {note}.")

        parsed_diff = parse_unified_diff(fetched.diff)

        # 3. Stage outdated invalidations & line-shift/rename remappings in memory using ONLY
        #    the delta from prior_reviewed_sha -> head_sha (never the full PR diff on a
        #    force_full re-review of the same head, and never mutating Redis before the
        #    review is accepted and published).
        raw_existing_findings = list_pr_findings(repo, pr_number)
        open_existing = [
            f for f in raw_existing_findings if str(f.get("state") or "open").lower() == "open"
        ]
        has_open_existing = bool(open_existing)
        has_open_left = any(str(f.get("side") or "RIGHT").upper() == "LEFT" for f in open_existing)
        invalidation_diff: ParsedDiff | None = None
        pr_diff_for_anchors: ParsedDiff | None = None if fetched.is_incremental else parsed_diff
        findings_reconciled = True

        if fetched.is_incremental and parsed_diff.files:
            invalidation_diff = parsed_diff
        elif (
            has_open_existing
            and not fetched.is_incremental
            and prior_reviewed_sha
            and head_sha
            and prior_reviewed_sha.lower() != head_sha.lower()
        ):
            try:
                delta_fetched = await fetch_incremental_diff(
                    gh,
                    repo,
                    pr_number,
                    base_sha,
                    head_sha,
                    prior_reviewed_sha,
                    github_token,
                )
                if delta_fetched.is_incremental:
                    if delta_fetched.diff.strip():
                        invalidation_diff = parse_unified_diff(delta_fetched.diff)
                else:
                    invalidation_diff = None
                    findings_reconciled = False
                    logs.append(
                        "review_only: incremental delta unavailable to reconcile existing findings; retaining prior reviewed SHA."
                    )
            except Exception:
                invalidation_diff = None
                findings_reconciled = False
                logs.append(
                    "review_only: failed fetching incremental delta to reconcile existing findings; retaining prior reviewed SHA."
                )

        if has_open_left and pr_diff_for_anchors is None:
            try:
                pr_left_fetched = await fetch_incremental_diff(
                    gh,
                    repo,
                    pr_number,
                    base_sha,
                    head_sha,
                    None,
                    github_token,
                )
                if pr_left_fetched.diff.strip():
                    pr_diff_for_anchors = parse_unified_diff(pr_left_fetched.diff)
                else:
                    findings_reconciled = False
            except Exception:
                findings_reconciled = False

        existing_findings, staged_outdated_fps, staged_remaps = stage_finding_updates(
            raw_existing_findings,
            invalidation_diff,
            pr_diff=pr_diff_for_anchors if has_open_left else None,
        )
        if staged_outdated_fps:
            logs.append(f"review_only: staged {len(staged_outdated_fps)} previous finding(s) as outdated.")

        # 4. Budget diff by risk & path_filters
        budget = diff_budget()
        path_filters = merged_cfg.get("path_filters")
        budgeted_raw, budget_notes = budget_diff_by_risk(
            parsed_diff, budget, path_filters=path_filters, numbered=False
        )
        numbered_selected, _ = select_budgeted_files(
            parsed_diff, budget, path_filters=path_filters, numbered=True
        )
        budgeted_numbered = "".join(chunk for _, chunk in numbered_selected)[:budget]

        cut_notes = list(fetched.notes) + list(budget_notes)
        for note in budget_notes[:10]:
            logs.append(f"review_only: {note}.")

        if not parsed_diff.files or not budgeted_raw.strip():
            logs.append("review_only: no reviewable diff lines after filtering.")
            if github_client is None:
                head_status = await _verify_pr_head(gh, repo, pr_number, head_sha, github_token)
                if head_status == "moved":
                    logs.append("review_only: PR head moved on while reviewing; skipping bookmark update.")
                    return _review_result(
                        status="completed_no_comment",
                        logs=logs,
                        diff=budgeted_raw,
                        findings=list_pr_findings(repo, pr_number, state="open"),
                        new_findings=[],
                        last_reviewed_sha=prior_reviewed_sha,
                        merged_config=merged_cfg,
                        stale_head=True,
                    )
                if head_status != "match":
                    logs.append("review_only: could not verify PR head SHA on GitHub; aborting bookmark update.")
                    return _review_result(
                        status="failed",
                        logs=logs,
                        diff=budgeted_raw,
                        findings=list_pr_findings(repo, pr_number, state="open"),
                        new_findings=[],
                        last_reviewed_sha=prior_reviewed_sha,
                        merged_config=merged_cfg,
                    )
            staged_committed = True
            if staged_outdated_fps or staged_remaps:
                try:
                    committed = commit_staged_finding_updates(
                        repo, pr_number, staged_outdated_fps, staged_remaps, head_sha=head_sha
                    )
                    if committed:
                        logs.append(f"review_only: marked {len(committed)} previous finding(s) outdated.")
                except Exception as exc:
                    staged_committed = False
                    logs.append(
                        f"review_only: failed committing staged finding updates ({type(exc).__name__}); retaining prior reviewed SHA."
                    )
            final_no_diff_sha = prior_reviewed_sha
            if staged_committed and findings_reconciled and head_sha:
                if set_last_reviewed_sha(repo, pr_number, head_sha, expected_sha=prior_reviewed_sha):
                    final_no_diff_sha = head_sha
                else:
                    final_no_diff_sha = get_last_reviewed_sha(repo, pr_number)
            return _review_result(
                status="completed_no_comment",
                logs=logs,
                diff=budgeted_raw,
                findings=list_pr_findings(repo, pr_number, state="open"),
                new_findings=[],
                last_reviewed_sha=final_no_diff_sha,
                merged_config=merged_cfg,
            )

        # 4b. Retrieve diff-scoped repository memories off the event loop when provider supplied
        effective_memories = memories
        if effective_memories is None and memories_provider is not None and merged_cfg.get("memory_enabled", True):
            try:
                if inspect.iscoroutinefunction(memories_provider):
                    loaded_mem = await memories_provider(parsed_diff.paths, budgeted_raw)
                else:
                    loaded_mem = await asyncio.to_thread(memories_provider, parsed_diff.paths, budgeted_raw)
                    if inspect.isawaitable(loaded_mem):
                        loaded_mem = await loaded_mem
                if isinstance(loaded_mem, dict):
                    effective_memories = loaded_mem
                    used_n = len(loaded_mem.get("used_ids") or [])
                    if used_n:
                        logs.append(f"review_only: loaded {used_n} scoped repo memory entry(s).")
            except Exception as exc:
                log.warning("review_only: memory retrieval failed (%s)", type(exc).__name__)
                logs.append(f"review_only: repo memory retrieval skipped ({type(exc).__name__}).")

        # 5. Pass A (Walkthrough) & Pass B (Findings)
        try:
            walkthrough = await generate_walkthrough(
                parsed_diff,
                pr_meta,
                budgeted_diff=budgeted_raw,
                cut_notes=cut_notes,
                config=merged_cfg,
                client=llm_client,
                llm_call=llm_call,
                logs=logs,
            )
            logs.append(f"review_only: Pass A walkthrough generated (effort={walkthrough.get('effort')}/5).")

            raw_findings = await generate_findings(
                parsed_diff,
                pr_meta,
                budgeted_diff=budgeted_numbered,
                cut_notes=cut_notes,
                config=merged_cfg,
                memories=effective_memories,
                client=llm_client,
                llm_call=llm_call,
                logs=logs,
            )
            logs.append(f"review_only: Pass B emitted {len(raw_findings)} candidate finding(s).")
        except LLMCallError as exc:
            logs.append(f"review_only: {exc}.")
            return _review_result(
                status="failed",
                logs=logs,
                diff=budgeted_raw,
                last_reviewed_sha=prior_reviewed_sha,
            )
        except Exception as exc:
            logs.append(f"review_only: LLM error: {type(exc).__name__}.")
            return _review_result(
                status="failed",
                logs=logs,
                diff=budgeted_raw,
                last_reviewed_sha=prior_reviewed_sha,
            )

        # 5b. When reviewing an incremental diff with candidate findings, fetch the full
        #     PR diff so Pass C validates inline anchors against GitHub's PR diff.
        #     If the full PR diff cannot be fetched (or is blank), never send unvalidated
        #     incremental anchors inline — keep them in the sticky summary only.
        anchor_diff: ParsedDiff | None = None
        anchor_diff_unavailable = False
        if fetched.is_incremental and raw_findings:
            if pr_diff_for_anchors is not None:
                anchor_diff = pr_diff_for_anchors
            else:
                for _attempt in range(2):
                    try:
                        pr_diff_fetched = await fetch_incremental_diff(
                            gh,
                            repo,
                            pr_number,
                            base_sha,
                            head_sha,
                            None,
                            github_token,
                        )
                        if pr_diff_fetched.diff.strip():
                            anchor_diff = parse_unified_diff(pr_diff_fetched.diff)
                            break
                    except Exception as exc:
                        log.warning("review_only: PR diff anchor fetch failed (%s)", type(exc).__name__)
                if anchor_diff is None:
                    anchor_diff_unavailable = True
                    logs.append(
                        "review_only: full PR diff unavailable for inline anchor validation; keeping findings in sticky summary."
                    )

        # 6. Pass C (Deterministic Verifier & Dedupe)
        verify_stats: dict[str, int] = {}
        new_findings = verify_and_filter_findings(
            raw_findings,
            parsed_diff,
            anchor_diff=anchor_diff,
            profile=merged_cfg["profile"],
            memories=effective_memories,
            existing_findings=existing_findings,
            stats=verify_stats,
        )
        logs.append(
            f"review_only: Pass C kept {len(new_findings)}/{verify_stats.get('input', 0)} finding(s) "
            f"(out_of_diff={verify_stats.get('dropped_out_of_diff', 0)}, "
            f"low_conf={verify_stats.get('dropped_low_confidence', 0)}, "
            f"dismissed={verify_stats.get('dropped_dismissed', 0)}, "
            f"dupes={verify_stats.get('dropped_duplicate', 0)}, "
            f"nit_cap={verify_stats.get('dropped_nit_cap', 0)})."
        )

        # 7. Head verification guard before committing invalidations or publishing.
        #    Treat lookup failure/empty SHA as unknown and refuse to publish stale or
        #    unverified reviews.
        if github_client is None:
            head_status = await _verify_pr_head(gh, repo, pr_number, head_sha, github_token)
            if head_status == "moved":
                logs.append("review_only: PR head moved on while reviewing; skipping stale comment.")
                return _review_result(
                    status="completed_no_comment",
                    logs=logs,
                    diff=budgeted_raw,
                    walkthrough=walkthrough,
                    findings=list_pr_findings(repo, pr_number, state="open"),
                    new_findings=new_findings,
                    last_reviewed_sha=prior_reviewed_sha,
                    stale_head=True,
                )
            if head_status != "match":
                logs.append("review_only: could not verify PR head SHA on GitHub; aborting publish.")
                return _review_result(
                    status="failed",
                    logs=logs,
                    diff=budgeted_raw,
                    walkthrough=walkthrough,
                    findings=list_pr_findings(repo, pr_number, state="open"),
                    new_findings=[],
                    last_reviewed_sha=prior_reviewed_sha,
                )

        # 8. Publish inline review + sticky summary comment BEFORE committing new
        #    findings or staged invalidations so a total GitHub write failure leaves
        #    unpublished findings eligible for retry without Pass C suppressing them.
        staged_open_preview = [
            dict(f) for f in existing_findings if str(f.get("state") or "open").lower() == "open"
        ]
        all_open_preview = staged_open_preview + [dict(f) for f in new_findings]
        applied_learnings = _collect_applied_learnings(effective_memories, new_findings)

        pub = await publish_review(
            repo,
            pr_number,
            head_sha,
            walkthrough,
            new_findings,
            agent_key=agent_key,
            agent_name=agent_name,
            github_token=github_token,
            incremental_from=fetched.incremental_from if fetched.is_incremental else None,
            applied_learnings=applied_learnings,
            cut_notes=budget_notes,
            all_open_findings=all_open_preview,
            persist_comment_ids=False,
            allow_inline=not anchor_diff_unavailable,
            client=gh,
        )
        if pub["review_posted"]:
            logs.append(f"review_only: posted PR review with {pub['inline_count']} inline comment(s).")
        elif pub["inline_fallback"]:
            logs.append(
                f"review_only: inline review fell back to sticky summary for {pub['fallback_count']} finding(s)."
            )
        logs.append(
            "review_only: sticky summary comment "
            + ("posted/updated." if pub["pr_comment_posted"] else "failed.")
        )

        delivered = bool(pub["pr_comment_posted"] or pub["review_posted"])
        final_sha = prior_reviewed_sha
        saved_new_updated: list[dict[str, Any]] = []

        if delivered:
            # Commit staged invalidations & line-shift remappings only after delivery succeeds.
            if staged_outdated_fps or staged_remaps:
                committed = commit_staged_finding_updates(
                    repo, pr_number, staged_outdated_fps, staged_remaps, head_sha=head_sha
                )
                if committed:
                    logs.append(f"review_only: marked {len(committed)} previous finding(s) outdated.")
            delivered_items = [{**f, "delivered": True} for f in pub["findings"]]
            saved_new_updated = save_pr_findings(repo, pr_number, delivered_items, head_sha=head_sha)
            if head_sha and findings_reconciled:
                if set_last_reviewed_sha(repo, pr_number, head_sha, expected_sha=prior_reviewed_sha):
                    final_sha = head_sha
                else:
                    final_sha = get_last_reviewed_sha(repo, pr_number)
        else:
            logs.append("review_only: both GitHub writes failed; keeping findings unpublished for retry.")

        all_open = list_pr_findings(repo, pr_number, state="open")
        status = "completed" if delivered else "completed_no_comment"
        return _review_result(
            status=status,
            logs=logs,
            diff=budgeted_raw,
            pr_comment_posted=bool(pub["pr_comment_posted"]),
            review_posted=bool(pub["review_posted"]),
            walkthrough=walkthrough,
            findings=all_open,
            new_findings=saved_new_updated if delivered else list(pub["findings"]),
            last_reviewed_sha=final_sha,
            merged_config=merged_cfg,
        )
    finally:
        if own_gh and hasattr(gh, "aclose"):
            await gh.aclose()
