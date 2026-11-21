"""Tests for repo-specific memory service (CRUD, LRU cap, retrieval,
dismissal learning, repo bootstrap, merged-PR extraction, and Markdown export)."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest
from pydantic import ValidationError

import datamodel as dm
import memory
import store
from memory import service as mem_svc

REPO = "Acme/Widget"


def _b64_payload(text: str) -> dict:
    return {
        "encoding": "base64",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


class _Resp:
    def __init__(self, status_code: int = 200, *, data=None, text: str = ""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


# ---------------------------------------------------------------------------
# CRUD + export / import
# ---------------------------------------------------------------------------


def test_memory_crud_lifecycle():
    created = memory.add_memory(
        REPO,
        dm.MemoryEntry(
            kind="learning",
            text="  Always use Result<T, AppError> in Rust modules.  ",
            scope_globs=["src-tauri/**"],
            source="manual",
            author="alice",
            confidence=0.9,
            blocking=True,
            status="pending",
        ),
    )
    mid = created["id"]
    assert len(mid) == 10 and all(c in "0123456789abcdef" for c in mid)  # nosec B101
    assert created["text"] == "Always use Result<T, AppError> in Rust modules."  # nosec B101
    assert created["created_at"]  # nosec B101
    assert created["status"] == "pending"  # nosec B101

    # Case-insensitive repo lookup
    fetched = memory.get_memory("acme/widget", mid)
    assert fetched is not None and fetched["id"] == mid  # nosec B101

    # Filter by status & kind
    assert len(memory.list_memories(REPO, status="pending")) == 1  # nosec B101
    assert memory.list_memories(REPO, status="active") == []  # nosec B101
    assert len(memory.list_memories(REPO, kind="learning")) == 1  # nosec B101
    assert memory.list_memories(REPO, kind="repo_fact") == []  # nosec B101

    # Approve sets status="active"
    approved = memory.approve_memory(REPO, mid)
    assert approved is not None and approved["status"] == "active"  # nosec B101

    # Update mutable fields; immutable fields ignored; invalid values rejected
    updated = memory.update_memory(
        REPO,
        mid,
        {
            "text": "Use AppError instead of unwrap() in src-tauri.",
            "scope_globs": ["src-tauri/src/**/*.rs"],
            "confidence": 0.95,
            "blocking": False,
            "id": "hijacked",
            "hits": 999,
            "created_at": "1970-01-01T00:00:00+00:00",
        },
    )
    assert updated is not None  # nosec B101
    assert updated["id"] == mid  # nosec B101
    assert updated["hits"] == 0  # nosec B101
    assert updated["created_at"] == created["created_at"]  # nosec B101
    assert updated["blocking"] is False and updated["confidence"] == 0.95  # nosec B101

    with pytest.raises(ValidationError):
        memory.update_memory(REPO, mid, {"status": "bogus"})

    assert memory.delete_memory(REPO, mid) is True  # nosec B101
    assert memory.delete_memory(REPO, mid) is False  # nosec B101
    assert memory.get_memory(REPO, mid) is None  # nosec B101
    assert memory.get_memory(REPO, "../bad") is None  # nosec B101

    with pytest.raises(ValueError):
        memory.list_memories("not-a-repo")


def test_export_and_import_deduplicates_and_skips_invalid():
    m1 = memory.add_memory(REPO, {"text": "Prefer pathlib over os.path", "scope_globs": ["backend/**"]})
    m2 = memory.add_memory(REPO, {"text": "Never log bearer tokens", "blocking": True})
    exported = memory.export_memories(REPO)
    assert {m["id"] for m in exported} == {m1["id"], m2["id"]}  # nosec B101

    # Re-importing the exact same items into the same repo skips all duplicates.
    res_same = memory.import_memories(REPO, exported)
    assert res_same == {"imported": 0, "skipped": 2}  # nosec B101

    # Importing into a fresh repo with duplicates & malformed rows.
    other = "acme/other"
    payload = exported + [
        {"text": "  prefer   pathlib over os.path ", "scope_globs": ["backend/**"]},  # dup normalized
        {"text": ""},  # invalid empty
        "not-a-dict",  # invalid type
        {"text": "Use explicit httpx timeouts", "kind": "learning"},  # valid new
    ]
    res_other = memory.import_memories(other, payload)
    assert res_other == {"imported": 3, "skipped": 3}  # nosec B101
    assert len(memory.list_memories(other)) == 3  # nosec B101


# ---------------------------------------------------------------------------
# LRU cap pruning
# ---------------------------------------------------------------------------


def test_lru_cap_prunes_oldest_unused_non_blocking_first(monkeypatch):
    monkeypatch.setattr(dm, "MEMORY_MAX_ENTRIES_PER_REPO", 4)

    # 1. Oldest blocking entry (unused) — MUST survive!
    blocking_old = memory.add_memory(
        REPO,
        {
            "text": "Blocking security rule",
            "blocking": True,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    # 2. Oldest unused non-blocking entry — MUST be evicted first!
    unused_oldest = memory.add_memory(
        REPO,
        {
            "text": "Oldest unused non-blocking",
            "blocking": False,
            "created_at": "2026-01-02T00:00:00+00:00",
        },
    )
    # 3. Older used non-blocking entry — MUST survive over unused!
    used_entry = memory.add_memory(
        REPO,
        {
            "text": "Used non-blocking rule",
            "blocking": False,
            "hits": 5,
            "last_used": "2026-06-01T00:00:00+00:00",
            "created_at": "2026-01-03T00:00:00+00:00",
        },
    )
    # 4. Newer unused non-blocking entry
    unused_newer = memory.add_memory(
        REPO,
        {
            "text": "Newer unused non-blocking",
            "blocking": False,
            "created_at": "2026-05-01T00:00:00+00:00",
        },
    )
    assert len(memory.list_memories(REPO)) == 4  # nosec B101

    # 5th add triggers eviction of `unused_oldest`
    fifth = memory.add_memory(
        REPO,
        {
            "text": "Brand new rule",
            "blocking": False,
            "created_at": "2026-07-01T00:00:00+00:00",
        },
    )
    remaining_ids = {m["id"] for m in memory.list_memories(REPO)}
    assert len(remaining_ids) == 4  # nosec B101
    assert unused_oldest["id"] not in remaining_ids  # nosec B101
    assert blocking_old["id"] in remaining_ids  # nosec B101
    assert used_entry["id"] in remaining_ids  # nosec B101
    assert unused_newer["id"] in remaining_ids  # nosec B101
    assert fifth["id"] in remaining_ids  # nosec B101

    # 6th add evicts `unused_newer`, keeping `blocking_old` and `used_entry`
    sixth = memory.add_memory(
        REPO,
        {
            "text": "Sixth rule",
            "blocking": False,
            "created_at": "2026-08-01T00:00:00+00:00",
        },
    )
    remaining_ids_2 = {m["id"] for m in memory.list_memories(REPO)}
    assert len(remaining_ids_2) == 4  # nosec B101
    assert unused_newer["id"] not in remaining_ids_2  # nosec B101
    assert blocking_old["id"] in remaining_ids_2  # nosec B101
    assert used_entry["id"] in remaining_ids_2  # nosec B101
    assert sixth["id"] in remaining_ids_2  # nosec B101


# ---------------------------------------------------------------------------
# Scoped retrieval, ranking, budget & hit counters
# ---------------------------------------------------------------------------


def test_retrieve_for_diff_globs_ranking_budget_and_hits():
    # Blocking rule matching backend/**
    m_block = memory.add_memory(
        REPO,
        {
            "text": "Never expose raw webhook secrets in responses.",
            "scope_globs": ["backend/**"],
            "blocking": True,
            "confidence": 0.7,
        },
    )
    # High keyword overlap with diff
    m_kw = memory.add_memory(
        REPO,
        {
            "text": "Validate redis_pipeline timeout and retry backoff on socket errors.",
            "scope_globs": ["backend/**/*.py"],
            "confidence": 0.8,
        },
    )
    # Generic low-relevance rule
    m_generic = memory.add_memory(
        REPO,
        {
            "text": "Keep documentation headings in sentence case.",
            "scope_globs": ["**"],
            "confidence": 0.5,
            "created_at": "2024-01-01T00:00:00+00:00",
        },
    )
    # Non-matching glob (should NOT be returned)
    m_rust = memory.add_memory(
        REPO,
        {
            "text": "Avoid unwrap() in Rust commands.",
            "scope_globs": ["src-tauri/**"],
            "blocking": True,
        },
    )
    # Pending entry (should NOT be returned)
    m_pending = memory.add_memory(
        REPO,
        {
            "text": "Pending redis_pipeline hint.",
            "scope_globs": ["backend/**"],
            "status": "pending",
        },
    )
    # Active dismissed pattern matching Python files
    m_dismiss = memory.add_memory(
        REPO,
        {
            "kind": "dismissed_pattern",
            "text": "Do not flag [style]: bare assert in pytest test helpers",
            "scope_globs": ["**/*.py"],
            "confidence": 0.9,
        },
    )

    diff = (
        "diff --git a/backend/cache.py b/backend/cache.py\n"
        "+def run_redis_pipeline(timeout, backoff):\n"
        "+    # handle socket errors\n"
    )
    res = memory.retrieve_for_diff(REPO, ["backend/cache.py"], diff_text=diff, max_chars=6000)

    rule_ids = [r["id"] for r in res["rules"]]
    dismissed_ids = [d["id"] for d in res["dismissed"]]
    assert rule_ids[0] == m_block["id"]  # blocking rule ranks first  # nosec B101
    assert rule_ids[1] == m_kw["id"]  # keyword-overlap + specific glob beats generic  # nosec B101
    assert rule_ids[2] == m_generic["id"]  # nosec B101
    assert m_rust["id"] not in rule_ids  # nosec B101
    assert m_pending["id"] not in rule_ids  # nosec B101
    assert dismissed_ids == [m_dismiss["id"]]  # nosec B101
    assert set(res["used_ids"]) == {m_block["id"], m_kw["id"], m_generic["id"], m_dismiss["id"]}  # nosec B101

    # Hit counters & last_used incremented only for returned entries
    for mid in res["used_ids"]:
        saved = memory.get_memory(REPO, mid)
        assert saved is not None and saved["hits"] == 1 and saved["last_used"] is not None  # nosec B101
    assert memory.get_memory(REPO, m_rust["id"])["hits"] == 0  # type: ignore[index]  # nosec B101
    assert memory.get_memory(REPO, m_pending["id"])["hits"] == 0  # type: ignore[index]  # nosec B101

    # Budget cap drops lower-ranked entries cleanly
    tight = memory.retrieve_for_diff(REPO, ["backend/cache.py"], diff_text=diff, max_chars=120)
    assert len(tight["rules"]) == 1 and tight["rules"][0]["id"] == m_block["id"]  # nosec B101
    assert memory.get_memory(REPO, m_block["id"])["hits"] == 2  # type: ignore[index]  # nosec B101

    # Empty changed_paths returns empty without incrementing hits
    empty = memory.retrieve_for_diff(REPO, [], diff_text=diff)
    assert empty == {"rules": [], "dismissed": [], "used_ids": []}  # nosec B101


# ---------------------------------------------------------------------------
# Implicit & explicit dismissal learning
# ---------------------------------------------------------------------------


def test_record_finding_dismissal_implicit_second_and_explicit_first():
    # Seed a stored finding in Redis for PR #10
    fp1 = dm.finding_fingerprint("src/api.py", "print(x)", "style", "Avoid print debugging statements")
    finding1 = {
        "file": "src/api.py",
        "start_line": 12,
        "end_line": 12,
        "severity": "nit",
        "category": "style",
        "title": "Avoid print debugging statements",
        "state": "open",
        "fingerprint": fp1,
    }
    store.rset_json(dm.finding_key(REPO, 10, fp1), finding1, ex=dm.FINDING_TTL_S)

    # 1st implicit dismissal (no reason): marks finding dismissed, increments counter with TTL, NO pattern yet
    r1 = memory.record_finding_dismissal(REPO, 10, fp1, author="alice")
    assert r1["finding_updated"] is True  # nosec B101
    assert r1["already_dismissed"] is False  # nosec B101
    assert r1["dismissal_count"] == 1  # nosec B101
    assert r1["pattern_created"] is False  # nosec B101
    assert r1["memory"] is None  # nosec B101
    stored = store.rget_json(dm.finding_key(REPO, 10, fp1))
    assert isinstance(stored, dict) and stored["state"] == "dismissed"  # nosec B101
    counter_key = f"memdismiss:{REPO.lower()}:{r1['stem_hash']}"
    counter_doc = store.rget_json(counter_key)
    assert isinstance(counter_doc, dict) and counter_doc["count"] == 1 and counter_doc["updated_at"]  # nosec B101
    assert store._MEM[counter_key][1] is not None  # TTL applied  # nosec B101

    # Duplicate dismissal of the SAME already-dismissed finding must NOT double-count or promote!
    r1_dup = memory.record_finding_dismissal(REPO, 10, fp1, author="alice")
    assert r1_dup["already_dismissed"] is True  # nosec B101
    assert r1_dup["dismissal_count"] == 1  # nosec B101
    assert r1_dup["pattern_created"] is False  # nosec B101
    assert r1_dup["memory"] is None  # nosec B101

    # 2nd implicit dismissal of the SAME stem on PR #11 -> creates dismissed_pattern!
    fp2 = dm.finding_fingerprint("src/cli.py", "print(y)", "style", "Avoid print debugging statements")
    r2 = memory.record_finding_dismissal(
        REPO,
        11,
        fp2,
        finding={"file": "src/cli.py", "category": "style", "title": "Avoid print debugging statements"},
        author="bob",
    )
    assert r2["dismissal_count"] == 2  # nosec B101
    assert r2["pattern_created"] is True  # nosec B101
    assert r2["memory"] is not None and r2["memory"]["kind"] == "dismissed_pattern"  # nosec B101
    assert r2["memory"]["status"] == "active"  # nosec B101

    # 3rd dismissal boosts existing pattern confidence
    conf_before = r2["memory"]["confidence"]
    r3 = memory.record_finding_dismissal(
        REPO,
        12,
        fp2,
        finding={"file": "src/cli.py", "category": "style", "title": "Avoid print debugging statements"},
        reason="CLI tools write to stdout intentionally",
        author="bob",
    )
    assert r3["dismissal_count"] == 3  # nosec B101
    assert r3["pattern_boosted"] is True  # nosec B101
    assert r3["memory"]["confidence"] > conf_before  # nosec B101
    assert "CLI tools write to stdout intentionally" in r3["memory"]["text"]  # nosec B101

    # New PR touching another .py file retrieves this dismissed pattern!
    ret = memory.retrieve_for_diff(REPO, ["src/other.py"], diff_text="print('ok')")
    assert any(d["id"] == r2["memory"]["id"] for d in ret["dismissed"])  # nosec B101

    # Explicit reason on 1st dismissal creates dismissed_pattern immediately!
    fp_exp = dm.finding_fingerprint("backend/auth.py", "sha1", "security", "Legacy hash algorithm")
    r_exp = memory.record_finding_dismissal(
        REPO,
        15,
        fp_exp,
        finding={"file": "backend/auth.py", "category": "security", "title": "Legacy hash algorithm"},
        reason="Used only for non-cryptographic cache keys",
        author="alice",
    )
    assert r_exp["dismissal_count"] == 1  # nosec B101
    assert r_exp["pattern_created"] is True  # nosec B101
    assert r_exp["memory"] is not None  # nosec B101
    assert "non-cryptographic cache keys" in r_exp["memory"]["text"]  # nosec B101


def test_duplicate_dismissal_allows_new_explicit_reason_without_incrementing():
    fp = dm.finding_fingerprint("src/app.py", "eval(x)", "security", "Avoid dynamic eval")
    store.rset_json(
        dm.finding_key(REPO, 20, fp),
        {"fingerprint": fp, "file": "src/app.py", "category": "security", "title": "Avoid dynamic eval", "state": "open"},
        ex=dm.FINDING_TTL_S,
    )
    first = memory.record_finding_dismissal(REPO, 20, fp, author="alice")
    assert first["dismissal_count"] == 1 and first["pattern_created"] is False  # nosec B101

    # Re-dismissing the same finding with an explicit reason creates the pattern without incrementing count
    with_reason = memory.record_finding_dismissal(
        REPO, 20, fp, reason="Sandboxed AST evaluator only", author="alice"
    )
    assert with_reason["already_dismissed"] is True  # nosec B101
    assert with_reason["dismissal_count"] == 1  # nosec B101
    assert with_reason["pattern_created"] is True  # nosec B101
    assert "Sandboxed AST evaluator only" in with_reason["memory"]["text"]  # nosec B101

    # Re-dismissing again without a new reason does NOT boost confidence again
    conf_before = with_reason["memory"]["confidence"]
    again = memory.record_finding_dismissal(REPO, 20, fp, author="alice")
    assert again["already_dismissed"] is True and again["pattern_boosted"] is False  # nosec B101
    assert again["memory"]["confidence"] == conf_before  # nosec B101


# ---------------------------------------------------------------------------
# Repo bootstrap (mocked GitHub Contents API)
# ---------------------------------------------------------------------------


def test_bootstrap_repo_extracts_profile_and_deduplicated_facts(monkeypatch):
    files = {
        "package.json": json.dumps({
            "name": "widget-ui",
            "packageManager": "pnpm@9.0.0",
            "dependencies": {"react": "^18.0.0", "@tauri-apps/api": "^2.0.0"},
            "devDependencies": {"typescript": "^5.0.0", "vite": "^5.0.0", "vitest": "^2.0.0"},
            "scripts": {"test": "vitest run", "build": "vite build", "typecheck": "tsc --noEmit"},
        }),
        "pyproject.toml": (
            "[project]\n"
            "name = 'widget-backend'\n"
            "dependencies = ['fastapi>=0.115', 'pydantic>=2', 'httpx>=0.27']\n"
            "[tool.pytest.ini_options]\n"
            "testpaths = ['tests']\n"
            "[tool.ruff]\n"
            "line-length = 100\n"
        ),
        "Cargo.toml": (
            "[package]\nname = 'widget-desktop'\n"
            "[dependencies]\ntauri = '2'\nserde = '1'\ntokio = '1'\n"
        ),
        ".editorconfig": (
            "[*]\nindent_style = space\nindent_size = 2\nend_of_line = lf\n"
            "[*.{py,rs}]\nindent_size = 4\n"
        ),
        "AGENTS.md": (
            "# Agent Rules\n"
            "- Never interpolate PR titles directly into GitHub Actions run blocks.\n"
            "- Always run pytest before opening a pull request.\n"
            "- [TOC](#toc)\n"
        ),
        "CONTRIBUTING.md": (
            "## Guidelines\n"
            "- Keep backend dependencies minimal; prefer Python standard library.\n"
        ),
        "CLAUDE.md": (
            "- Always verify webhook HMAC signatures before parsing payloads.\n"
        ),
    }
    requested_urls: list[str] = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            requested_urls.append(url)
            assert headers and headers.get("Authorization") == "Bearer gh-inst-token"  # nosec B101
            path = url.split("/contents/", 1)[-1]
            if path in files:
                return _Resp(200, data=_b64_payload(files[path]))
            return _Resp(404, data={"message": "Not Found"})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-inst-token")

    res = asyncio.run(memory.bootstrap_repo(REPO))
    profile = res["profile"]
    assert set(profile["languages"]) >= {"TypeScript", "JavaScript", "Python", "Rust"}  # nosec B101
    assert {"React", "Tauri", "FastAPI", "Pydantic", "Serde", "Tokio"} <= set(profile["frameworks"])  # nosec B101
    assert "pnpm test" in profile["test_commands"]  # nosec B101
    assert "pytest" in profile["test_commands"]  # nosec B101
    assert "cargo test" in profile["test_commands"]  # nosec B101
    assert "pnpm run build" in profile["build_commands"]  # nosec B101
    assert "cargo build" in profile["build_commands"]  # nosec B101
    assert len(requested_urls) == len(memory.BOOTSTRAP_FILES)  # nosec B101

    # Profile persisted in Redis at repo:{repo}:profile
    saved_profile = memory.get_repo_profile(REPO)
    assert saved_profile == profile  # nosec B101

    # Facts created with source="bootstrap"
    assert len(res["created"]) >= 5  # nosec B101
    assert all(m["kind"] == "repo_fact" and m["source"] == "bootstrap" for m in res["created"])  # nosec B101
    all_texts = "\n".join(m["text"] for m in res["created"])
    assert "Never interpolate PR titles" in all_texts  # nosec B101
    assert "Always verify webhook HMAC signatures" in all_texts  # nosec B101
    assert "EditorConfig" in all_texts  # nosec B101
    assert "[TOC]" not in all_texts  # nosec B101

    # Re-running bootstrap deduplicates everything
    res2 = asyncio.run(memory.bootstrap_repo(REPO))
    assert res2["created"] == []  # nosec B101
    assert res2["skipped"] == len(res["created"])  # nosec B101


def test_bootstrap_repo_accepts_explicit_token_without_token_lookup(monkeypatch):
    seen_auth: list[str] = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            seen_auth.append((headers or {}).get("Authorization", ""))
            return _Resp(404, data={})

    import github_app
    import httpx

    async def _boom(_repo):
        raise AssertionError("token_for_repo should not be called when github_token is provided")

    monkeypatch.setattr(github_app, "token_for_repo", _boom)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    res = asyncio.run(memory.bootstrap_repo("acme/empty", github_token="explicit-tok"))
    assert res["profile"]["files_indexed"] == []  # nosec B101
    assert seen_auth and all(a == "Bearer explicit-tok" for a in seen_auth)  # nosec B101


# ---------------------------------------------------------------------------
# Markdown rendering for Actions-lite mode
# ---------------------------------------------------------------------------


def test_render_memory_markdown_includes_active_only_and_profile():
    empty_md = memory.render_memory_markdown("empty/repo")
    assert "No active repo memories" in empty_md  # nosec B101

    store.rset_json(
        dm.repo_profile_key(REPO),
        {
            "languages": ["Python", "Rust"],
            "frameworks": ["FastAPI", "Tauri"],
            "test_commands": ["pytest", "cargo test"],
            "build_commands": ["cargo build"],
        },
    )
    m_block = memory.add_memory(REPO, {"text": "Never use unwrap() in Tauri IPC.", "blocking": True, "scope_globs": ["src-tauri/**"]})
    m_learn = memory.add_memory(REPO, {"text": "Use UTC timestamps everywhere.", "kind": "learning"})
    m_fact = memory.add_memory(REPO, {"text": "State lives in Redis.", "kind": "repo_fact"})
    m_dis = memory.add_memory(REPO, {"text": "Ignore assert statements in tests.", "kind": "dismissed_pattern", "scope_globs": ["**/tests/**"]})
    memory.add_memory(REPO, {"text": "Pending secret rule should not render.", "status": "pending"})
    memory.add_memory(REPO, {"text": "Archived old rule should not render.", "status": "archived"})

    md = memory.render_memory_markdown(REPO)
    assert "## Repository Profile" in md and "`pytest`" in md  # nosec B101
    assert "## Blocking Rules" in md and m_block["id"] in md  # nosec B101
    assert "## Repository Learnings" in md and m_learn["id"] in md  # nosec B101
    assert "## Repository Facts" in md and m_fact["id"] in md  # nosec B101
    assert "## Known False Positives" in md and m_dis["id"] in md  # nosec B101
    assert "Pending secret rule" not in md  # nosec B101
    assert "Archived old rule" not in md  # nosec B101


# ---------------------------------------------------------------------------
# Merged-PR candidate extraction
# ---------------------------------------------------------------------------


def test_extract_learnings_from_merged_pr_pending_and_auto_approve():
    comments = [
        {"author": "alice", "path": "backend/store.py", "body": "Always set a TTL on temporary Redis keys so free-tier memory never fills up."},
        {"author": "bob", "body": "@pr-agent remember Prefer httpx.AsyncClient context managers for backend requests."},
        {"author": "carol", "body": "LGTM!"},
        {"author": "dependabot[bot]", "body": "Always bump minor dependencies weekly."},
        {"author": "dave", "body": "Why did we rename this?"},
    ]

    pending = memory.extract_learnings_from_merged_pr(REPO, 42, comments, auto_approve=False)
    assert len(pending) == 2  # nosec B101
    assert all(m["status"] == "pending" and m["source"] == "merged_outcome" for m in pending)  # nosec B101
    assert pending[0]["scope_globs"] == ["backend/store.py"]  # nosec B101
    assert pending[1]["scope_globs"] == ["**"]  # nosec B101
    assert pending[1]["text"].startswith("Prefer httpx.AsyncClient")  # nosec B101

    # Re-running on same comments produces no duplicates
    assert memory.extract_learnings_from_merged_pr(REPO, 42, comments, auto_approve=True) == []  # nosec B101

    # Auto-approve mode on a fresh repo saves directly as active
    active = memory.extract_learnings_from_merged_pr("acme/auto", 7, comments, auto_approve=True)
    assert len(active) == 2 and all(m["status"] == "active" for m in active)  # nosec B101
