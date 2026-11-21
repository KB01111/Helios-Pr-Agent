"""Tests for repo-specific memory service (CRUD, LRU cap, retrieval,
dismissal learning, repo bootstrap, merged-PR extraction, and Markdown export)."""
from __future__ import annotations

import asyncio
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

import datamodel as dm
import memory
import store

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
    assert res_same == {"imported": 0, "skipped": 2, "failed": 0, "errors": []}  # nosec B101

    # Importing into a fresh repo with duplicates & malformed rows.
    other = "acme/other"
    payload = exported + [
        {"text": "  prefer   pathlib over os.path ", "scope_globs": ["backend/**"]},  # dup normalized
        {"text": ""},  # invalid empty
        "not-a-dict",  # invalid type
        {"text": "Use explicit httpx timeouts", "kind": "learning"},  # valid new
    ]
    res_other = memory.import_memories(other, payload)
    assert res_other == {"imported": 3, "skipped": 3, "failed": 0, "errors": []}  # nosec B101
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
    assert store.rget_json(counter_key) == 1  # nosec B101
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
            if url.endswith("/contents"):
                return _Resp(200, data=[])
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



# ---------------------------------------------------------------------------
# PR #18 review fixes: validation, locking, batching, idempotency, bootstrap, globs
# ---------------------------------------------------------------------------


def test_valid_memory_id_export():
    assert memory.valid_memory_id("abc123_-Z") is True  # nosec B101
    assert memory.valid_memory_id("dp_1234567890ab") is True  # nosec B101
    assert memory.valid_memory_id("") is False  # nosec B101
    assert memory.valid_memory_id("../bad") is False  # nosec B101
    assert memory.valid_memory_id("has space") is False  # nosec B101
    assert memory.valid_memory_id("a" * 65) is False  # nosec B101
    assert memory.valid_memory_id(None) is False  # type: ignore[arg-type]  # nosec B101


