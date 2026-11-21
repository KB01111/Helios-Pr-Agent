"""Deterministic Merge-Readiness Scorer (policy v1).

Turns objective ``MergeSignals`` into a 0-100 score, band, and ordered
"Before you merge" checklist (``datamodel.ReadinessReport``).

Scoring rules:
  * Starts at 100, subtracts weighted penalties, clamps to ``[0, 100]``.
  * Hard blocker rule: any open hard blocker adds a ``blocker`` item AND
    caps the final score at 49 so a blocked PR never displays as ready.
  * Bands (default threshold 85):
      >= 85 -> ``ready``
      70..84 -> ``ready_with_nits``
      50..69 -> ``needs_work``
      < 50 -> ``blocked``
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:  # cwd=backend
    import datamodel as dm
except ImportError:  # pragma: no cover - `uvicorn backend.main:app` from repo root
    from backend import datamodel as dm  # type: ignore[no-redef]

from .signals import MergeSignals

MERGE_POLICY_VERSION = "v1"

DEFAULT_WEIGHTS: dict[str, int] = {
    "conflict": 50,
    "failing_check": 30,
    "critical_finding": 40,
    "major_finding": 12,
    "minor_finding": 3,
    "nit_finding": 0,
    "changes_requested": 25,
    "unresolved_thread": 6,
    "safety_flagged": 35,
    "blocking_memory": 40,
    "custom_error": 35,
    "custom_warning": 8,
    "untested_source": 8,
    "behind_base": 5,
    "weak_description": 4,
    "pending_check": 5,
    "missing_approval": 20,
}

_KIND_ORDER: dict[str, int] = {"blocker": 0, "should_fix": 1, "nice_to_have": 2}
_HARD_BLOCKER_CAP = 49


def _clip(text: str, limit: int = 500) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


def _clean_url(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    u = url.strip()
    if u.startswith(("https://", "http://")) and not any(c.isspace() for c in u):
        return u
    return None


def _loc(finding: dict[str, Any]) -> str:
    file_path = str(finding.get("file") or "").strip()
    line = finding.get("start_line")
    if file_path and line:
        return f"{file_path}:{line}"
    return file_path


def band_for_score(
    score: int,
    *,
    has_blocker: bool = False,
    score_threshold: int = 85,
) -> dm.ReadinessBand:
    """Map numeric score + blocker state into one of the four readiness bands."""
    if has_blocker or score < 50:
        return "blocked"
    ready_min = max(50, min(100, int(score_threshold)))
    if score >= ready_min:
        return "ready"
    if score >= 70:
        return "ready_with_nits"
    return "needs_work"


def compute_readiness(
    repo: str,
    pr_number: int,
    head_sha: str,
    signals: MergeSignals,
    *,
    weights: dict[str, int] | None = None,
    score_threshold: int = 85,
    require_approval: bool = False,
) -> dm.ReadinessReport:
    """Compute a deterministic ``ReadinessReport`` from ``signals``."""
    w: dict[str, int] = dict(DEFAULT_WEIGHTS)
    if weights:
        for key, val in weights.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                w[key] = max(0, int(val))

    score = 100
    has_blocker = False
    items: list[dm.ReadinessItem] = []
    pr_url = _clean_url(signals.pr_url)

    # 1. Merge conflicts (hard blocker)
    if signals.has_conflicts:
        score -= w["conflict"]
        has_blocker = True
        items.append(
            dm.ReadinessItem(
                kind="blocker",
                code="merge_conflict",
                text="Merge conflicts must be resolved before merging",
                url=pr_url,
                done=False,
            )
        )

    # 2. Failing CI checks (hard blocker)
    for chk in signals.failing_checks:
        if not isinstance(chk, dict):
            continue
        score -= w["failing_check"]
        has_blocker = True
        name = str(chk.get("name") or "CI check").strip()
        items.append(
            dm.ReadinessItem(
                kind="blocker",
                code="failing_check",
                text=_clip(f"CI check failing: `{name}`"),
                url=_clean_url(chk.get("url")),
                done=False,
            )
        )

    # 3. Open findings by severity (critical = hard blocker)
    for finding in signals.open_findings:
        if not isinstance(finding, dict):
            continue
        sev = str(finding.get("severity") or "minor").strip().lower()
        cat = str(finding.get("category") or "maintainability").strip().lower()
        title = str(finding.get("title") or "Review finding").strip()
        loc = _loc(finding)
        suffix = f" — `{loc}`" if loc else ""
        url = _clean_url(finding.get("url"))

        if sev == "critical":
            score -= w["critical_finding"]
            has_blocker = True
            items.append(
                dm.ReadinessItem(
                    kind="blocker",
                    code="critical_finding",
                    text=_clip(f"Critical ({cat}): {title}{suffix}"),
                    url=url,
                    done=False,
                )
            )
        elif sev == "major":
            score -= w["major_finding"]
            items.append(
                dm.ReadinessItem(
                    kind="should_fix",
                    code="major_finding",
                    text=_clip(f"Major ({cat}): {title}{suffix}"),
                    url=url,
                    done=False,
                )
            )
        elif sev == "minor":
            score -= w["minor_finding"]
            items.append(
                dm.ReadinessItem(
                    kind="nice_to_have",
                    code="minor_finding",
                    text=_clip(f"Minor ({cat}): {title}{suffix}"),
                    url=url,
                    done=False,
                )
            )
        else:  # nit (or unknown low severity)
            score -= w["nit_finding"]
            items.append(
                dm.ReadinessItem(
                    kind="nice_to_have",
                    code="nit_finding",
                    text=_clip(f"Nit ({cat}): {title}{suffix}"),
                    url=url,
                    done=False,
                )
            )

    # 4. Changes requested (hard blocker)
    if signals.changes_requested > 0:
        n = int(signals.changes_requested)
        score -= w["changes_requested"] * n
        has_blocker = True
        noun = "reviewer" if n == 1 else "reviewers"
        items.append(
            dm.ReadinessItem(
                kind="blocker",
                code="changes_requested",
                text=f"{n} {noun} requested changes",
                url=pr_url,
                done=False,
            )
        )

    # 5. Safety-flagged paths/patterns (hard blocker unless human approved)
    if signals.safety_flagged:
        if not signals.safety_human_approved:
            score -= w["safety_flagged"]
            has_blocker = True
            items.append(
                dm.ReadinessItem(
                    kind="blocker",
                    code="safety_flagged",
                    text="Safety-sensitive changes require human review and approval",
                    url=pr_url,
                    done=False,
                )
            )
        else:
            items.append(
                dm.ReadinessItem(
                    kind="blocker",
                    code="safety_flagged",
                    text="Safety-sensitive changes approved by a human reviewer",
                    url=pr_url,
                    done=True,
                )
            )

    # 6. Blocking repo memory violations (hard blocker)
    for violation in signals.blocking_memory_violations:
        if not isinstance(violation, dict):
            continue
        score -= w["blocking_memory"]
        has_blocker = True
        mid = str(violation.get("id") or "").strip()
        rule_text = str(violation.get("text") or "Blocking repository rule violated").strip()
        prefix = f"Blocking repo rule (#{mid}): " if mid else "Blocking repo rule: "
        items.append(
            dm.ReadinessItem(
                kind="blocker",
                code="blocking_memory",
                text=_clip(prefix + rule_text),
                url=None,
                done=False,
            )
        )

    # 7. Custom pre-merge check failures (mode == "error" is a hard blocker)
    for chk in signals.custom_check_failures:
        if not isinstance(chk, dict):
            continue
        mode = str(chk.get("mode") or "warning").strip().lower()
        name = str(chk.get("name") or "Custom check").strip()
        detail = str(chk.get("detail") or "").strip()
        text = f"Pre-merge check `{name}`: {detail}" if detail else f"Pre-merge check `{name}` failed"
        if mode == "error":
            score -= w["custom_error"]
            has_blocker = True
            items.append(
                dm.ReadinessItem(
                    kind="blocker",
                    code="custom_error",
                    text=_clip(text),
                    url=None,
                    done=False,
                )
            )
        else:
            score -= w["custom_warning"]
            items.append(
                dm.ReadinessItem(
                    kind="should_fix",
                    code="custom_warning",
                    text=_clip(text),
                    url=None,
                    done=False,
                )
            )

    # 8. Optional approval requirement
    if require_approval:
        if signals.approvals <= 0:
            score -= w.get("missing_approval", 20)
            has_blocker = True
            items.append(
                dm.ReadinessItem(
                    kind="blocker",
                    code="missing_approval",
                    text="At least one human approval is required before merging",
                    url=pr_url,
                    done=False,
                )
            )
        elif signals.changes_requested == 0:
            n = int(signals.approvals)
            noun = "reviewer" if n == 1 else "reviewers"
            items.append(
                dm.ReadinessItem(
                    kind="blocker",
                    code="missing_approval",
                    text=f"Approved by {n} {noun}",
                    url=pr_url,
                    done=True,
                )
            )

    # 9. Pending CI checks (non-blocking)
    for chk in signals.pending_checks:
        if not isinstance(chk, dict):
            continue
        score -= w["pending_check"]
        name = str(chk.get("name") or "CI check").strip()
        items.append(
            dm.ReadinessItem(
                kind="should_fix",
                code="pending_check",
                text=_clip(f"CI check still running: `{name}`"),
                url=_clean_url(chk.get("url")),
                done=False,
            )
        )

    # 10. Unresolved review threads (non-blocking)
    if signals.unresolved_threads > 0:
        n = int(signals.unresolved_threads)
        score -= w["unresolved_thread"] * n
        noun = "thread" if n == 1 else "threads"
        items.append(
            dm.ReadinessItem(
                kind="should_fix",
                code="unresolved_thread",
                text=f"{n} unresolved review {noun}",
                url=pr_url,
                done=False,
            )
        )
    elif signals.resolved_threads > 0:
        n = int(signals.resolved_threads)
        noun = "thread" if n == 1 else "threads"
        items.append(
            dm.ReadinessItem(
                kind="should_fix",
                code="unresolved_thread",
                text=f"All {n} review {noun} resolved",
                url=pr_url,
                done=True,
            )
        )

    # 11. Source changed without test updates (non-blocking)
    if signals.source_changed_without_tests:
        score -= w["untested_source"]
        items.append(
            dm.ReadinessItem(
                kind="should_fix",
                code="untested_source",
                text="Source files changed without corresponding test updates",
                url=pr_url,
                done=False,
            )
        )

    # 12. Branch behind base (non-blocking)
    if signals.behind_by > 0:
        n = int(signals.behind_by)
        score -= w["behind_base"]
        noun = "commit" if n == 1 else "commits"
        base = signals.base_ref or "base"
        items.append(
            dm.ReadinessItem(
                kind="nice_to_have",
                code="behind_base",
                text=_clip(f"Branch is {n} {noun} behind `{base}`"),
                url=pr_url,
                done=False,
            )
        )

    # 13. Weak PR description (non-blocking)
    if signals.weak_description:
        score -= w["weak_description"]
        items.append(
            dm.ReadinessItem(
                kind="nice_to_have",
                code="weak_description",
                text="Add a clearer PR description explaining what changed and why",
                url=pr_url,
                done=False,
            )
        )

    # 14. Resolved findings (ticked off visually, zero penalty)
    for finding in signals.resolved_findings:
        if not isinstance(finding, dict):
            continue
        sev = str(finding.get("severity") or "minor").strip().lower()
        title = str(finding.get("title") or "Review finding").strip()
        loc = _loc(finding)
        suffix = f" — `{loc}`" if loc else ""
        if sev == "critical":
            kind = "blocker"
            code = "critical_finding"
        elif sev == "major":
            kind = "should_fix"
            code = "major_finding"
        elif sev == "minor":
            kind = "nice_to_have"
            code = "minor_finding"
        else:
            kind = "nice_to_have"
            code = "nit_finding"
        items.append(
            dm.ReadinessItem(
                kind=kind,
                code=code,
                text=_clip(f"Resolved ({sev}): {title}{suffix}"),
                url=_clean_url(finding.get("url")),
                done=True,
            )
        )

    score = max(0, min(100, int(score)))
    if has_blocker and score > _HARD_BLOCKER_CAP:
        score = _HARD_BLOCKER_CAP

    band = band_for_score(score, has_blocker=has_blocker, score_threshold=score_threshold)

    # Stable order: blocker -> should_fix -> nice_to_have; open items before resolved items.
    items.sort(key=lambda item: (_KIND_ORDER.get(item.kind, 9), 1 if item.done else 0))

    return dm.ReadinessReport(
        repo=repo,
        pr_number=int(pr_number),
        head_sha=str(head_sha or ""),
        score=score,
        band=band,
        items=items,
        policy_version=MERGE_POLICY_VERSION,
        computed_at=datetime.now(timezone.utc).isoformat(),
    )
