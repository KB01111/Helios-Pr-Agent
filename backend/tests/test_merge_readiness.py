"""Tests for Phase 4 deterministic merge-readiness scorer, collector, publisher & PR Inbox."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import datamodel as dm
import events
import github_app
import store
from merge import (
    CHECK_RUN_NAME,
    DEFAULT_WEIGHTS,
    MERGE_POLICY_VERSION,
    READINESS_MARKER,
    STATUS_CONTEXT,
    MergeSignals,
    collect_signals,
    compute_readiness,
    get_stored_readiness,
    is_own_check_run,
    is_own_status,
    list_inbox_prs,
    mark_pr_closed,
    publish_readiness,
    recompute_pr_readiness,
    render_readiness_comment,
)

REPO = "acme/widget"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
CASES = json.loads((Path(__file__).parent / "merge_cases.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Golden fixture scoring tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_golden_merge_cases(case: dict[str, Any]):
    signals = MergeSignals(**case["signals"])
    report = compute_readiness(
        REPO,
        42,
        SHA,
        signals,
        weights=case.get("weights"),
        score_threshold=case.get("score_threshold", 85),
        require_approval=case.get("require_approval", False),
    )
    expect = case["expect"]
    assert report.policy_version == MERGE_POLICY_VERSION == CASES["policy_version"]
    assert report.score == expect["score"]
    assert report.band == expect["band"]

    open_blockers = [i for i in report.items if i.kind == "blocker" and not i.done]
    assert len(open_blockers) == expect["open_blockers"]

    if "done_items" in expect:
        assert sum(1 for i in report.items if i.done) == expect["done_items"]

    assert [i.code for i in report.items] == expect["item_codes"]

    # Ordering invariant: blocker -> should_fix -> nice_to_have, open before done per kind.
    kind_rank = {"blocker": 0, "should_fix": 1, "nice_to_have": 2}
    sort_keys = [(kind_rank[i.kind], 1 if i.done else 0) for i in report.items]
    assert sort_keys == sorted(sort_keys)


def test_all_four_bands_covered_in_golden_cases():
    bands = {c["expect"]["band"] for c in CASES["cases"]}
    assert bands == {"ready", "ready_with_nits", "needs_work", "blocked"}


def test_default_weights_include_all_documented_keys():
    expected = {
        "conflict",
        "failing_check",
        "critical_finding",
        "major_finding",
        "minor_finding",
        "nit_finding",
        "changes_requested",
        "unresolved_thread",
        "safety_flagged",
        "blocking_memory",
        "custom_error",
        "custom_warning",
        "untested_source",
        "behind_base",
        "weak_description",
        "pending_check",
    }
    assert expected.issubset(set(DEFAULT_WEIGHTS))


def test_mixed_open_and_resolved_ordering():
    signals = MergeSignals(
        weak_description=True,
        behind_by=2,
        unresolved_threads=1,
        failing_checks=[{"name": "ci/test"}],
        resolved_findings=[
            {"severity": "critical", "category": "security", "title": "Fixed leak", "file": "a.py", "start_line": 1},
            {"severity": "major", "category": "bug", "title": "Fixed bug", "file": "b.py", "start_line": 2},
        ],
    )
    report = compute_readiness(REPO, 10, SHA, signals)
    assert [(i.kind, i.done, i.code) for i in report.items] == [
        ("blocker", False, "failing_check"),
        ("blocker", True, "critical_finding"),
        ("should_fix", False, "unresolved_thread"),
        ("should_fix", True, "major_finding"),
        ("nice_to_have", False, "behind_base"),
        ("nice_to_have", False, "weak_description"),
    ]


# ---------------------------------------------------------------------------
# 2. Self-check exclusion & signal collection
# ---------------------------------------------------------------------------


def test_self_check_filters(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "my-pr-agent-app")
    # External app using our check name MUST NOT be excluded when our app identity is known
    assert is_own_check_run({"name": CHECK_RUN_NAME, "app": {"slug": "other"}}) is False
    assert is_own_check_run({"name": CHECK_RUN_NAME, "app": {"slug": "my-pr-agent-app"}}) is True
    assert is_own_check_run({"name": "Custom check", "app": {"slug": "my-pr-agent-app"}}) is True
    assert is_own_check_run({"name": "pytest", "app": {"slug": "github-actions"}}) is False

    # Numeric GITHUB_APP_ID also distinguishes own vs external check runs
    monkeypatch.delenv("GITHUB_APP_SLUG")
    monkeypatch.setenv("GITHUB_APP_ID", "77")
    assert is_own_check_run({"name": CHECK_RUN_NAME, "app": {"id": 77, "slug": "any"}}) is True
    assert is_own_check_run({"name": CHECK_RUN_NAME, "app": {"id": 99, "slug": "other"}}) is False

    # Cached live app slug discovered from GET /app works too
    monkeypatch.delenv("GITHUB_APP_ID")
    github_app.remember_app_slug("live-bot-app")
    assert is_own_check_run({"name": CHECK_RUN_NAME, "app": {"slug": "live-bot-app"}}) is True
    assert is_own_check_run({"name": CHECK_RUN_NAME, "app": {"slug": "spoof-app"}}) is False

    # Fallback by name only when no app identity is known
    github_app.clear_token_cache()
    assert is_own_check_run({"name": CHECK_RUN_NAME, "app": {}}) is True

    assert is_own_status({"context": STATUS_CONTEXT}) is True
    assert is_own_status({"context": "PR-AGENT/MERGE-READINESS"}) is True
    assert is_own_status({"context": "ci/circleci"}) is False


def test_collect_signals_filters_own_checks_dedupes_reruns_and_tallies_reviews(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "acme-review-bot")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/pulls/7":
            return httpx.Response(200, json={
                "number": 7,
                "title": "feat: add rate limiter",
                "body": "Implements a Redis token bucket rate limiter with unit tests.",
                "html_url": f"https://github.com/{REPO}/pull/7",
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "user": {"login": "alice"},
                "head": {"sha": SHA, "ref": "feat/rate-limit"},
                "base": {"sha": "0" * 40, "ref": "main"},
            })
        if path == f"/repos/{REPO}/commits/{SHA}/check-runs":
            return httpx.Response(200, json={
                "check_runs": [
                    # Own readiness check from our app (failing!) -> MUST be ignored
                    {"id": 1, "name": CHECK_RUN_NAME, "status": "completed", "conclusion": "failure",
                     "app": {"slug": "acme-review-bot"}},
                    # Own check by app slug (failing!) -> MUST be ignored
                    {"id": 2, "name": "Any Other Name", "status": "completed", "conclusion": "failure",
                     "app": {"slug": "acme-review-bot"}},
                    # Older failed run superseded by newer passing rerun -> NOT failing
                    {"id": 10, "name": "pytest", "status": "completed", "conclusion": "failure",
                     "app": {"slug": "github-actions"}},
                    {"id": 20, "name": "pytest", "status": "completed", "conclusion": "success",
                     "app": {"slug": "github-actions"}},
                    # Genuine failing external check
                    {"id": 30, "name": "ruff-lint", "status": "completed", "conclusion": "failure",
                     "html_url": "https://github.com/acme/widget/runs/30",
                     "app": {"slug": "github-actions"}},
                    # Genuine pending external check
                    {"id": 31, "name": "build-macos", "status": "in_progress", "conclusion": None,
                     "html_url": "https://github.com/acme/widget/runs/31",
                     "app": {"slug": "github-actions"}},
                ]
            })
        if path == f"/repos/{REPO}/commits/{SHA}/status":
            return httpx.Response(200, json={
                "state": "failure",
                "statuses": [
                    # Own commit status (failure!) -> MUST be ignored
                    {"context": STATUS_CONTEXT, "state": "failure", "target_url": "https://example.com/self"},
                    # External error status
                    {"context": "codecov/project", "state": "error", "target_url": "https://codecov.io/1"},
                    # External pending status
                    {"context": "vercel", "state": "pending", "target_url": "https://vercel.com/1"},
                ],
            })
        if path == f"/repos/{REPO}/pulls/7/reviews":
            return httpx.Response(200, json=[
                # Bob requests changes, then only leaves a COMMENTED review -> still changes_requested
                {"user": {"login": "bob", "type": "User"}, "state": "CHANGES_REQUESTED"},
                {"user": {"login": "bob", "type": "User"}, "state": "COMMENTED"},
                # Carol requests changes, then approves -> counts as approved
                {"user": {"login": "carol", "type": "User"}, "state": "CHANGES_REQUESTED"},
                {"user": {"login": "carol", "type": "User"}, "state": "APPROVED"},
                # Dave requests changes, then dismissed -> counts as neither
                {"user": {"login": "dave", "type": "User"}, "state": "CHANGES_REQUESTED"},
                {"user": {"login": "dave", "type": "User"}, "state": "DISMISSED"},
                # Bot approval -> ignored
                {"user": {"login": "dependabot[bot]", "type": "Bot"}, "state": "APPROVED"},
            ])
        if path == f"/repos/{REPO}/compare/main...{SHA}":
            return httpx.Response(200, json={"behind_by": 3, "ahead_by": 2})
        if path == f"/repos/{REPO}/pulls/7/files":
            return httpx.Response(200, json=[
                {"filename": "backend/ratelimit.py", "status": "added", "additions": 40, "deletions": 0, "patch": "+def ok(): pass"},
            ])
        return httpx.Response(404)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_signals(REPO, 7, github_token="ghs_test", client=client)

    sha, sig = asyncio.run(_run())
    assert sha == SHA
    assert sig.has_conflicts is False
    assert sig.weak_description is False
    assert [c["name"] for c in sig.failing_checks] == ["ruff-lint", "codecov/project"]
    assert [c["name"] for c in sig.pending_checks] == ["build-macos", "vercel"]
    assert sig.approvals == 1
    assert sig.changes_requested == 1
    assert sig.behind_by == 3
    assert sig.source_changed_without_tests is True
    assert sig.safety_flagged is False
    assert sig.pr_author == "alice"
    assert sig.base_ref == "main"
    assert sig.head_ref == "feat/rate-limit"


def test_collect_signals_reads_redis_findings_blocking_memories_threads_and_safety():
    # Seed blocking & non-blocking memories
    store.rset_json(dm.memory_key(REPO, "m_block"), {
        "id": "m_block", "kind": "learning", "text": "Never expose stack traces", "blocking": True, "status": "active",
    })
    store.rset_json(dm.memory_key(REPO, "m_soft"), {
        "id": "m_soft", "kind": "learning", "text": "Prefer snake_case", "blocking": False, "status": "active",
    })
    store.rset_json(dm.memory_key(REPO, "m_arch"), {
        "id": "m_arch", "kind": "learning", "text": "Old rule", "blocking": True, "status": "archived",
    })

    # Seed open, resolved, dismissed, outdated findings
    fp_open = "a" * 32
    fp_res = "b" * 32
    fp_dis = "c" * 32
    store.rset_json(dm.finding_key(REPO, 12, fp_open), {
        "file": "backend/api.py", "start_line": 18, "end_line": 20, "severity": "major",
        "category": "security", "title": "Leaks traceback", "state": "open",
        "learning_ids": ["m_block", "m_soft", "m_arch"],
        "url": "https://github.com/acme/widget/pull/12#discussion_r10",
    })
    store.rset_json(dm.finding_key(REPO, 12, fp_res), {
        "file": "backend/api.py", "start_line": 5, "end_line": 5, "severity": "critical",
        "category": "bug", "title": "Fixed crash", "state": "resolved",
    })
    store.rset_json(dm.finding_key(REPO, 12, fp_dis), {
        "file": "backend/api.py", "start_line": 9, "end_line": 9, "severity": "critical",
        "category": "style", "title": "Dismissed", "state": "dismissed",
    })

    # Seed PR activity: 2 threads opened, 1 resolved, 1 reply (should not add a thread)
    events.record_pr_activity(REPO, 12, {"kind": "review_comment", "action": "created", "comment_id": 101, "in_reply_to": None})
    events.record_pr_activity(REPO, 12, {"kind": "review_comment", "action": "created", "comment_id": 102, "in_reply_to": 101})
    events.record_pr_activity(REPO, 12, {"kind": "review_comment", "action": "created", "comment_id": 201, "in_reply_to": None})
    events.record_pr_activity(REPO, 12, {"kind": "thread", "action": "resolved", "thread_node_id": "T1", "first_comment_id": 101})

    pr_data = {
        "title": "chore: update ci workflow",
        "body": "short",  # < 20 chars -> weak_description
        "mergeable": False,
        "mergeable_state": "dirty",
        "state": "open",
        "html_url": f"https://github.com/{REPO}/pull/12",
        "user": {"login": "bob"},
        "head": {"sha": SHA, "ref": "fix-ci"},
        "base": {"ref": "main"},
    }
    diff_files = [
        {"filename": ".github/workflows/ci.yml", "additions": 3, "deletions": 1, "patch": "+run: echo hi"},
    ]

    sha, sig = asyncio.run(
        collect_signals(REPO, 12, github_token="", pr_data=pr_data, diff_files=diff_files)
    )
    assert sha == SHA
    assert sig.has_conflicts is True
    assert sig.weak_description is True
    assert sig.safety_flagged is True
    assert sig.safety_human_approved is False
    assert sig.source_changed_without_tests is False
    assert sig.unresolved_threads == 1
    assert sig.resolved_threads == 1
    assert len(sig.open_findings) == 1
    assert sig.open_findings[0]["fingerprint"] == fp_open
    assert len(sig.resolved_findings) == 1
    assert sig.resolved_findings[0]["fingerprint"] == fp_res
    assert sig.blocking_memory_violations == [{"id": "m_block", "text": "Never expose stack traces"}]


def test_collect_signals_degrades_gracefully_on_api_errors():
    def failing_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/9"):
            return httpx.Response(200, json={
                "title": "fix: handle null",
                "body": "Properly guards against null pointers in the parser module.",
                "head": {"sha": SHA, "ref": "fix"},
                "base": {"ref": "main"},
                "mergeable_state": "behind",
            })
        return httpx.Response(500, text="upstream failure")

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)) as client:
            return await collect_signals(REPO, 9, github_token="ghs_test", client=client)

    sha, sig = asyncio.run(_run())
    assert sha == SHA
    assert sig.behind_by == 1  # fallback from mergeable_state == "behind"
    assert sig.failing_checks == []
    assert sig.approvals == 0


# ---------------------------------------------------------------------------
# 3. Markdown rendering & GitHub publishing (App Check Run vs PAT Status + Sticky Comment)
# ---------------------------------------------------------------------------


def test_render_readiness_comment_formats_sections_checkboxes_and_safe_links():
    signals = MergeSignals(
        failing_checks=[{"name": "ci/test", "url": "https://github.com/acme/widget/runs/1"}],
        open_findings=[
            {"severity": "major", "category": "perf", "title": "Slow loop", "file": "a.py", "start_line": 9,
             "url": "javascript:alert(1)"},  # unsafe URL must be dropped
        ],
        resolved_findings=[
            {"severity": "critical", "category": "security", "title": "Leak fixed", "file": "b.py", "start_line": 3,
             "url": "https://github.com/acme/widget/pull/5#r1"},
        ],
        weak_description=True,
    )
    report = compute_readiness(REPO, 5, SHA, signals)
    md = render_readiness_comment(report)
    assert md.startswith(READINESS_MARKER)
    assert "## PR Agent Merge Readiness: 49/100 · Blocked" in md
    assert "### Blockers" in md
    assert "- [ ] CI check failing: `ci/test` ([view](https://github.com/acme/widget/runs/1))" in md
    assert "- [x] Resolved (critical): Leak fixed — `b.py:3` ([view](https://github.com/acme/widget/pull/5#r1))" in md
    assert "### Should fix" in md
    assert "javascript:" not in md
    assert "### Nice to have" in md

    clean_report = compute_readiness(REPO, 5, SHA, MergeSignals())
    clean_md = render_readiness_comment(clean_report)
    assert "## PR Agent Merge Readiness: 100/100 · Ready" in clean_md
    assert "All pre-merge checks passed" in clean_md


@pytest.mark.parametrize(
    ("merge_gate", "score_signals", "expected_conclusion"),
    [
        ("advisory", MergeSignals(), "success"),
        ("advisory", MergeSignals(failing_checks=[{"name": "ci"}]), "neutral"),
        ("required", MergeSignals(failing_checks=[{"name": "ci"}]), "failure"),
        ("required", MergeSignals(), "success"),
    ],
)
def test_publish_readiness_app_check_run_and_sticky_comment_upsert(
    merge_gate: str,
    score_signals: MergeSignals,
    expected_conclusion: str,
):
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/check-runs":
            return httpx.Response(201, json={"id": 5001})
        if request.method == "PATCH" and request.url.path == f"/repos/{REPO}/check-runs/5001":
            return httpx.Response(200, json={"id": 5001})
        if request.method == "GET" and request.url.path == f"/repos/{REPO}/issues/21/comments":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/issues/21/comments":
            return httpx.Response(201, json={"id": 9001})
        if request.method == "PATCH" and request.url.path == f"/repos/{REPO}/issues/comments/9001":
            return httpx.Response(200, json={"id": 9001})
        return httpx.Response(404)

    store.rset(dm.dirty_key(REPO, 21), "1")
    report = compute_readiness(REPO, 21, SHA, score_signals)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res1 = await publish_readiness(
                REPO,
                21,
                report,
                merge_gate=merge_gate,
                score_threshold=85,
                github_token="ghs_app_token",
                auth_kind="app",
                pr_meta={"title": "Test PR", "author": "alice"},
                client=client,
            )
            # Second publish updates check run & sticky comment in place (no duplicates!)
            res2 = await publish_readiness(
                REPO,
                21,
                report,
                merge_gate=merge_gate,
                score_threshold=85,
                github_token="ghs_app_token",
                auth_kind="app",
                client=client,
            )
            return res1, res2

    res1, res2 = asyncio.run(_run())
    assert res1["published"] is True
    assert res1["check_run"]["action"] == "created"
    assert res1["check_run"]["conclusion"] == expected_conclusion
    assert res1["commit_status"] is None
    assert res1["comment"] == {"action": "created", "id": 9001, "status_code": 201}

    assert res2["check_run"]["action"] == "updated"
    assert res2["comment"] == {"action": "updated", "id": 9001, "status_code": 200}

    # Verify Check Run payload name & conclusion
    post_check = [c for c in calls if c[0] == "POST" and c[1].endswith("/check-runs")][0]
    assert post_check[2]["name"] == CHECK_RUN_NAME
    assert post_check[2]["conclusion"] == expected_conclusion

    # Verify Redis persistence & dirty marker cleared & metadata preserved across update
    assert store.rget(dm.dirty_key(REPO, 21)) is None
    stored = get_stored_readiness(REPO, 21)
    assert stored is not None and stored["score"] == report.score
    assert res2["inbox"]["title"] == "Test PR"
    assert res2["inbox"]["author"] == "alice"


@pytest.mark.parametrize(
    ("merge_gate", "has_failure", "expected_state", "advisory_note"),
    [
        ("advisory", False, "success", False),
        ("advisory", True, "success", True),
        ("required", True, "failure", False),
        ("required", False, "success", False),
    ],
)
def test_publish_readiness_pat_commit_status_and_cold_comment_upsert(
    merge_gate: str,
    has_failure: bool,
    expected_state: str,
    advisory_note: bool,
):
    status_payloads: list[dict[str, Any]] = []
    comment_actions: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/statuses/{SHA}":
            status_payloads.append(body)
            return httpx.Response(201, json={"id": 1})
        if request.method == "GET" and request.url.path == f"/repos/{REPO}/issues/33/comments":
            # Existing sticky comment already on PR (cold Redis cache)
            return httpx.Response(200, json=[
                {"id": 100, "body": "Unrelated comment"},
                {"id": 777, "body": f"{READINESS_MARKER}\nOld readiness comment"},
            ])
        if request.method == "PATCH" and request.url.path == f"/repos/{REPO}/issues/comments/777":
            comment_actions.append(("PATCH", request.url.path))
            return httpx.Response(200, json={"id": 777})
        return httpx.Response(404)

    sig = MergeSignals(failing_checks=[{"name": "ci"}]) if has_failure else MergeSignals()
    report = compute_readiness(REPO, 33, SHA, sig)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_readiness(
                REPO,
                33,
                report,
                merge_gate=merge_gate,
                github_token="ghp_pat_token",
                auth_kind="pat",
                client=client,
            )

    res = asyncio.run(_run())
    assert res["check_run"] is None
    assert res["commit_status"]["action"] == "posted"
    assert res["commit_status"]["state"] == expected_state
    assert res["commit_status"]["context"] == STATUS_CONTEXT
    assert len(status_payloads) == 1
    assert status_payloads[0]["context"] == STATUS_CONTEXT
    assert status_payloads[0]["state"] == expected_state
    if advisory_note:
        assert "Advisory" in status_payloads[0]["description"]
    assert res["comment"] == {"action": "updated", "id": 777, "status_code": 200}
    assert comment_actions == [("PATCH", f"/repos/{REPO}/issues/comments/777")]


def test_publish_readiness_merge_gate_off_persists_without_github_calls():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    store.rset(dm.dirty_key(REPO, 88), "1")
    report = compute_readiness(REPO, 88, SHA, MergeSignals(weak_description=True))

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_readiness(
                REPO, 88, report, merge_gate="off", github_token="ghp_x", auth_kind="pat", client=client
            )

    res = asyncio.run(_run())
    assert called is False
    assert res["published"] is False
    assert store.rget(dm.dirty_key(REPO, 88)) is None
    assert get_stored_readiness(REPO, 88)["score"] == 96


def test_recompute_pr_readiness_end_to_end():
    store.rset_json(dm.finding_key(REPO, 15, "f" * 32), {
        "file": "src/main.py", "start_line": 10, "severity": "major",
        "category": "bug", "title": "Off-by-one", "state": "open",
    })
    pr_data = {
        "title": "fix: boundary check",
        "body": "Fixes off-by-one loop condition in parser with thorough test coverage.",
        "html_url": f"https://github.com/{REPO}/pull/15",
        "state": "open",
        "user": {"login": "erin"},
        "head": {"sha": SHA, "ref": "fix-loop"},
        "base": {"ref": "main"},
    }
    diff_files = [
        {"filename": "src/main.py", "additions": 5, "deletions": 2, "patch": "+x = 1"},
        {"filename": "tests/test_main.py", "additions": 10, "deletions": 0, "patch": "+assert True"},
    ]

    res = asyncio.run(
        recompute_pr_readiness(
            REPO,
            15,
            agent_config={"merge_gate": "required", "score_threshold": 85},
            diff_files=diff_files,
            publish=False,
            github_token="",
            pr_data=pr_data,
        )
    )
    assert res["published"] is False
    assert res["report"]["score"] == 88
    assert res["report"]["band"] == "ready"
    assert res["inbox"]["findings_counts"] == {"critical": 0, "major": 1, "minor": 0, "nit": 0, "total": 1}
    assert res["inbox"]["author"] == "erin"
    assert res["inbox"]["title"] == "fix: boundary check"


# ---------------------------------------------------------------------------
# 4. PR Inbox listing, filtering, closing & validation
# ---------------------------------------------------------------------------


def test_pr_inbox_list_filter_and_close():
    r1 = compute_readiness("acme/api", 1, SHA, MergeSignals())
    r2 = compute_readiness("acme/api", 2, SHA, MergeSignals(failing_checks=[{"name": "ci"}]))
    r3 = compute_readiness("acme/web", 3, SHA, MergeSignals(open_findings=[
        {"severity": "major", "category": "bug", "title": "Bug", "file": "a.ts", "start_line": 1}
    ], weak_description=True))

    async def _seed():
        await publish_readiness("acme/api", 1, r1, merge_gate="off", pr_meta={"title": "PR 1"})
        await publish_readiness("acme/api", 2, r2, merge_gate="off", pr_meta={"title": "PR 2"})
        await publish_readiness("acme/web", 3, r3, merge_gate="off", pr_meta={"title": "PR 3"})

    asyncio.run(_seed())

    all_open = list_inbox_prs()
    assert len(all_open) == 3

    api_only = list_inbox_prs(repo="ACME/API")
    assert {p["pr_number"] for p in api_only} == {1, 2}

    blocked = list_inbox_prs(band="blocked")
    assert [(p["repo"], p["pr_number"]) for p in blocked] == [("acme/api", 2)]
    assert blocked[0]["blockers_count"] == 1
    assert len(blocked[0]["top_blockers"]) == 1

    # Close PR 2 as merged, PR 3 as closed (unmerged)
    store.rset(dm.dirty_key("acme/api", 2), "1")
    merged_rec = mark_pr_closed("acme/api", 2, merged=True)
    assert merged_rec is not None
    assert merged_rec["state"] == "merged" and merged_rec["merged"] is True
    assert store.rget(dm.dirty_key("acme/api", 2)) is None

    closed_rec = mark_pr_closed("acme/web", 3, merged=False)
    assert closed_rec is not None
    assert closed_rec["state"] == "closed" and closed_rec["merged"] is False

    # Unknown PR returns None without creating an entry
    assert mark_pr_closed("acme/web", 999) is None

    assert [(p["repo"], p["pr_number"]) for p in list_inbox_prs(state="open")] == [("acme/api", 1)]
    assert {p["pr_number"] for p in list_inbox_prs(state="closed")} == {2, 3}
    assert [p["pr_number"] for p in list_inbox_prs(state="merged")] == [2]
    assert len(list_inbox_prs(state="all")) == 3


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        asyncio.run(collect_signals("../bad", 1, github_token=""))
    with pytest.raises(ValueError):
        asyncio.run(collect_signals(REPO, 0, github_token=""))
    with pytest.raises(ValueError):
        list_inbox_prs(repo="not-a-repo")
    with pytest.raises(ValueError):
        mark_pr_closed("bad/..", 1)


# ---------------------------------------------------------------------------
# 5. Regression tests (from audit #21)
# ---------------------------------------------------------------------------


def test_pending_blocking_memory_does_not_trigger_blocker():
    store.rset_json(dm.memory_key(REPO, "m_pending"), {
        "id": "m_pending",
        "kind": "learning",
        "text": "Unapproved blocking candidate",
        "blocking": True,
        "status": "pending",
    })
    store.rset_json(dm.memory_key(REPO, "m_active"), {
        "id": "m_active",
        "kind": "learning",
        "text": "Approved blocking rule",
        "blocking": True,
        "status": "active",
    })
    store.rset_json(dm.finding_key(REPO, 50, "e" * 32), {
        "file": "backend/a.py",
        "start_line": 10,
        "severity": "minor",
        "category": "maintainability",
        "title": "Rule cited",
        "state": "open",
        "learning_ids": ["m_pending"],
    })
    pr_data = {
        "head": {"sha": SHA},
        "body": "Detailed PR description explaining what changed and why.",
    }
    _, sig = asyncio.run(collect_signals(REPO, 50, github_token="", pr_data=pr_data))
    assert sig.blocking_memory_violations == []
    report = compute_readiness(REPO, 50, SHA, sig)
    assert report.band == "ready"
    assert report.score == 97  # 1 minor finding only, not capped at 49

    # Now cite the active blocking memory as well -> triggers blocker
    store.rset_json(dm.finding_key(REPO, 50, "e" * 32), {
        "file": "backend/a.py",
        "start_line": 10,
        "severity": "minor",
        "category": "maintainability",
        "title": "Rule cited",
        "state": "open",
        "learning_ids": ["m_pending", "m_active"],
    })
    _, sig2 = asyncio.run(collect_signals(REPO, 50, github_token="", pr_data=pr_data))
    assert sig2.blocking_memory_violations == [{"id": "m_active", "text": "Approved blocking rule"}]
    report2 = compute_readiness(REPO, 50, SHA, sig2)
    assert report2.band == "blocked"
    assert report2.score == 49


def test_transient_github_failure_preserves_closed_or_merged_inbox_state():
    # Seed a tracked PR and close it as merged.
    report = compute_readiness(REPO, 60, SHA, MergeSignals())
    asyncio.run(publish_readiness(REPO, 60, report, merge_gate="off", pr_meta={"title": "Shipped PR", "state": "open"}))
    mark_pr_closed(REPO, 60, merged=True)
    assert list_inbox_prs(repo=REPO, state="open") == []

    # Now GitHub returns 500 on every endpoint during a recompute.
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="temporary outage")

    async def _recompute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)) as client:
            return await recompute_pr_readiness(
                REPO, 60, publish=False, github_token="ghs_test", client=client
            )

    res = asyncio.run(_recompute())
    assert res["signals"]["pr_state"] == ""
    assert res["inbox"]["state"] == "merged"
    assert list_inbox_prs(repo=REPO, state="open") == []
    assert [p["pr_number"] for p in list_inbox_prs(repo=REPO, state="merged")] == [60]


def test_corrupted_cached_check_run_id_is_rejected_and_falls_back_to_post():
    from merge import publisher as merge_pub

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/check-runs":
            return httpx.Response(201, json={"id": 6001})
        if request.method == "GET" and request.url.path == f"/repos/{REPO}/issues/70/comments":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/issues/70/comments":
            return httpx.Response(201, json={"id": 7001})
        return httpx.Response(404)

    cache_key = merge_pub._check_cache_key(REPO, 70, SHA)
    store.rset(cache_key, "../bad-id")
    report = compute_readiness(REPO, 70, SHA, MergeSignals())

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_readiness(
                REPO,
                70,
                report,
                merge_gate="advisory",
                github_token="ghs_app",
                auth_kind="app",
                client=client,
            )

    res = asyncio.run(_run())
    assert res["check_run"]["action"] == "created"
    assert res["check_run"]["id"] == 6001
    # Must NEVER issue a PATCH to the corrupted id
    assert all(method != "PATCH" for method, _ in calls)
    assert store.rget(cache_key) == "6001"


# ---------------------------------------------------------------------------
# 6. PR #18 hardening tests (bands/gates, pagination, safety SHA, locking, retries)
# ---------------------------------------------------------------------------


def test_threshold_consistent_between_band_and_merge_gate():
    from merge import band_for_score

    # Non-blocker PR with 4 majors + weak_description -> score 48
    sig_48 = MergeSignals(
        open_findings=[
            {"severity": "major", "category": "bug", "title": f"M{i}", "file": "a.py", "start_line": i}
            for i in range(1, 5)
        ],
        weak_description=True,
    )
    # Threshold 40: score 48 -> ready, passes required gate
    rep_ready = compute_readiness(REPO, 101, SHA, sig_48, score_threshold=40)
    assert rep_ready.score == 48
    assert rep_ready.band == "ready"
    assert band_for_score(48, has_blocker=False, score_threshold=40) == "ready"

    # Threshold 40: score 39 -> blocked
    assert band_for_score(39, has_blocker=False, score_threshold=40) == "blocked"
    # Hard blocker with threshold 40 -> STILL blocked (score capped at 49, has_blocker=True)
    rep_blocked = compute_readiness(
        REPO, 102, SHA, MergeSignals(failing_checks=[{"name": "ci"}]), score_threshold=40
    )
    assert rep_blocked.band == "blocked"

    # Also verify a report banded "blocked" (e.g., computed with threshold 85 -> score 48 blocked)
    # can NEVER pass the gate even if publish_readiness is called with threshold=40.
    rep_48_blocked = compute_readiness(REPO, 103, SHA, sig_48, score_threshold=85)
    assert rep_48_blocked.band == "blocked"

    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/check-runs":
            posted.append(json.loads(request.content.decode()))
            return httpx.Response(201, json={"id": 8000 + len(posted)})
        if request.method == "GET" and request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": 8100 + len(posted)})
        return httpx.Response(404)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            r_ok = await publish_readiness(
                REPO, 101, rep_ready, merge_gate="required", score_threshold=40,
                github_token="ghs_app", auth_kind="app", client=client,
            )
            r_blk = await publish_readiness(
                REPO, 102, rep_blocked, merge_gate="required", score_threshold=40,
                github_token="ghs_app", auth_kind="app", client=client,
            )
            r_mismatch = await publish_readiness(
                REPO, 103, rep_48_blocked, merge_gate="required", score_threshold=40,
                github_token="ghs_app", auth_kind="app", client=client,
            )
            return r_ok, r_blk, r_mismatch

    r_ok, r_blk, r_mismatch = asyncio.run(_run())
    assert r_ok["check_run"]["conclusion"] == "success"
    assert r_blk["check_run"]["conclusion"] == "failure"
    assert r_mismatch["check_run"]["conclusion"] == "failure"


def test_cancelled_and_stale_check_conclusions_block_readiness():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/pulls/104":
            return httpx.Response(200, json={
                "number": 104,
                "title": "feat: test checks",
                "body": "Detailed PR body describing changes cleanly.",
                "state": "open",
                "head": {"sha": SHA, "ref": "feat"},
                "base": {"sha": "0" * 40, "ref": "main"},
            })
        if path == f"/repos/{REPO}/commits/{SHA}/check-runs":
            return httpx.Response(200, json={
                "check_runs": [
                    {"id": 1, "name": "ci/cancelled-job", "status": "completed", "conclusion": "cancelled",
                     "app": {"slug": "github-actions"}},
                    {"id": 2, "name": "ci/stale-job", "status": "completed", "conclusion": "stale",
                     "app": {"slug": "github-actions"}},
                    {"id": 3, "name": "ci/ok", "status": "completed", "conclusion": "success",
                     "app": {"slug": "github-actions"}},
                ]
            })
        if path == f"/repos/{REPO}/commits/{SHA}/status":
            return httpx.Response(200, json={"statuses": []})
        if path == f"/repos/{REPO}/pulls/104/reviews":
            return httpx.Response(200, json=[])
        if path == f"/repos/{REPO}/pulls/104/files":
            return httpx.Response(200, json=[])
        if "/compare/" in path:
            return httpx.Response(200, json={"behind_by": 0})
        return httpx.Response(404)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_signals(REPO, 104, github_token="ghs_test", client=client)

    _, sig = asyncio.run(_run())
    assert [c["name"] for c in sig.failing_checks] == ["ci/cancelled-job", "ci/stale-job"]
    rep = compute_readiness(REPO, 104, SHA, sig)
    assert rep.band == "blocked"
    assert rep.score <= 49


def test_collect_signals_paginates_check_runs_reviews_and_files():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        page = int(request.url.params.get("page", "1"))
        if path == f"/repos/{REPO}/pulls/105":
            return httpx.Response(200, json={
                "number": 105,
                "title": "feat: large PR",
                "body": "Detailed PR body with sufficient explanation of changes.",
                "state": "open",
                "head": {"sha": SHA, "ref": "feat/big"},
                "base": {"sha": "0" * 40, "ref": "main"},
            })
        if path == f"/repos/{REPO}/commits/{SHA}/check-runs":
            if page == 1:
                return httpx.Response(
                    200,
                    headers={"Link": f'<https://api.github.com{path}?page=2>; rel="next"'},
                    json={
                        "total_count": 101,
                        "check_runs": [
                            {"id": i, "name": f"job-{i}", "status": "completed", "conclusion": "success",
                             "app": {"slug": "github-actions"}}
                            for i in range(1, 101)
                        ],
                    },
                )
            return httpx.Response(
                200,
                headers={"Link": f'<https://api.github.com{path}?page=1>; rel="prev"'},
                json={
                    "total_count": 101,
                    "check_runs": [
                        {"id": 101, "name": "late-failing-check", "status": "completed", "conclusion": "failure",
                         "app": {"slug": "github-actions"}},
                    ],
                },
            )
        if path == f"/repos/{REPO}/commits/{SHA}/status":
            return httpx.Response(200, json={"statuses": []})
        if path == f"/repos/{REPO}/pulls/105/reviews":
            if page == 1:
                return httpx.Response(
                    200,
                    headers={"Link": f'<https://api.github.com{path}?page=2>; rel="next"'},
                    json=[{"user": {"login": "bob", "type": "User"}, "state": "COMMENTED"}] * 100,
                )
            return httpx.Response(
                200,
                json=[{"user": {"login": "carol", "type": "User"}, "state": "CHANGES_REQUESTED", "commit_id": SHA}],
            )
        if path == f"/repos/{REPO}/pulls/105/files":
            if page == 1:
                return httpx.Response(
                    200,
                    headers={"Link": f'<https://api.github.com{path}?page=2>; rel="next"'},
                    json=[{"filename": f"docs/f{i}.md", "additions": 1, "deletions": 0} for i in range(100)],
                )
            return httpx.Response(
                200,
                json=[{"filename": ".github/workflows/deploy.yml", "additions": 2, "deletions": 0, "patch": "+run: deploy"}],
            )
        if "/compare/" in path:
            return httpx.Response(200, json={"behind_by": 0})
        return httpx.Response(404)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_signals(REPO, 105, github_token="ghs_test", client=client)

    sha, sig = asyncio.run(_run())
    assert sha == SHA
    assert sig.head_verified is True
    assert sig.incomplete_signals == []
    assert [c["name"] for c in sig.failing_checks] == ["late-failing-check"]
    assert sig.changes_requested == 1
    assert sig.safety_flagged is True


def test_incomplete_signals_withhold_required_gate_success_but_allow_advisory():
    # Simulate /check-runs failing and /files hitting the pagination cap
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/repos/{REPO}/pulls/106":
            return httpx.Response(200, json={
                "number": 106,
                "title": "docs: readme",
                "body": "Updates documentation with thorough usage examples.",
                "state": "open",
                "head": {"sha": SHA, "ref": "docs"},
                "base": {"sha": "0" * 40, "ref": "main"},
            })
        if path == f"/repos/{REPO}/commits/{SHA}/check-runs":
            return httpx.Response(503, text="service unavailable")
        if path == f"/repos/{REPO}/commits/{SHA}/status":
            return httpx.Response(200, json={"statuses": []})
        if path == f"/repos/{REPO}/pulls/106/reviews":
            return httpx.Response(200, json=[])
        if path == f"/repos/{REPO}/pulls/106/files":
            # Always advertises rel="next" -> hits pagination cap
            return httpx.Response(
                200,
                headers={"Link": f'<https://api.github.com{path}?page=99>; rel="next"'},
                json=[{"filename": "docs/a.md", "additions": 1, "deletions": 0}] * 100,
            )
        if "/compare/" in path:
            return httpx.Response(200, json={"behind_by": 0})
        if request.method == "POST" and path == f"/repos/{REPO}/check-runs":
            return httpx.Response(201, json={"id": 8501})
        if request.method == "POST" and path == f"/repos/{REPO}/statuses/{SHA}":
            return httpx.Response(201, json={"id": 8502})
        if request.method == "GET" and path == f"/repos/{REPO}/issues/106/comments":
            return httpx.Response(200, json=[])
        if request.method == "POST" and path == f"/repos/{REPO}/issues/106/comments":
            return httpx.Response(201, json={"id": 8503})
        return httpx.Response(404)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            res_req = await recompute_pr_readiness(
                REPO, 106,
                agent_config={"merge_gate": "required", "score_threshold": 85},
                github_token="ghs_app", auth_kind="app", client=client,
            )
            res_adv = await recompute_pr_readiness(
                REPO, 106,
                agent_config={"merge_gate": "advisory", "score_threshold": 85},
                github_token="ghp_pat", auth_kind="pat", client=client,
            )
            return res_req, res_adv

    res_req, res_adv = asyncio.run(_run())
    assert "check_runs" in res_req["signals"]["incomplete_signals"]
    assert "files" in res_req["signals"]["incomplete_signals"]
    # Required gate MUST NOT publish success on incomplete signals
    assert res_req["check_run"]["conclusion"] == "failure"
    # Advisory still publishes partial report without blocking
    assert res_adv["commit_status"]["state"] == "success"
    assert "Advisory" in res_adv["commit_status"]["description"]


def test_unverified_pr_head_does_not_fallback_to_activity_sha_and_preserves_report():
    # Seed a previously verified report and an older review activity entry with a stale commit_id
    prev_report = compute_readiness(REPO, 107, SHA, MergeSignals())
    asyncio.run(publish_readiness(REPO, 107, prev_report, merge_gate="off", pr_meta={"title": "Verified PR"}))
    events.record_pr_activity(REPO, 107, {"kind": "review", "author": "alice", "state": "approved", "commit_id": "deadbeef" * 5})
    store.rset(dm.dirty_key(REPO, 107), "tok-107")

    writes: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method in ("POST", "PATCH"):
            writes.append((request.method, request.url.path))
        return httpx.Response(500, text="github down")

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await recompute_pr_readiness(
                REPO, 107,
                agent_config={"merge_gate": "required", "score_threshold": 85},
                github_token="ghs_app", auth_kind="app", client=client,
            )

    res = asyncio.run(_run())
    assert res["signals"]["head_verified"] is False
    assert "pr_metadata" in res["signals"]["incomplete_signals"]
    assert res["skipped"] == "unverified_head"
    assert res["published"] is False
    assert writes == []
    # Previously verified report and dirty flag are preserved
    stored = get_stored_readiness(REPO, 107)
    assert stored is not None and stored["head_sha"] == SHA
    assert store.rget(dm.dirty_key(REPO, 107)) == "tok-107"


def test_safety_flagged_requires_approval_for_current_head_sha():
    old_sha = "f" * 40
    pr_data = {
        "number": 108,
        "title": "ci: update workflow",
        "body": "Updates GitHub Actions workflow permissions cleanly.",
        "state": "open",
        "head": {"sha": SHA, "ref": "ci-update"},
        "base": {"ref": "main"},
    }
    diff_files = [{"filename": ".github/workflows/ci.yml", "additions": 2, "deletions": 1, "patch": "+run: test"}]

    review_batches: list[list[dict[str, Any]]] = [
        # 1. Approval on an OLDER commit -> MUST NOT satisfy safety approval
        [{"user": {"login": "alice", "type": "User"}, "state": "APPROVED", "commit_id": old_sha}],
        # 2. Approval on CURRENT head SHA -> satisfies safety approval
        [
            {"user": {"login": "alice", "type": "User"}, "state": "APPROVED", "commit_id": old_sha},
            {"user": {"login": "bob", "type": "User"}, "state": "APPROVED", "commit_id": SHA},
        ],
        # 3. Current head approval superseded by CHANGES_REQUESTED -> NOT approved
        [
            {"user": {"login": "bob", "type": "User"}, "state": "APPROVED", "commit_id": SHA},
            {"user": {"login": "bob", "type": "User"}, "state": "CHANGES_REQUESTED", "commit_id": SHA},
        ],
    ]

    results = []
    for reviews in review_batches:
        def handler(request: httpx.Request, _reviews=reviews) -> httpx.Response:
            path = request.url.path
            if path == f"/repos/{REPO}/commits/{SHA}/check-runs":
                return httpx.Response(200, json={"check_runs": []})
            if path == f"/repos/{REPO}/commits/{SHA}/status":
                return httpx.Response(200, json={"statuses": []})
            if path == f"/repos/{REPO}/pulls/108/reviews":
                return httpx.Response(200, json=_reviews)
            if "/compare/" in path:
                return httpx.Response(200, json={"behind_by": 0})
            return httpx.Response(404)

        async def _run(_handler=handler):
            async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
                return await collect_signals(
                    REPO, 108, github_token="ghs_test", pr_data=pr_data, diff_files=diff_files, client=client
                )

        _, sig = asyncio.run(_run())
        results.append(sig)

    assert results[0].safety_flagged is True
    assert results[0].approvals == 1
    assert results[0].safety_human_approved is False
    assert compute_readiness(REPO, 108, SHA, results[0]).band == "blocked"

    assert results[1].safety_flagged is True
    assert results[1].safety_human_approved is True
    assert compute_readiness(REPO, 108, SHA, results[1]).band == "ready"

    assert results[2].safety_human_approved is False


def test_shared_head_sha_commit_status_aggregates_strictest_open_pr():
    posted_statuses: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/statuses/{SHA}":
            posted_statuses.append(json.loads(request.content.decode()))
            return httpx.Response(201, json={"id": len(posted_statuses)})
        if request.method == "GET" and request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": 9100})
        if request.method == "PATCH" and "/issues/comments/" in request.url.path:
            return httpx.Response(200, json={"id": 9100})
        return httpx.Response(404)

    events.remember_head(REPO, 110, SHA)
    events.remember_head(REPO, 111, SHA)
    rep_fail = compute_readiness(REPO, 110, SHA, MergeSignals(failing_checks=[{"name": "ci"}]))
    rep_pass = compute_readiness(REPO, 111, SHA, MergeSignals())

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            r1 = await publish_readiness(
                REPO, 110, rep_fail, merge_gate="required", github_token="ghp_pat1", auth_kind="pat", client=client
            )
            # PR #111 passes, but shares SHA with failing open PR #110 -> MUST NOT overwrite with success!
            r2 = await publish_readiness(
                REPO, 111, rep_pass, merge_gate="required", github_token="ghp_pat1", auth_kind="pat", client=client
            )
            # Close PR #110 -> now PR #111 is the only open PR on SHA -> publishes success!
            mark_pr_closed(REPO, 110, merged=False)
            r3 = await publish_readiness(
                REPO, 111, rep_pass, merge_gate="required", github_token="ghp_pat1", auth_kind="pat", client=client
            )
            return r1, r2, r3

    r1, r2, r3 = asyncio.run(_run())
    assert r1["commit_status"]["state"] == "failure"
    assert r2["commit_status"]["state"] == "failure"
    assert "PR #110" in r2["commit_status"]["description"]
    assert r3["commit_status"]["state"] == "success"
    assert [p["state"] for p in posted_statuses] == ["failure", "failure", "success"]


def test_pat_commit_status_skips_unchanged_payloads():
    status_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/statuses/{SHA}":
            status_posts.append(json.loads(request.content.decode()))
            return httpx.Response(201, json={"id": len(status_posts)})
        if request.method == "GET" and request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": 9200})
        if request.method == "PATCH" and "/issues/comments/" in request.url.path:
            return httpx.Response(200, json={"id": 9200})
        return httpx.Response(404)

    rep_ready = compute_readiness(REPO, 112, SHA, MergeSignals())
    rep_fail = compute_readiness(REPO, 112, SHA, MergeSignals(failing_checks=[{"name": "ci"}]))

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            r1 = await publish_readiness(
                REPO, 112, rep_ready, merge_gate="required", github_token="ghp_pat2", auth_kind="pat", client=client
            )
            r2 = await publish_readiness(
                REPO, 112, rep_ready, merge_gate="required", github_token="ghp_pat2", auth_kind="pat", client=client
            )
            r3 = await publish_readiness(
                REPO, 112, rep_fail, merge_gate="required", github_token="ghp_pat2", auth_kind="pat", client=client
            )
            return r1, r2, r3

    r1, r2, r3 = asyncio.run(_run())
    assert r1["commit_status"]["action"] == "posted"
    assert r2["commit_status"]["action"] == "unchanged"
    assert r2["published"] is True
    assert r2["gate_published"] is True
    assert r3["commit_status"]["action"] == "posted"
    assert len(status_posts) == 2


def test_find_readiness_comment_verifies_author_in_app_and_pat_modes(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "pr-agent")
    patched_ids: list[int] = []

    def app_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/check-runs":
            return httpx.Response(201, json={"id": 9301})
        if request.method == "GET" and request.url.path == f"/repos/{REPO}/issues/113/comments":
            return httpx.Response(200, json=[
                {"id": 10, "user": {"login": "attacker", "type": "User"}, "body": f"{READINESS_MARKER}\nspoofed"},
                {"id": 11, "user": {"login": "other-bot[bot]", "type": "Bot"}, "body": f"{READINESS_MARKER}\nother bot"},
                {"id": 12, "user": {"login": "pr-agent[bot]", "type": "Bot"}, "body": f"{READINESS_MARKER}\nreal"},
            ])
        if request.method == "PATCH" and "/issues/comments/" in request.url.path:
            cid = int(request.url.path.rsplit("/", 1)[-1])
            patched_ids.append(cid)
            return httpx.Response(200, json={"id": cid})
        return httpx.Response(404)

    rep_app = compute_readiness(REPO, 113, SHA, MergeSignals())

    async def _run_app():
        async with httpx.AsyncClient(transport=httpx.MockTransport(app_handler)) as client:
            return await publish_readiness(
                REPO, 113, rep_app, merge_gate="advisory", github_token="ghs_app", auth_kind="app", client=client
            )

    res_app = asyncio.run(_run_app())
    assert res_app["comment"] == {"action": "updated", "id": 12, "status_code": 200}
    assert patched_ids == [12]

    # Now verify PAT mode checks authenticated GET /user login
    pat_patched: list[int] = []

    def pat_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/user":
            return httpx.Response(200, json={"login": "ci-pat-bot"})
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/statuses/{SHA}":
            return httpx.Response(201, json={"id": 9302})
        if request.method == "GET" and request.url.path == f"/repos/{REPO}/issues/114/comments":
            return httpx.Response(200, json=[
                {"id": 20, "user": {"login": "attacker", "type": "User"}, "body": f"{READINESS_MARKER}\nspoofed"},
                {"id": 21, "user": {"login": "ci-pat-bot", "type": "User"}, "body": f"{READINESS_MARKER}\nown"},
            ])
        if request.method == "PATCH" and "/issues/comments/" in request.url.path:
            cid = int(request.url.path.rsplit("/", 1)[-1])
            pat_patched.append(cid)
            return httpx.Response(200, json={"id": cid})
        return httpx.Response(404)

    rep_pat = compute_readiness(REPO, 114, SHA, MergeSignals())

    async def _run_pat():
        async with httpx.AsyncClient(transport=httpx.MockTransport(pat_handler)) as client:
            return await publish_readiness(
                REPO, 114, rep_pat, merge_gate="advisory", github_token="ghp_unique_pat_tok", auth_kind="pat", client=client
            )

    res_pat = asyncio.run(_run_pat())
    assert res_pat["comment"] == {"action": "updated", "id": 21, "status_code": 200}
    assert pat_patched == [21]


def test_incomplete_comment_lookup_skips_posting_duplicate_comment():
    comment_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/check-runs":
            return httpx.Response(201, json={"id": 9401})
        if request.method == "GET" and request.url.path == f"/repos/{REPO}/issues/115/comments":
            return httpx.Response(500, text="temporary error")
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/issues/115/comments":
            comment_posts.append(request.url.path)
            return httpx.Response(201, json={"id": 9402})
        return httpx.Response(404)

    rep = compute_readiness(REPO, 115, SHA, MergeSignals())

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_readiness(
                REPO, 115, rep, merge_gate="advisory", github_token="ghs_app", auth_kind="app", client=client
            )

    res = asyncio.run(_run())
    assert res["comment"] == {"action": "skipped", "reason": "comment_lookup_incomplete"}
    assert comment_posts == []


def test_dirty_flag_uses_atomic_compare_and_delete():
    dkey = dm.dirty_key(REPO, 116)
    store.rset(dkey, "tok-before")
    bumped = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bumped
        path = request.url.path
        if path == f"/repos/{REPO}/pulls/116":
            if not bumped:
                # Simulate a new webhook event arriving mid-refresh!
                store.rset(dkey, "tok-during")
                bumped = True
            return httpx.Response(200, json={
                "number": 116,
                "title": "fix: concurrency",
                "body": "Detailed PR body describing concurrency fix cleanly.",
                "state": "open",
                "head": {"sha": SHA, "ref": "fix"},
                "base": {"ref": "main"},
            })
        if path == f"/repos/{REPO}/commits/{SHA}/check-runs":
            return httpx.Response(200, json={"check_runs": []})
        if path == f"/repos/{REPO}/commits/{SHA}/status":
            return httpx.Response(200, json={"statuses": []})
        if path == f"/repos/{REPO}/pulls/116/reviews":
            return httpx.Response(200, json=[])
        if path == f"/repos/{REPO}/pulls/116/files":
            return httpx.Response(200, json=[])
        if "/compare/" in path:
            return httpx.Response(200, json={"behind_by": 0})
        if request.method == "POST" and path == f"/repos/{REPO}/check-runs":
            return httpx.Response(201, json={"id": 9501})
        if request.method == "PATCH" and "/check-runs/" in path:
            return httpx.Response(200, json={"id": 9501})
        if request.method == "GET" and path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={"id": 9502})
        if request.method == "PATCH" and "/issues/comments/" in path:
            return httpx.Response(200, json={"id": 9502})
        return httpx.Response(404)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await recompute_pr_readiness(
                REPO, 116, agent_config={"merge_gate": "required"}, github_token="ghs_app", auth_kind="app", client=client
            )
            after_first = store.rget(dkey)
            # Second run has no concurrent mutation -> dirty key IS cleared
            await recompute_pr_readiness(
                REPO, 116, agent_config={"merge_gate": "required"}, github_token="ghs_app", auth_kind="app", client=client
            )
            after_second = store.rget(dkey)
            return after_first, after_second

    after_first, after_second = asyncio.run(_run())
    assert after_first == "tok-during"
    assert after_second is None


def test_stale_snapshot_cannot_overwrite_newer_head_or_closed_inbox():
    sha_old = "1" * 40
    sha_new = "2" * 40

    rep_new = compute_readiness(REPO, 117, sha_new, MergeSignals())
    rep_old = compute_readiness(REPO, 117, sha_old, MergeSignals(failing_checks=[{"name": "ci"}]))

    async def _run():
        # Publish newer snapshot first
        r_new = await publish_readiness(
            REPO, 117, rep_new, merge_gate="off",
            pr_meta={"title": "Newer head", "state": "open"},
            snapshot_at="2026-11-21T10:00:05+00:00",
        )
        # Late-arriving older snapshot for older head MUST be rejected
        r_stale_head = await publish_readiness(
            REPO, 117, rep_old, merge_gate="off",
            pr_meta={"title": "Older head", "state": "open"},
            snapshot_at="2026-11-21T10:00:01+00:00",
        )
        # Mark PR closed as merged, then attempt to overwrite back to open
        mark_pr_closed(REPO, 117, merged=True)
        r_stale_merged = await publish_readiness(
            REPO, 117, rep_new, merge_gate="off",
            pr_meta={"title": "Stale open", "state": "open"},
            snapshot_at="2099-01-01T00:00:00+00:00",
        )
        return r_new, r_stale_head, r_stale_merged

    r_new, r_stale_head, r_stale_merged = asyncio.run(_run())
    assert r_new["inbox"]["head_sha"] == sha_new
    assert r_stale_head["stale"] is True
    assert get_stored_readiness(REPO, 117)["head_sha"] == sha_new
    assert r_stale_merged["stale"] is True
    assert list_inbox_prs(repo=REPO, state="open") == []
    assert [p["pr_number"] for p in list_inbox_prs(repo=REPO, state="merged")] == [117]


def test_required_gate_write_failure_keeps_dirty_flag_and_bounded_retries():
    from merge import publisher as merge_pub

    dkey = dm.dirty_key(REPO, 118)
    store.rset(dkey, "tok-118")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/check-runs":
            return httpx.Response(500, text="check run API error")
        if request.method == "GET" and request.url.path == f"/repos/{REPO}/issues/118/comments":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/issues/118/comments":
            return httpx.Response(201, json={"id": 9601})
        if request.method == "PATCH" and "/issues/comments/" in request.url.path:
            return httpx.Response(200, json={"id": 9601})
        return httpx.Response(404)

    rep = compute_readiness(REPO, 118, SHA, MergeSignals())

    async def _run_once():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await publish_readiness(
                REPO, 118, rep, merge_gate="required", github_token="ghs_app", auth_kind="app", client=client
            )

    # First failure: comment succeeded, check run failed -> published MUST be False and dirty kept!
    res1 = asyncio.run(_run_once())
    assert res1["comment"]["action"] == "created"
    assert res1["check_run"]["action"] == "failed"
    assert res1["gate_published"] is False
    assert res1["published"] is False
    assert store.rget(dkey) == "tok-118"

    # Exhaust retry budget -> dirty flag is eventually cleared so permanent errors don't spin forever
    for _ in range(merge_pub._MAX_PUBLISH_RETRIES):
        res_n = asyncio.run(_run_once())
        assert res_n["published"] is False
    assert store.rget(dkey) is None