def test_update_memory_preserves_concurrent_edits_to_different_fields(monkeypatch):
    created = memory.add_memory(
        REPO,
        {"text": "Original rule text", "blocking": False, "confidence": 0.7},
    )
    mid = created["id"]
    mem_key = dm.memory_key(REPO, mid)
    lock_key = f"memlock:{REPO.lower()}:{mid}"
    first_in_lock = threading.Event()
    second_contended = threading.Event()
    second_read_while_first_held = threading.Event()
    first_holder_tid: list[int] = []
    first_lock_active = threading.Event()

    orig_rget_json = store.rget_json
    orig_rset = store.rset

    def _tracking_rset(key: str, value: str, ex=None, nx: bool = False):
        ok = orig_rset(key, value, ex=ex, nx=nx)
        if key == lock_key and nx and not ok:
            second_contended.set()
        return ok

    def _guarded_rget_json(key: str):
        val = orig_rget_json(key)
        if key == mem_key:
            tid = threading.get_ident()
            if not first_in_lock.is_set():
                first_holder_tid.append(tid)
                first_lock_active.set()
                first_in_lock.set()
                # Wait deterministically until the second worker hits the contested key lock.
                assert second_contended.wait(timeout=2.0)  # nosec B101
                assert not second_read_while_first_held.is_set()  # nosec B101
                first_lock_active.clear()
            elif first_lock_active.is_set() and tid not in first_holder_tid:
                second_read_while_first_held.set()
        return val

    monkeypatch.setattr(store, "rset", _tracking_rset)
    monkeypatch.setattr(store, "rget_json", _guarded_rget_json)

    def _patch_blocking():
        return memory.update_memory(REPO, mid, {"blocking": True})

    def _patch_text():
        assert first_in_lock.wait(timeout=2.0)  # nosec B101
        return memory.update_memory(REPO, mid, {"text": "Updated rule text"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_patch_blocking)
        f2 = pool.submit(_patch_text)
        f1.result()
        f2.result()

    assert second_contended.is_set()  # nosec B101
    assert not second_read_while_first_held.is_set()  # nosec B101
    final = memory.get_memory(REPO, mid)
    assert final is not None  # nosec B101
    assert final["blocking"] is True  # nosec B101
    assert final["text"] == "Updated rule text"  # nosec B101


def test_import_memories_single_repo_read_and_single_batch_prune(monkeypatch):
    monkeypatch.setattr(dm, "MEMORY_MAX_ENTRIES_PER_REPO", 4)
    blocking_old = memory.add_memory(
        REPO,
        {"text": "Blocking rule", "blocking": True, "created_at": "2026-01-01T00:00:00+00:00"},
    )
    unused_old_1 = memory.add_memory(
        REPO,
        {"text": "Unused old 1", "blocking": False, "created_at": "2026-01-02T00:00:00+00:00"},
    )
    unused_old_2 = memory.add_memory(
        REPO,
        {"text": "Unused old 2", "blocking": False, "created_at": "2026-01-03T00:00:00+00:00"},
    )
    used_old = memory.add_memory(
        REPO,
        {
            "text": "Used rule",
            "blocking": False,
            "hits": 3,
            "last_used": "2026-05-01T00:00:00+00:00",
            "created_at": "2026-01-04T00:00:00+00:00",
        },
    )

    rkeys_calls: list[str] = []
    orig_rkeys = store.rkeys

    def _counting_rkeys(pattern: str):
        rkeys_calls.append(pattern)
        return orig_rkeys(pattern)

    monkeypatch.setattr(store, "rkeys", _counting_rkeys)

    res = memory.import_memories(
        REPO,
        [
            {"text": "Imported rule A", "created_at": "2026-07-01T00:00:00+00:00"},
            {"text": "Imported rule B", "created_at": "2026-07-02T00:00:00+00:00"},
        ],
    )
    assert res == {"imported": 2, "skipped": 0, "failed": 0, "errors": []}  # nosec B101
    # Only ONE full repo scan for the entire batch import (no per-entry scan).
    assert len(rkeys_calls) == 1  # nosec B101

    remaining = memory.list_memories(REPO)
    remaining_ids = {m["id"] for m in remaining}
    assert len(remaining_ids) == 4  # nosec B101
    assert unused_old_1["id"] not in remaining_ids  # nosec B101
    assert unused_old_2["id"] not in remaining_ids  # nosec B101
    assert blocking_old["id"] in remaining_ids  # nosec B101
    assert used_old["id"] in remaining_ids  # nosec B101


def test_import_memories_reports_storage_and_prune_failures_separately(monkeypatch):
    monkeypatch.setattr(dm, "MEMORY_MAX_ENTRIES_PER_REPO", 2)
    memory.add_memory(REPO, {"text": "Existing rule 1", "created_at": "2026-01-01T00:00:00+00:00"})
    memory.add_memory(REPO, {"text": "Existing rule 2", "created_at": "2026-01-02T00:00:00+00:00"})

    orig_rset_json = store.rset_json
    orig_rdelete = store.rdelete

    def _flaky_rset_json(key: str, obj: object, ex=None, nx=False):
        if isinstance(obj, dict) and obj.get("text") == "Write boom":
            raise RuntimeError("redis write error")
        return orig_rset_json(key, obj, ex=ex, nx=nx)

    def _flaky_rdelete(key: str):
        if key.startswith(dm.memory_prefix(REPO)):
            raise RuntimeError("redis delete error")
        return orig_rdelete(key)

    monkeypatch.setattr(store, "rset_json", _flaky_rset_json)
    monkeypatch.setattr(store, "rdelete", _flaky_rdelete)

    res = memory.import_memories(
        REPO,
        [
            {"text": "Invalid confidence rule", "confidence": 9.5},  # validation -> skipped
            {"text": "Write boom"},  # storage write failure -> failed
            {"text": "Persisted rule"},  # succeeds, then batch prune fails -> imported + prune error
        ],
    )
    assert res["imported"] == 1  # nosec B101
    assert res["skipped"] == 1  # nosec B101
    assert res["failed"] == 2  # 1 write failure + 1 prune failure  # nosec B101
    assert len(res["errors"]) == 2  # nosec B101
    assert any("item 1" in e for e in res["errors"])  # nosec B101
    assert any("prune" in e for e in res["errors"])  # nosec B101


def test_retrieve_for_diff_does_not_overwrite_concurrent_edit_delete_or_archive(monkeypatch):
    import memory.service as memory_service

    m_edit = memory.add_memory(REPO, {"text": "Original redis timeout rule", "scope_globs": ["backend/**"]})
    m_del = memory.add_memory(REPO, {"text": "Rule about to be deleted", "scope_globs": ["backend/**"]})
    m_arch = memory.add_memory(REPO, {"text": "Rule about to be archived", "scope_globs": ["backend/**"]})

    orig_list = memory_service.list_memories

    def _list_then_mutate(repo: str, **kwargs):
        entries = orig_list(repo, **kwargs)
        # Simulate concurrent edits/delete/archive occurring after selection list was read.
        memory.update_memory(repo, m_edit["id"], {"text": "Concurrently updated redis timeout rule", "blocking": True})
        memory.delete_memory(repo, m_del["id"])
        memory.update_memory(repo, m_arch["id"], {"status": "archived"})
        return entries

    monkeypatch.setattr(memory_service, "list_memories", _list_then_mutate)

    res = memory.retrieve_for_diff(REPO, ["backend/store.py"], diff_text="redis timeout")

    # Edited entry keeps its concurrent edit AND has hits incremented.
    saved_edit = memory.get_memory(REPO, m_edit["id"])
    assert saved_edit is not None  # nosec B101
    assert saved_edit["text"] == "Concurrently updated redis timeout rule"  # nosec B101
    assert saved_edit["blocking"] is True  # nosec B101
    assert saved_edit["hits"] == 1 and saved_edit["last_used"] is not None  # nosec B101

    # Deleted entry was NOT resurrected.
    assert memory.get_memory(REPO, m_del["id"]) is None  # nosec B101

    # Archived entry stayed archived and hits were NOT incremented.
    saved_arch = memory.get_memory(REPO, m_arch["id"])
    assert saved_arch is not None and saved_arch["status"] == "archived" and saved_arch["hits"] == 0  # nosec B101

    # Only the still-active entry is returned and in used_ids.
    assert res["used_ids"] == [m_edit["id"]]  # nosec B101
    assert [r["id"] for r in res["rules"]] == [m_edit["id"]]  # nosec B101


def test_record_finding_dismissal_partial_failures_are_retryable(monkeypatch):
    import memory.service as memory_service

    # Part A: saving finding state succeeds, then counter increment fails -> retry counts it!
    fp1 = dm.finding_fingerprint("src/a.py", "foo()", "style", "Retryable partial dismissal")
    store.rset_json(
        dm.finding_key(REPO, 30, fp1),
        {"fingerprint": fp1, "file": "src/a.py", "category": "style", "title": "Retryable partial dismissal", "state": "open"},
        ex=dm.FINDING_TTL_S,
    )

    orig_rincr = store.rincr
    fail_once = {"counter": True}

    def _fail_counter_once(key: str, by: int = 1, ex=None):
        if fail_once["counter"] and key.startswith("memdismiss:"):
            fail_once["counter"] = False
            raise RuntimeError("transient counter failure")
        return orig_rincr(key, by, ex=ex)

    monkeypatch.setattr(store, "rincr", _fail_counter_once)

    with pytest.raises(RuntimeError, match="transient counter failure"):
        memory.record_finding_dismissal(REPO, 30, fp1, author="alice")

    # Finding state was saved as dismissed, yet counter was NOT incremented yet.
    saved_f1 = store.rget_json(dm.finding_key(REPO, 30, fp1))
    assert isinstance(saved_f1, dict) and saved_f1["state"] == "dismissed"  # nosec B101

    # Retry completes the missing counter increment!
    r1_retry = memory.record_finding_dismissal(REPO, 30, fp1, author="alice")
    assert r1_retry["already_dismissed"] is True  # nosec B101
    assert r1_retry["dismissal_count"] == 1  # nosec B101

    # A second retry does NOT double-count!
    r1_again = memory.record_finding_dismissal(REPO, 30, fp1, author="alice")
    assert r1_again["dismissal_count"] == 1  # nosec B101

    # Part B: 2nd dismissal increments counter to 2, then pattern creation fails -> retry creates pattern without double-counting!
    fp2 = dm.finding_fingerprint("src/b.py", "bar()", "style", "Retryable partial dismissal")
    store.rset_json(
        dm.finding_key(REPO, 31, fp2),
        {"fingerprint": fp2, "file": "src/b.py", "category": "style", "title": "Retryable partial dismissal", "state": "open"},
        ex=dm.FINDING_TTL_S,
    )

    orig_add = memory_service.add_memory
    fail_add = {"once": True}

    def _fail_add_once(repo: str, entry):
        if fail_add["once"]:
            fail_add["once"] = False
            raise RuntimeError("transient memory write failure")
        return orig_add(repo, entry)

    monkeypatch.setattr(memory_service, "add_memory", _fail_add_once)

    with pytest.raises(RuntimeError, match="transient memory write failure"):
        memory.record_finding_dismissal(REPO, 31, fp2, author="bob")

    # Retry sees count==2 (does NOT increment to 3!) and creates the missing pattern!
    r2_retry = memory.record_finding_dismissal(REPO, 31, fp2, author="bob")
    assert r2_retry["dismissal_count"] == 2  # nosec B101
    assert r2_retry["pattern_created"] is True  # nosec B101
    assert r2_retry["memory"] is not None  # nosec B101

    # Another retry does NOT boost or double-count!
    conf_before = r2_retry["memory"]["confidence"]
    r2_again = memory.record_finding_dismissal(REPO, 31, fp2, author="bob")
    assert r2_again["dismissal_count"] == 2  # nosec B101
    assert r2_again["pattern_boosted"] is False  # nosec B101
    assert r2_again["memory"]["confidence"] == conf_before  # nosec B101


def test_record_finding_dismissal_concurrent_same_and_different_findings():
    # 8 concurrent dismissals of DIFFERENT findings with the SAME stem -> count must be 8.
    def _dismiss_distinct(idx: int):
        fp = dm.finding_fingerprint(f"src/f{idx}.py", f"code_{idx}", "style", "Concurrent stem test")
        return memory.record_finding_dismissal(
            REPO,
            100 + idx,
            fp,
            finding={"file": f"src/f{idx}.py", "category": "style", "title": "Concurrent stem test"},
            author=f"dev{idx}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_dismiss_distinct, range(8)))

    stem_hash = results[0]["stem_hash"]
    counter_key = f"memdismiss:{REPO.lower()}:{stem_hash}"
    assert store.rget_json(counter_key) == 8  # nosec B101
    assert sum(1 for r in results if r["pattern_created"]) == 1  # nosec B101

    # 6 concurrent overlapping dismissals of the SAME finding -> counter increments only once (to 9).
    fp_same = dm.finding_fingerprint("src/same.py", "same_code", "style", "Concurrent stem test")
    store.rset_json(
        dm.finding_key(REPO, 200, fp_same),
        {"fingerprint": fp_same, "file": "src/same.py", "category": "style", "title": "Concurrent stem test", "state": "open"},
        ex=dm.FINDING_TTL_S,
    )

    def _dismiss_same(_i: int):
        return memory.record_finding_dismissal(REPO, 200, fp_same, author="alice")

    with ThreadPoolExecutor(max_workers=6) as pool:
        same_results = list(pool.map(_dismiss_same, range(6)))

    assert store.rget_json(counter_key) == 9  # nosec B101
    assert sum(1 for r in same_results if not r["already_dismissed"]) == 1  # nosec B101


def test_record_finding_dismissal_keeps_archived_pattern_archived():
    fp1 = dm.finding_fingerprint("src/x.py", "x", "style", "Archived dismissal pattern rule")
    r1 = memory.record_finding_dismissal(
        REPO,
        40,
        fp1,
        finding={"file": "src/x.py", "category": "style", "title": "Archived dismissal pattern rule"},
        reason="Intentional pattern",
        author="alice",
    )
    assert r1["pattern_created"] is True and r1["memory"] is not None  # nosec B101
    mid = r1["memory"]["id"]

    # Operator explicitly archives this dismissal pattern.
    archived = memory.update_memory(REPO, mid, {"status": "archived"})
    assert archived is not None and archived["status"] == "archived"  # nosec B101

    # Another dismissal of the same stem must NOT silently reactivate it.
    fp2 = dm.finding_fingerprint("src/y.py", "y", "style", "Archived dismissal pattern rule")
    r2 = memory.record_finding_dismissal(
        REPO,
        41,
        fp2,
        finding={"file": "src/y.py", "category": "style", "title": "Archived dismissal pattern rule"},
        author="bob",
    )
    assert r2["pattern_boosted"] is True  # nosec B101
    assert r2["memory"] is not None and r2["memory"]["status"] == "archived"  # nosec B101
    saved = memory.get_memory(REPO, mid)
    assert saved is not None and saved["status"] == "archived"  # nosec B101

    # Explicit approval reactivates it.
    approved = memory.approve_memory(REPO, mid)
    assert approved is not None and approved["status"] == "active"  # nosec B101


def test_editorconfig_numeric_ranges_expand_bounded_and_skip_invalid():
    import memory.service as memory_service

    cfg = (
        "[*.{1..3}]\n"
        "indent_size = 2\n"
        "[file{-1..1}.txt]\n"
        "indent_size = 4\n"
        "[*.{1..50}]\n"
        "indent_size = 8\n"
        "[*.{5..2}]\n"
        "indent_size = 3\n"
        "[*.{a..z}]\n"
        "indent_size = 6\n"
    )
    facts = memory_service._extract_editorconfig_facts(cfg)
    by_text = dict(facts)

    assert "EditorConfig [*.{1..3}]: indent_size=2" in by_text  # nosec B101
    assert by_text["EditorConfig [*.{1..3}]: indent_size=2"] == ["**/*.1", "**/*.2", "**/*.3"]  # nosec B101
    assert "EditorConfig [file{-1..1}.txt]: indent_size=4" in by_text  # nosec B101
    assert by_text["EditorConfig [file{-1..1}.txt]: indent_size=4"] == [
        "**/file-1.txt",
        "**/file0.txt",
        "**/file1.txt",
    ]  # nosec B101
    # Oversized, reversed, and non-numeric range sections are skipped cleanly.
    assert not any("1..50" in t for t in by_text)  # nosec B101
    assert not any("5..2" in t for t in by_text)  # nosec B101
    assert not any("a..z" in t for t in by_text)  # nosec B101


def test_bootstrap_repo_preserves_profile_on_fetch_errors(monkeypatch):
    import httpx

    # 1. Initial successful bootstrap persists a valid profile.
    initial_files = {
        "package.json": json.dumps({"dependencies": {"react": "^18"}, "scripts": {"test": "vitest"}}),
        "pyproject.toml": "[project]\nname='app'\ndependencies=['fastapi']\n",
    }

    class _Client:
        def __init__(self, handler):
            self._handler = handler

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return self._handler(url.split("/contents/", 1)[-1])

    res1 = asyncio.run(
        memory.bootstrap_repo(
            REPO,
            github_token="tok",
            client=_Client(
                lambda p: _Resp(200, data=_b64_payload(initial_files[p]))
                if p in initial_files
                else _Resp(404, data={"message": "Not Found"})
            ),
        )
    )
    assert res1["ok"] is True and res1["partial"] is False and res1["fetch_errors"] == {}  # nosec B101
    saved_initial = memory.get_repo_profile(REPO)
    assert saved_initial is not None and "React" in saved_initial["frameworks"] and "FastAPI" in saved_initial["frameworks"]  # nosec B101

    # 2. Full GitHub outage (503 / timeout) must NOT overwrite the last valid profile.
    def _all_fail(path: str):
        if path == "package.json":
            raise httpx.ConnectTimeout("timed out")
        return _Resp(503, data={"message": "Service Unavailable"})

    res_outage = asyncio.run(memory.bootstrap_repo(REPO, github_token="tok", client=_Client(_all_fail)))
    assert res_outage["ok"] is False  # nosec B101
    assert res_outage["partial"] is True  # nosec B101
    assert res_outage["profile_preserved"] is True  # nosec B101
    assert res_outage["profile"] == saved_initial  # nosec B101
    assert memory.get_repo_profile(REPO) == saved_initial  # nosec B101
    assert "package.json" in res_outage["fetch_errors"]  # nosec B101

    # 3. Partial manifest error (package.json 500, pyproject.toml 200) also preserves valid profile.
    def _partial_fail(path: str):
        if path == "package.json":
            return _Resp(500, data={"message": "Internal Error"})
        if path in initial_files:
            return _Resp(200, data=_b64_payload(initial_files[path]))
        return _Resp(404, data={"message": "Not Found"})

    res_partial = asyncio.run(memory.bootstrap_repo(REPO, github_token="tok", client=_Client(_partial_fail)))
    assert res_partial["ok"] is False and res_partial["profile_preserved"] is True  # nosec B101
    assert memory.get_repo_profile(REPO) == saved_initial  # nosec B101

    # 4. Fresh repo with all 500s does NOT persist a bogus empty profile.
    res_fresh_fail = asyncio.run(
        memory.bootstrap_repo(
            "acme/unreachable",
            github_token="tok",
            client=_Client(lambda _p: _Resp(500, data={"message": "error"})),
        )
    )
    assert res_fresh_fail["ok"] is False and res_fresh_fail["profile"] is None  # nosec B101
    assert memory.get_repo_profile("acme/unreachable") is None  # nosec B101


def test_bootstrap_repo_replaces_stale_facts_per_source_and_preserves_manual_and_failed():
    class _Client:
        def __init__(self, handler):
            self._handler = handler

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return self._handler(url.split("/contents/", 1)[-1])

    v1_files = {
        "package.json": json.dumps({"dependencies": {"react": "^18"}, "scripts": {"test": "jest"}}),
        "AGENTS.md": "- Always run jest before committing changes.\n- Keep old convention alive in v1.\n",
        "CLAUDE.md": "- Always sign webhook payloads with HMAC-SHA256.\n",
        "CONTRIBUTING.md": "- Never commit temporary build artifacts to git.\n",
    }
    res1 = asyncio.run(
        memory.bootstrap_repo(
            REPO,
            github_token="tok",
            client=_Client(
                lambda p: _Resp(200, data=_b64_payload(v1_files[p]))
                if p in v1_files
                else _Resp(404, data={"message": "Not Found"})
            ),
        )
    )
    assert res1["ok"] is True and res1["removed"] == 0  # nosec B101

    # Add a manual memory that must NEVER be removed by bootstrap.
    manual = memory.add_memory(
        REPO,
        {"kind": "repo_fact", "source": "manual", "text": "[AGENTS.md] Manual operator note that must survive."},
    )

    # Second bootstrap:
    # - package.json switched React/jest -> Vue/vitest
    # - AGENTS.md replaced old rule with new rule
    # - CONTRIBUTING.md deleted (404)
    # - CLAUDE.md failed to fetch (500) -> its facts MUST be preserved!
    v2_files = {
        "package.json": json.dumps({"dependencies": {"vue": "^3"}, "scripts": {"test": "vitest"}}),
        "AGENTS.md": "- Always run vitest before committing changes.\n",
    }

    def _v2_handler(path: str):
        if path == "CLAUDE.md":
            return _Resp(500, data={"message": "Internal Error"})
        if path in v2_files:
            return _Resp(200, data=_b64_payload(v2_files[path]))
        return _Resp(404, data={"message": "Not Found"})

    res2 = asyncio.run(memory.bootstrap_repo(REPO, github_token="tok", client=_Client(_v2_handler)))
    assert res2["partial"] is True and res2["fetch_errors"] == {"CLAUDE.md": "http_500"}  # nosec B101
    assert res2["removed"] >= 3  # old AGENTS.md rules + old CONTRIBUTING.md + old React stack  # nosec B101

    all_texts = [m["text"] for m in memory.list_memories(REPO)]
    # Updated AGENTS.md & manifests facts replaced stale ones
    assert any("Always run vitest" in t for t in all_texts)  # nosec B101
    assert not any("Keep old convention alive" in t for t in all_texts)  # nosec B101
    assert any("Vue" in t for t in all_texts)  # nosec B101
    assert not any("React" in t for t in all_texts)  # nosec B101
    # Verified-absent CONTRIBUTING.md fact was removed
    assert not any("temporary build artifacts" in t for t in all_texts)  # nosec B101
    # Failed-to-fetch CLAUDE.md fact was PRESERVED
    assert any("HMAC-SHA256" in t for t in all_texts)  # nosec B101
    # Manual memory was PRESERVED
    assert memory.get_memory(REPO, manual["id"]) is not None  # nosec B101


def test_extract_learnings_from_merged_pr_escapes_literal_glob_metacharacters():
    comments = [
        {
            "author": "alice",
            "path": "app/[id]/page.ts",
            "body": "Always validate dynamic route params before querying the database.",
        },
        {
            "author": "bob",
            "path": "src/a*b?c.ts",
            "body": "Never skip schema validation on wildcard filename helpers.",
        },
    ]

    created = memory.extract_learnings_from_merged_pr(REPO, 77, comments, auto_approve=True)
    assert len(created) == 2  # nosec B101

    # Exact literal file path matches!
    hit_bracket = memory.retrieve_for_diff(REPO, ["app/[id]/page.ts"], diff_text="route params")
    assert [r["id"] for r in hit_bracket["rules"]] == [created[0]["id"]]  # nosec B101

    # Character-class lookalike path must NOT match!
    miss_bracket = memory.retrieve_for_diff(REPO, ["app/i/page.ts"], diff_text="route params")
    assert miss_bracket["rules"] == []  # nosec B101

    # Wildcard literal path matches itself and NOT expanded lookalike!
    hit_wild = memory.retrieve_for_diff(REPO, ["src/a*b?c.ts"], diff_text="schema validation")
    assert [r["id"] for r in hit_wild["rules"]] == [created[1]["id"]]  # nosec B101
    miss_wild = memory.retrieve_for_diff(REPO, ["src/axbyc.ts"], diff_text="schema validation")
    assert miss_wild["rules"] == []  # nosec B101


# ---------------------------------------------------------------------------
# PR #18 follow-up review fixes (threads 1-10)
# ---------------------------------------------------------------------------


def test_batch_eviction_preserves_blocking_and_used_over_imported_unused(monkeypatch):
    monkeypatch.setattr(dm, "MEMORY_MAX_ENTRIES_PER_REPO", 4)
    blocking_old = memory.add_memory(
        REPO,
        {"text": "Blocking security rule", "blocking": True, "created_at": "2026-01-01T00:00:00+00:00"},
    )
    used_old = memory.add_memory(
        REPO,
        {
            "text": "Used non-blocking rule",
            "blocking": False,
            "hits": 4,
            "last_used": "2026-06-01T00:00:00+00:00",
            "created_at": "2026-01-02T00:00:00+00:00",
        },
    )
    unused_old = memory.add_memory(
        REPO,
        {"text": "Unused old rule", "blocking": False, "created_at": "2026-01-03T00:00:00+00:00"},
    )

    # Import 3 unused non-blocking entries (total 6, cap 4 -> 2 victims).
    # Must evict unused_old + 1 imported unused entry, preserving blocking_old and used_old!
    res = memory.import_memories(
        REPO,
        [
            {"text": "Imported unused 1", "created_at": "2026-01-01T00:00:01+00:00"},
            {"text": "Imported unused 2", "created_at": "2026-07-02T00:00:00+00:00"},
            {"text": "Imported unused 3", "created_at": "2026-07-03T00:00:00+00:00"},
        ],
    )
    assert res["imported"] == 3 and res["failed"] == 0  # nosec B101
    remaining_ids = {m["id"] for m in memory.list_memories(REPO)}
    assert len(remaining_ids) == 4  # nosec B101
    assert blocking_old["id"] in remaining_ids  # nosec B101
    assert used_old["id"] in remaining_ids  # nosec B101
    assert unused_old["id"] not in remaining_ids  # nosec B101


def test_add_memory_and_import_serialize_through_repo_lock(monkeypatch):
    monkeypatch.setattr(dm, "MEMORY_MAX_ENTRIES_PER_REPO", 4)
    repo_lock_key = f"memrepolock:{REPO.lower()}"
    contended = threading.Event()
    import_in_lock = threading.Event()

    orig_rset = store.rset
    orig_rkeys = store.rkeys

    def _tracking_rset(key: str, value: str, ex=None, nx: bool = False):
        ok = orig_rset(key, value, ex=ex, nx=nx)
        if key == repo_lock_key and nx and not ok:
            contended.set()
        return ok

    def _pausing_rkeys(pattern: str):
        keys = orig_rkeys(pattern)
        if pattern.startswith(dm.memory_prefix(REPO)) and not import_in_lock.is_set():
            import_in_lock.set()
            assert contended.wait(timeout=2.0)  # nosec B101
        return keys

    monkeypatch.setattr(store, "rset", _tracking_rset)
    monkeypatch.setattr(store, "rkeys", _pausing_rkeys)

    def _run_import():
        return memory.import_memories(
            REPO,
            [{"text": f"Imported item {i}"} for i in range(4)],
        )

    def _run_add():
        assert import_in_lock.wait(timeout=2.0)  # nosec B101
        return memory.add_memory(REPO, {"text": "Concurrent add_memory item"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_imp = pool.submit(_run_import)
        f_add = pool.submit(_run_add)
        f_imp.result()
        f_add.result()

    assert contended.is_set()  # nosec B101
    assert len(memory.list_memories(REPO)) <= 4  # nosec B101


def test_retrieve_for_diff_reclassifies_rechecks_scope_and_enforces_latest_budget(monkeypatch):
    import memory.service as memory_service

    m_reclass = memory.add_memory(
        REPO,
        {"kind": "learning", "text": "Originally a learning rule", "scope_globs": ["backend/**"], "blocking": True},
    )
    m_rescope = memory.add_memory(
        REPO,
        {"kind": "learning", "text": "Rule whose scope will move", "scope_globs": ["backend/**"]},
    )
    m_grow = memory.add_memory(
        REPO,
        {"kind": "learning", "text": "Short rule", "scope_globs": ["backend/**"]},
    )
    m_ok = memory.add_memory(
        REPO,
        {"kind": "learning", "text": "Normal surviving rule", "scope_globs": ["backend/**"]},
    )

    orig_list = memory_service.list_memories

    def _list_then_edit(repo: str, **kwargs):
        entries = orig_list(repo, **kwargs)
        # 1. Reclassify m_reclass from learning -> dismissed_pattern
        memory.update_memory(repo, m_reclass["id"], {"kind": "dismissed_pattern", "blocking": False})
        # 2. Change m_rescope scope so it no longer matches backend/store.py
        memory.update_memory(repo, m_rescope["id"], {"scope_globs": ["frontend/**"]})
        # 3. Grow m_grow text so it exceeds the remaining budget
        memory.update_memory(repo, m_grow["id"], {"text": "X" * 400})
        return entries

    monkeypatch.setattr(memory_service, "list_memories", _list_then_edit)

    res = memory.retrieve_for_diff(REPO, ["backend/store.py"], diff_text="rule", max_chars=250)

    # m_reclass must be in dismissed, NOT rules!
    assert [d["id"] for d in res["dismissed"]] == [m_reclass["id"]]  # nosec B101
    assert [r["id"] for r in res["rules"]] == [m_ok["id"]]  # nosec B101
    assert set(res["used_ids"]) == {m_reclass["id"], m_ok["id"]}  # nosec B101

    # m_rescope and m_grow were skipped without incrementing hits
    assert memory.get_memory(REPO, m_rescope["id"])["hits"] == 0  # type: ignore[index]  # nosec B101
    assert memory.get_memory(REPO, m_grow["id"])["hits"] == 0  # type: ignore[index]  # nosec B101
    assert memory.get_memory(REPO, m_reclass["id"])["hits"] == 1  # type: ignore[index]  # nosec B101
    assert memory.get_memory(REPO, m_ok["id"])["hits"] == 1  # type: ignore[index]  # nosec B101


def test_record_finding_dismissal_sets_dirty_marker_on_retry(monkeypatch):
    fp = dm.finding_fingerprint("src/d.py", "dirty()", "style", "Dirty marker retry test")
    store.rset_json(
        dm.finding_key(REPO, 55, fp),
        {"fingerprint": fp, "file": "src/d.py", "category": "style", "title": "Dirty marker retry test", "state": "open"},
        ex=dm.FINDING_TTL_S,
    )
    dirty_k = dm.dirty_key(REPO, 55)
    orig_rset = store.rset
    fail_dirty = {"once": True}

    def _fail_dirty_once(key: str, value: str, ex=None, nx: bool = False):
        if key == dirty_k and fail_dirty["once"]:
            fail_dirty["once"] = False
            raise RuntimeError("transient dirty write failure")
        return orig_rset(key, value, ex=ex, nx=nx)

    monkeypatch.setattr(store, "rset", _fail_dirty_once)

    with pytest.raises(RuntimeError, match="transient dirty write failure"):
        memory.record_finding_dismissal(REPO, 55, fp, author="alice")

    # Finding is already marked dismissed, yet dirty key is absent.
    assert store.rget(dirty_k) is None  # nosec B101

    # Retry succeeds and sets the dirty key even though already_dismissed is True!
    res = memory.record_finding_dismissal(REPO, 55, fp, author="alice")
    assert res["already_dismissed"] is True  # nosec B101
    assert store.rget(dirty_k) is not None  # nosec B101


def test_record_finding_dismissal_atomic_redis_lua_claim_and_no_claim_delete(monkeypatch):
    import memory.service as memory_service

    # Simulate a Redis client whose EVAL commits claim + INCR + TTL atomically,
    # then drops the response on the first call (post-commit network error).
    redis_kv: dict[str, str] = {}
    eval_calls = {"n": 0}
    delete_calls: list[str] = []

    class _FakeRedis:
        def eval(self, script, numkeys, *keys_and_args):
            eval_calls["n"] += 1
            counted_k, counter_k, _ttl = keys_and_args[0], keys_and_args[1], keys_and_args[2]
            if counted_k not in redis_kv:
                redis_kv[counted_k] = "1"
                redis_kv[counter_k] = str(int(redis_kv.get(counter_k, "0")) + 1)
            val = int(redis_kv[counter_k])
            if eval_calls["n"] == 1:
                raise RuntimeError("socket timeout after Redis commit")
            return val

    orig_rdelete = store.rdelete

    def _track_delete(key: str):
        delete_calls.append(key)
        return orig_rdelete(key)

    monkeypatch.setattr(store, "_REDIS", _FakeRedis())
    monkeypatch.setattr(store, "_REDIS_CONFIGURED", True)
    monkeypatch.setattr(store, "rdelete", _track_delete)

    counter_k = f"memdismiss:{REPO.lower()}:abcdef1234567890"
    counted_k = f"memdismisscounted:{REPO.lower()}:60:fp:abcdef1234567890"

    with pytest.raises(RuntimeError, match="socket timeout after Redis commit"):
        memory_service._claim_and_increment_dismiss_count(counter_k, counted_k, 3600)

    # Claim was NOT deleted after committed increment!
    assert counted_k not in delete_calls  # nosec B101
    assert redis_kv[counter_k] == "1"  # nosec B101

    # Retry sees the already-committed claim and returns 1 without incrementing again!
    count_retry = memory_service._claim_and_increment_dismiss_count(counter_k, counted_k, 3600)
    assert count_retry == 1  # nosec B101
    assert redis_kv[counter_k] == "1"  # nosec B101


def test_record_finding_dismissal_pattern_boost_is_idempotent_across_done_marker_failure(monkeypatch):
    # First create a dismissed_pattern with explicit reason on PR #70.
    fp1 = dm.finding_fingerprint("src/p1.py", "x()", "style", "Idempotent boost test")
    r1 = memory.record_finding_dismissal(
        REPO,
        70,
        fp1,
        finding={"file": "src/p1.py", "category": "style", "title": "Idempotent boost test"},
        reason="Intentional pattern",
        author="alice",
    )
    assert r1["pattern_created"] is True  # nosec B101
    conf_after_create = r1["memory"]["confidence"]

    # Second distinct dismissal on PR #71 boosts confidence, then done_key write fails!
    fp2 = dm.finding_fingerprint("src/p2.py", "y()", "style", "Idempotent boost test")
    store.rset_json(
        dm.finding_key(REPO, 71, fp2),
        {"fingerprint": fp2, "file": "src/p2.py", "category": "style", "title": "Idempotent boost test", "state": "open"},
        ex=dm.FINDING_TTL_S,
    )

    orig_rset = store.rset
    fail_done = {"once": True}

    def _fail_done_once(key: str, value: str, ex=None, nx: bool = False):
        if key.startswith("memdismissdone:") and fail_done["once"]:
            fail_done["once"] = False
            raise RuntimeError("transient done marker failure")
        return orig_rset(key, value, ex=ex, nx=nx)

    monkeypatch.setattr(store, "rset", _fail_done_once)

    with pytest.raises(RuntimeError, match="transient done marker failure"):
        memory.record_finding_dismissal(REPO, 71, fp2, author="bob")

    saved_mid = memory.get_memory(REPO, r1["memory"]["id"])
    assert saved_mid is not None  # nosec B101
    conf_after_boost = saved_mid["confidence"]
    assert conf_after_boost > conf_after_create  # nosec B101

    # Retry completes missing done_key without boosting confidence a second time!
    r2_retry = memory.record_finding_dismissal(REPO, 71, fp2, author="bob")
    assert r2_retry["pattern_boosted"] is False  # nosec B101
    assert r2_retry["memory"]["confidence"] == conf_after_boost  # nosec B101

    # A 3rd DISTINCT dismissal still boosts confidence!
    fp3 = dm.finding_fingerprint("src/p3.py", "z()", "style", "Idempotent boost test")
    r3 = memory.record_finding_dismissal(
        REPO,
        72,
        fp3,
        finding={"file": "src/p3.py", "category": "style", "title": "Idempotent boost test"},
        author="carol",
    )
    assert r3["pattern_boosted"] is True  # nosec B101
    assert r3["memory"]["confidence"] > conf_after_boost  # nosec B101


def test_editorconfig_braces_inside_char_classes_are_not_expanded():
    import memory.service as memory_service

    cfg = (
        "[file[ab{1..2}].txt]\n"
        "indent_size = 2\n"
        "[file[ab{1..2}].{py,rs}]\n"
        "indent_size = 4\n"
        "[file[!]{1..2}].txt]\n"
        "indent_size = 3\n"
        "[*.{1..3}]\n"
        "indent_size = 2\n"
    )
    facts = dict(memory_service._extract_editorconfig_facts(cfg))
    assert facts["EditorConfig [file[ab{1..2}].txt]: indent_size=2"] == ["**/file[ab{1..2}].txt"]  # nosec B101
    assert facts["EditorConfig [file[ab{1..2}].{py,rs}]: indent_size=4"] == [
        "**/file[ab{1..2}].py",
        "**/file[ab{1..2}].rs",
    ]  # nosec B101
    assert facts["EditorConfig [file[!]{1..2}].txt]: indent_size=3"] == ["**/file[!]{1..2}].txt"]  # nosec B101
    assert facts["EditorConfig [*.{1..3}]: indent_size=2"] == ["**/*.1", "**/*.2", "**/*.3"]  # nosec B101


def test_bootstrap_repo_verifies_contents_access_before_treating_all_404_as_absence():
    class _Client:
        def __init__(self, handler):
            self._handler = handler

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return self._handler(url)

    # 1. Initial valid bootstrap creates profile + bootstrap facts.
    initial_files = {
        "package.json": json.dumps({"dependencies": {"react": "^18"}, "scripts": {"test": "vitest"}}),
        "AGENTS.md": "- Always run vitest before merging.\n",
    }

    def _ok_handler(url: str):
        path = url.split("/contents/", 1)[-1] if "/contents/" in url else ""
        if path in initial_files:
            return _Resp(200, data=_b64_payload(initial_files[path]))
        return _Resp(404, data={"message": "Not Found"})

    res1 = asyncio.run(memory.bootstrap_repo(REPO, github_token="tok", client=_Client(_ok_handler)))
    assert res1["ok"] is True  # nosec B101
    saved_profile = memory.get_repo_profile(REPO)
    assert saved_profile is not None and "React" in saved_profile["frameworks"]  # nosec B101
    initial_texts = {m["text"] for m in memory.list_memories(REPO)}
    assert any("Always run vitest" in t for t in initial_texts)  # nosec B101

    # 2. All-404 response when token lacks Contents access (probe /contents also returns 404):
    # MUST NOT overwrite saved profile or delete existing bootstrap facts!
    res_unauth = asyncio.run(
        memory.bootstrap_repo(
            REPO,
            github_token="bad-tok",
            client=_Client(lambda _u: _Resp(404, data={"message": "Not Found"})),
        )
    )
    assert res_unauth["ok"] is False  # nosec B101
    assert res_unauth["partial"] is True  # nosec B101
    assert res_unauth["profile_preserved"] is True  # nosec B101
    assert res_unauth["removed"] == 0  # nosec B101
    assert memory.get_repo_profile(REPO) == saved_profile  # nosec B101
    assert {m["text"] for m in memory.list_memories(REPO)} == initial_texts  # nosec B101

    # 3. All-404 file responses when probe /contents returns 200 (genuine removal):
    # Confirmed absence removes obsolete bootstrap facts and updates profile.
    def _confirmed_empty_handler(url: str):
        if url.endswith("/contents"):
            return _Resp(200, data=[])
        return _Resp(404, data={"message": "Not Found"})

    res_empty = asyncio.run(
        memory.bootstrap_repo(REPO, github_token="tok", client=_Client(_confirmed_empty_handler))
    )
    assert res_empty["ok"] is True  # nosec B101
    assert res_empty["removed"] >= 2  # nosec B101
    assert memory.get_repo_profile(REPO)["files_indexed"] == []  # type: ignore[index]  # nosec B101

