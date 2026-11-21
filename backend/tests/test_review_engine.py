"""Comprehensive unit & golden tests for the Phase 1 Native Structured Review Engine (`backend/review/`)."""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

import datamodel as dm
import store
from review import (
    DiffFetchError,
    RepoConfig,
    budget_diff_by_risk,
    call_llm,
    extract_json,
    fetch_incremental_diff,
    find_finding_by_comment_id,
    findings_index_key,
    format_inline_comment,
    generate_findings,
    generate_walkthrough,
    get_last_reviewed_sha,
    list_pr_findings,
    load_repo_config,
    mark_outdated_findings,
    merge_agent_and_repo_config,
    parse_pragent_yaml,
    parse_unified_diff,
    pr_last_reviewed_key,
    publish_review,
    render_pragent_yaml,
    render_summary_comment,
    run_structured_review,
    save_pr_findings,
    set_finding_state,
    set_last_reviewed_sha,
    should_auto_review,
    strip_code_fences,
    verify_and_filter_findings,
)
from sandboxes.providers_review import OPENROUTER_FALLBACK_MODEL, review_marker


# ---------------------------------------------------------------------------
# Sample diffs & fake HTTP helpers
# ---------------------------------------------------------------------------

SAMPLE_DIFF = """\
diff --git a/backend/auth.py b/backend/auth.py
index 1111111..2222222 100644
--- a/backend/auth.py
+++ b/backend/auth.py
@@ -10,6 +10,8 @@ def verify_token(token: str) -> bool:
     if not token:
         return False
-    expected = get_secret()
-    return token == expected
+    expected = get_secret()
+    query = f"SELECT * FROM tokens WHERE val = '{token}'"
+    db.execute(query)
+    return token == expected
 
 def helper():
@@ -40,3 +42,4 @@ def helper():
     x = 1
+    y = x + 1
     return x
diff --git a/src/new_file.py b/src/new_file.py
new file mode 100644
--- /dev/null
+++ b/src/new_file.py
@@ -0,0 +1,4 @@
+def greet(name):
+    if not name:
+        return "hi"
+    return f"hello {name}"
diff --git a/old_removed.py b/old_removed.py
deleted file mode 100644
--- a/old_removed.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def legacy():
-    return 42
-
"""


class _FakeResp:
    def __init__(self, status_code: int, data: Any = None, text: str = ""):
        self.status_code = status_code
        self._data = data
        if text:
            self.text = text
        elif isinstance(data, (dict, list)):
            self.text = json.dumps(data)
        else:
            self.text = str(data or "")

    def json(self) -> Any:
        if self._data is not None:
            return self._data
        return json.loads(self.text)


class _MockGitHubClient:
    """Scriptable async HTTP mock capturing every request for assertions."""

    def __init__(self, handler):
        self._handler = handler
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aclose(self):
        return None

    async def get(self, url: str, headers: dict | None = None, params: dict | None = None):
        req = {"method": "GET", "url": url, "headers": dict(headers or {}), "params": dict(params or {})}
        self.requests.append(req)
        return self._handler("GET", url, headers or {}, params or {}, None)

    async def post(self, url: str, headers: dict | None = None, json: Any = None):
        req = {"method": "POST", "url": url, "headers": dict(headers or {}), "json": json}
        self.requests.append(req)
        return self._handler("POST", url, headers or {}, {}, json)

    async def patch(self, url: str, headers: dict | None = None, json: Any = None):
        req = {"method": "PATCH", "url": url, "headers": dict(headers or {}), "json": json}
        self.requests.append(req)
        return self._handler("PATCH", url, headers or {}, {}, json)


def _file_diff(path: str, n_lines: int, prefix: str = "val") -> str:
    body = "".join(f"+{prefix}_{i} = {i}\n" for i in range(n_lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{n_lines} @@\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# 1. Diff parsing, line mapping, incremental fetch & risk budgeting
# ---------------------------------------------------------------------------


def test_parse_unified_diff_line_sets_and_code_lookup():
    parsed = parse_unified_diff(SAMPLE_DIFF)
    assert parsed.paths == ["backend/auth.py", "src/new_file.py", "old_removed.py"]

    auth = parsed.get_file("backend/auth.py")
    assert auth is not None
    assert auth.status == "modified"
    assert auth.additions == 5
    assert auth.deletions == 2
    assert len(auth.hunks) == 2
    assert auth.hunks[0].section_header == "def verify_token(token: str) -> bool:"

    # Hunk 1 right lines: 10(ctx), 11(ctx), 12(add), 13(add), 14(add), 15(add), 16(ctx), 17(ctx)
    assert {10, 11, 12, 13, 14, 15, 16, 17}.issubset(auth.right_lines)
    assert {12, 13, 14, 15}.issubset(auth.added_lines)
    # Hunk 1 left lines: 10(ctx), 11(ctx), 12(del), 13(del), 14(ctx), 15(ctx)
    assert {10, 11, 12, 13, 14, 15}.issubset(auth.left_lines)
    assert auth.deleted_lines == {12, 13}
    assert "SELECT * FROM tokens" in auth.right_code[13]
    assert "return token == expected" in auth.left_code[13]
    # Hunk 2 right lines: 42(ctx), 43(add), 44(ctx)
    assert 43 in auth.added_lines
    assert 30 not in auth.right_lines
    assert auth.code_span(13, 14, "RIGHT") == (
        "    query = f\"SELECT * FROM tokens WHERE val = '{token}'\"\n    db.execute(query)"
    )
    assert parsed.get_file("./b/backend/auth.py") is None  # odd double prefix not matched
    assert parsed.get_file("b/backend/auth.py") is auth
    assert parsed.has_line("backend/auth.py", 13, "RIGHT") is True
    assert parsed.has_line("backend/auth.py", 999, "RIGHT") is False

    new_f = parsed.get_file("src/new_file.py")
    assert new_f is not None and new_f.status == "added"
    assert new_f.right_lines == {1, 2, 3, 4}
    assert new_f.left_lines == set()

    del_f = parsed.get_file("old_removed.py")
    assert del_f is not None and del_f.status == "deleted"
    assert del_f.left_lines == {1, 2, 3}
    assert del_f.right_lines == set()


def test_parse_unified_diff_no_newline_single_line_hunk_and_binary():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
        "diff --git a/logo.png b/logo.png\n"
        "Binary files a/logo.png and b/logo.png differ\n"
        "diff --git a/old_name.py b/new_name.py\n"
        "rename from old_name.py\n"
        "rename to new_name.py\n"
        "@@ -1,1 +1,2 @@\n"
        " keep\n"
        "+added\n"
    )
    parsed = parse_unified_diff(diff)
    a = parsed.get_file("a.py")
    assert a is not None
    assert a.left_lines == {1} and a.right_lines == {1}
    assert a.left_code[1] == "old" and a.right_code[1] == "new"

    logo = parsed.get_file("logo.png")
    assert logo is not None and logo.is_binary is True

    ren = parsed.get_file("new_name.py")
    assert ren is not None and ren.status == "renamed" and ren.old_path == "old_name.py"
    assert ren.added_lines == {2}
    assert "[R2] +added" in ren.render_numbered()


def test_fetch_incremental_diff_compare_and_fallbacks():
    inc_diff = _file_diff("src/app.py", 3)
    full_diff = _file_diff("src/full.py", 4)

    # 1. Incremental happy path
    def ok_compare(method, url, headers, params, body):
        assert "/compare/aaa1111...bbb2222" in url
        assert headers["Accept"] == "application/vnd.github.diff"
        return _FakeResp(200, text=inc_diff)

    client = _MockGitHubClient(ok_compare)
    res = asyncio.run(fetch_incremental_diff(client, "o/r", 7, "base000", "bbb2222", "aaa1111", "tok"))
    assert res.is_incremental is True
    assert res.incremental_from == "aaa1111"
    assert res.diff == inc_diff

    # 2. Same head SHA short-circuits cleanly
    res_same = asyncio.run(fetch_incremental_diff(client, "o/r", 7, "base000", "aaa1111", "aaa1111", "tok"))
    assert res_same.is_incremental is True and res_same.diff == ""

    # 3. Compare returns 404 (force push) -> falls back to full PR diff
    def compare_404(method, url, headers, params, body):
        if "/compare/" in url:
            return _FakeResp(404, {"message": "Not Found"})
        if url.endswith("/pulls/7"):
            return _FakeResp(200, text=full_diff)
        raise AssertionError(url)

    client2 = _MockGitHubClient(compare_404)
    res2 = asyncio.run(fetch_incremental_diff(client2, "o/r", 7, "base000", "bbb2222", "deadbeef", "tok"))
    assert res2.is_incremental is False
    assert res2.incremental_from is None
    assert res2.diff == full_diff
    assert any("falling back to full PR diff" in n for n in res2.notes)

    # 4. Full PR diff returns 406 -> falls back to PR files patches
    def pr_406(method, url, headers, params, body):
        if url.endswith("/pulls/7"):
            return _FakeResp(406, {"message": "too large"})
        if url.endswith("/pulls/7/files"):
            return _FakeResp(200, [{"filename": "src/big.py", "patch": "@@ -1,1 +1,2 @@\n a\n+b"}])
        raise AssertionError(url)

    client3 = _MockGitHubClient(pr_406)
    res3 = asyncio.run(fetch_incremental_diff(client3, "o/r", 7, "base000", "bbb2222", None, "tok"))
    assert res3.is_incremental is False
    assert "src/big.py" in res3.diff
    assert any("406" in n for n in res3.notes)

    # 5. Full PR diff fails with 500 -> raises DiffFetchError
    client4 = _MockGitHubClient(lambda *a: _FakeResp(500, {"message": "err"}))
    with pytest.raises(DiffFetchError):
        asyncio.run(fetch_incremental_diff(client4, "o/r", 7, "base000", "bbb2222", None, "tok"))


def test_budget_diff_by_risk_prioritizes_security_and_source_over_tests_docs_and_drops_lockfiles():
    diff = (
        _file_diff("package-lock.json", 120)
        + _file_diff("README.md", 60, prefix="doc")
        + _file_diff("tests/test_app.py", 60, prefix="tst")
        + _file_diff("backend/service.py", 15, prefix="src")
        + _file_diff("backend/auth.py", 15, prefix="auth")
    )
    parsed = parse_unified_diff(diff)
    budget = 950
    text, notes = budget_diff_by_risk(parsed, budget)
    assert len(text) <= budget
    # Lockfile always dropped
    assert "package-lock.json" not in text
    assert any("package-lock.json" in n for n in notes)
    # High-risk auth + source files kept ahead of docs/tests
    assert "backend/auth.py" in text
    assert "backend/service.py" in text
    assert text.index("backend/auth.py") < text.index("backend/service.py")
    # Docs dropped when budget is tight
    assert "README.md" not in text

    # Path filters include/exclude
    filtered_text, fnotes = budget_diff_by_risk(
        parsed,
        50_000,
        path_filters={"include": ["backend/**"], "exclude": ["**/service.py"]},
    )
    assert "backend/auth.py" in filtered_text
    assert "backend/service.py" not in filtered_text
    assert "README.md" not in filtered_text
    assert any("path_filters" in n for n in fnotes)


# ---------------------------------------------------------------------------
# 2. `.pragent.yaml` parser (PyYAML + pure-Python), base-branch loader & merge
# ---------------------------------------------------------------------------

COMPLEX_YAML = """\
# Repo configuration
profile: assertive # strict mode
merge_gate: required
score_threshold: 90
path_filters:
  include:
    - "backend/**"
    - "src/**"
  exclude:
    - "**/generated/**"
path_instructions:
  - path: "src-tauri/**"
    instructions: |-
      Always return Result<T, AppError>.
      Never call unwrap() in production code.
  - path: "backend/**"
    instructions: "Validate all external inputs: never trust #headers"
auto_review:
  drafts: true
  base_branches: [main, "release/*"]
  ignore_title_keywords: ["WIP", "DO NOT MERGE"]
memory:
  enabled: true
  auto_approve: true
chat:
  enabled: false
labels:
  enabled: true
  auto_apply: true
pre_merge_checks:
  - name: "Changelog updated"
    mode: error
    instructions: "Public API changes must update CHANGELOG.md"
tools: [ruff, gitleaks]
"""


@pytest.mark.parametrize("use_pyyaml", [True, False])
def test_parse_pragent_yaml_both_engines_match(use_pyyaml):
    cfg = parse_pragent_yaml(COMPLEX_YAML, use_pyyaml=use_pyyaml)
    assert cfg.profile == "assertive"
    assert cfg.merge_gate == "required"
    assert cfg.score_threshold == 90
    assert cfg.path_filters.include == ["backend/**", "src/**"]
    assert cfg.path_filters.exclude == ["**/generated/**"]
    assert len(cfg.path_instructions) == 2
    assert cfg.path_instructions[0].path == "src-tauri/**"
    assert "Always return Result<T, AppError>." in cfg.path_instructions[0].instructions
    assert "Never call unwrap()" in cfg.path_instructions[0].instructions
    assert cfg.path_instructions[1].instructions == "Validate all external inputs: never trust #headers"
    assert cfg.auto_review.drafts is True
    assert cfg.auto_review.base_branches == ["main", "release/*"]
    assert cfg.auto_review.ignore_title_keywords == ["WIP", "DO NOT MERGE"]
    assert cfg.memory.enabled is True and cfg.memory.auto_approve is True
    assert cfg.chat.enabled is False
    assert cfg.labels.enabled is True and cfg.labels.auto_apply is True
    assert len(cfg.pre_merge_checks) == 1
    assert cfg.pre_merge_checks[0].mode == "error"
    assert cfg.tools == ["ruff", "gitleaks"]


def test_render_pragent_yaml_round_trip():
    original = parse_pragent_yaml(COMPLEX_YAML, use_pyyaml=False)
    rendered = render_pragent_yaml(original)
    rt_pyyaml = parse_pragent_yaml(rendered, use_pyyaml=True)
    rt_pure = parse_pragent_yaml(rendered, use_pyyaml=False)
    assert rt_pyyaml.model_dump() == original.model_dump()
    assert rt_pure.model_dump() == original.model_dump()


def test_parse_pragent_yaml_supports_coderabbit_reviews_nesting_and_bang_filters():
    yaml_text = """\
reviews:
  profile: assertive
  path_filters:
    - "!dist/**"
    - "src/**"
"""
    cfg = parse_pragent_yaml(yaml_text, use_pyyaml=False)
    assert cfg.profile == "assertive"
    assert cfg.path_filters.include == ["src/**"]
    assert cfg.path_filters.exclude == ["dist/**"]


def test_load_repo_config_fetches_base_branch_only_and_caches_by_blob_sha():
    yaml_v1 = "profile: assertive\nscore_threshold: 92\n"
    yaml_v2 = "profile: chill\nscore_threshold: 75\n"
    state = {"version": 1}

    def handler(method, url, headers, params, body):
        # Security invariant: MUST always query base_ref ("main"), never head branch
        assert params.get("ref") == "main"
        if url.endswith("/contents/.pragent.yaml"):
            if state["version"] == 1:
                if headers.get("If-None-Match") == '"sha_blob_1"':
                    return _FakeResp(304)
                return _FakeResp(
                    200,
                    {
                        "sha": "sha_blob_1",
                        "content": base64.b64encode(yaml_v1.encode()).decode(),
                        "encoding": "base64",
                    },
                )
            return _FakeResp(
                200,
                {
                    "sha": "sha_blob_2",
                    "content": base64.b64encode(yaml_v2.encode()).decode(),
                    "encoding": "base64",
                },
            )
        return _FakeResp(404)

    client = _MockGitHubClient(handler)
    # 1st fetch: misses cache, parses & caches under datamodel.repo_config_key
    cfg1 = asyncio.run(load_repo_config("Acme/Repo", "main", "ghp_tok", client=client))
    assert cfg1.profile == "assertive" and cfg1.score_threshold == 92
    cached = store.rget_json(dm.repo_config_key("Acme/Repo"))
    assert isinstance(cached, dict) and cached["sha"] == "sha_blob_1"

    # 2nd fetch: sends If-None-Match and hits 304 cache
    cfg2 = asyncio.run(load_repo_config("Acme/Repo", "main", "ghp_tok", client=client))
    assert cfg2.profile == "assertive" and cfg2.score_threshold == 92
    assert client.requests[-1]["headers"].get("If-None-Match") == '"sha_blob_1"'

    # 3rd fetch: blob SHA changed on base branch -> refreshes cache
    state["version"] = 2
    cfg3 = asyncio.run(load_repo_config("Acme/Repo", "main", "ghp_tok", client=client))
    assert cfg3.profile == "chill" and cfg3.score_threshold == 75
    assert store.rget_json(dm.repo_config_key("Acme/Repo"))["sha"] == "sha_blob_2"


def test_load_repo_config_falls_back_to_yml_and_handles_invalid_yaml():
    def handler(method, url, headers, params, body):
        if url.endswith("/contents/.pragent.yaml"):
            return _FakeResp(404)
        if url.endswith("/contents/.pragent.yml"):
            return _FakeResp(
                200,
                {
                    "sha": "yml_sha",
                    "content": base64.b64encode(b"merge_gate: required\n").decode(),
                },
            )
        return _FakeResp(404)

    cfg = asyncio.run(load_repo_config("o/r", "main", "tok", client=_MockGitHubClient(handler)))
    assert cfg.merge_gate == "required"

    # Malformed config -> safe default, never raises
    bad_client = _MockGitHubClient(
        lambda *a: _FakeResp(200, {"sha": "bad", "content": base64.b64encode(b"profile: INVALID_MODE\n").decode()})
    )
    cfg_bad = asyncio.run(load_repo_config("o/r", "main", "tok", client=bad_client))
    assert cfg_bad.profile == "chill"

    # Unsafe base ref -> no HTTP calls made
    spy = _MockGitHubClient(lambda *a: (_ for _ in ()).throw(AssertionError("should not call")))
    cfg_unsafe = asyncio.run(load_repo_config("o/r", "--evil", "tok", client=spy))
    assert cfg_unsafe.profile == "chill" and not spy.requests


def test_merge_agent_and_repo_config_and_auto_review_filters():
    agent = {
        "id": "agent-1",
        "name": "Reviewer",
        "profile": "assertive",
        "merge_gate": "required",
        "score_threshold": 80,
        "memory_enabled": False,
        "chat_enabled": True,
        "path_instructions": [{"path": "backend/**", "instructions": "Check auth"}],
        "tools": ["ruff"],
    }
    # Absent repo config keeps agent v2 values
    m0 = merge_agent_and_repo_config(agent, None)
    assert m0["profile"] == "assertive"
    assert m0["merge_gate"] == "required"
    assert m0["score_threshold"] == 80
    assert m0["memory_enabled"] is False

    # Partial repo config overrides only explicitly set fields and combines path_instructions
    rc = parse_pragent_yaml(
        "score_threshold: 95\n"
        "memory:\n  auto_approve: true\n"
        "path_instructions:\n"
        "  - path: backend/**\n    instructions: Check auth\n"
        "  - path: src/**\n    instructions: No any\n"
        "tools: [gitleaks]\n",
        use_pyyaml=False,
    )
    m1 = merge_agent_and_repo_config(agent, rc)
    assert m1["profile"] == "assertive"  # inherited from agent
    assert m1["merge_gate"] == "required"  # inherited from agent
    assert m1["score_threshold"] == 95  # overridden by repo config
    assert m1["memory_enabled"] is False  # memory.enabled was NOT explicitly set in yaml
    assert m1["memory"]["auto_approve"] is True
    assert len(m1["path_instructions"]) == 2  # deduplicated union
    assert m1["tools"] == ["ruff", "gitleaks"]

    # Auto-review gating
    m2 = merge_agent_and_repo_config(
        agent,
        RepoConfig.model_validate(
            {"auto_review": {"drafts": False, "base_branches": ["main"], "ignore_title_keywords": ["WIP"]}}
        ),
    )
    assert should_auto_review({"draft": True, "base_ref": "main", "title": "Feat"}, m2)[0] is False
    assert should_auto_review({"draft": False, "base_ref": "dev", "title": "Feat"}, m2)[0] is False
    assert should_auto_review({"draft": False, "base_ref": "main", "title": "[wip] stuff"}, m2)[0] is False
    assert should_auto_review({"draft": False, "base_ref": "main", "title": "Fix bug"}, m2)[0] is True


# ---------------------------------------------------------------------------
# 3. JSON extraction, Pass A retry/fallback, Pass B prompt & Pass C verification
# ---------------------------------------------------------------------------


def test_extract_json_and_strip_code_fences():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Here is the JSON:\n```json\n{"a": 1, "b": [1, 2,],}\n```\nThanks!') == {"a": 1, "b": [1, 2]}
    assert extract_json('Prefix prose {"msg": "brace } inside string", "ok": true} suffix') == {
        "msg": "brace } inside string",
        "ok": True,
    }
    assert extract_json("not json at all") is None

    fenced = "```suggestion\n    if token:\n        return hmac.compare_digest(token, expected)\n```"
    assert strip_code_fences(fenced) == "    if token:\n        return hmac.compare_digest(token, expected)"
    assert strip_code_fences("```python\nreturn 1\n```") == "return 1"
    assert strip_code_fences("   \n  ") is None


def test_pass_a_walkthrough_retry_and_markdown_fallback():
    parsed = parse_unified_diff(SAMPLE_DIFF)
    pr_meta = {"repo": "o/r", "pr_number": 12, "title": "Auth update"}

    # 1. Retry succeeds on 2nd attempt
    responses = iter([
        "Sorry, let me think about this PR...",
        '```json\n{"summary": "Updates token check and adds greet helper.", "effort": 3, '
        '"file_table": [{"file": "backend/auth.py", "change_summary": "Adds DB query"}], '
        '"sequence_diagram": "```mermaid\\nsequenceDiagram\\nA->>B: verify\\n```"}\n```',
    ])
    calls: list[list[dict[str, str]]] = []

    async def mock_llm(messages, json_mode):
        calls.append(messages)
        return next(responses)

    wt = asyncio.run(generate_walkthrough(parsed, pr_meta, llm_call=mock_llm))
    assert len(calls) == 2
    assert wt["summary"] == "Updates token check and adds greet helper."
    assert wt["effort"] == 3
    assert wt["sequence_diagram"] == "sequenceDiagram\nA->>B: verify"
    assert wt["file_table"][0]["file"] == "backend/auth.py"

    # 2. Both attempts return plain markdown -> falls back cleanly without raising
    async def bad_llm(messages, json_mode):
        return "Plain markdown summary of the changes."

    logs: list[str] = []
    wt_fb = asyncio.run(generate_walkthrough(parsed, pr_meta, llm_call=bad_llm, logs=logs))
    assert wt_fb["summary"] == "Plain markdown summary of the changes."
    assert 1 <= wt_fb["effort"] <= 5
    assert len(wt_fb["file_table"]) == 3
    assert wt_fb["sequence_diagram"] is None
    assert any("falling back to markdown" in l for l in logs)


def test_pass_b_injects_path_instructions_and_memories_and_retries():
    parsed = parse_unified_diff(SAMPLE_DIFF)
    pr_meta = {"repo": "o/r", "pr_number": 12, "title": "Auth update", "sha": "abc1234"}
    captured_prompts: list[str] = []
    replies = iter([
        "Not JSON",
        json.dumps({
            "findings": [
                {
                    "file": "backend/auth.py",
                    "start_line": 13,
                    "end_line": 14,
                    "side": "RIGHT",
                    "severity": "high",  # alias -> critical
                    "category": "vuln",  # alias -> security
                    "title": "SQL injection in token lookup",
                    "body": "Interpolating token directly into SQL enables injection.",
                    "suggestion_patch": "```python\n    db.execute('SELECT * FROM tokens WHERE val = %s', (token,))\n```",
                    "confidence": 0.95,
                    "learning_ids": ["#mem42"],
                }
            ]
        }),
    ])

    async def mock_llm(messages, json_mode):
        captured_prompts.append(messages[0]["content"] + "\n" + messages[1]["content"])
        return next(replies)

    cfg = {
        "profile": "assertive",
        "path_instructions": [
            {"path": "backend/**", "instructions": "Always use parameterized SQL"},
            {"path": "docs/**", "instructions": "Unrelated docs instruction"},
        ],
    }
    memories = {
        "rules": [{"id": "mem42", "text": "Never build SQL queries with f-strings", "blocking": True}],
        "dismissed": [{"text": "Prefer single quotes"}],
    }

    findings = asyncio.run(
        generate_findings(parsed, pr_meta, config=cfg, memories=memories, llm_call=mock_llm)
    )
    assert len(captured_prompts) == 2
    prompt0 = captured_prompts[0]
    assert "Always use parameterized SQL" in prompt0
    assert "Unrelated docs instruction" not in prompt0
    assert "[#mem42] [BLOCKING] Never build SQL queries with f-strings" in prompt0
    assert "Prefer single quotes" in prompt0
    assert "[R13] +    query = f\"SELECT * FROM tokens" in prompt0

    assert len(findings) == 1
    assert isinstance(findings[0], dm.Finding)
    assert findings[0].severity == "critical"
    assert findings[0].category == "security"
    assert findings[0].learning_ids == ["mem42"]


def test_pass_c_verifier_hallucinations_fences_profiles_dismissals_and_fingerprints():
    parsed = parse_unified_diff(SAMPLE_DIFF)
    auth = parsed.get_file("backend/auth.py")
    assert auth is not None

    # Pre-existing open finding on src/new_file.py:2 that should be deduped
    existing_span = parsed.get_file("src/new_file.py").code_span(2, 2, "RIGHT")
    existing_fp = dm.finding_fingerprint("src/new_file.py", existing_span, "maintainability", "Empty name check")
    existing_findings = [
        {"fingerprint": existing_fp, "state": "open", "file": "src/new_file.py"},
        # Outdated finding should NOT block a fresh finding
        {
            "fingerprint": dm.finding_fingerprint("backend/auth.py", auth.code_span(13, 14, "RIGHT"), "security", "SQL injection"),
            "state": "outdated",
            "file": "backend/auth.py",
        },
    ]

    raw_findings = [
        # 1. Valid critical finding with fenced suggestion_patch
        {
            "file": "backend/auth.py",
            "start_line": 13,
            "end_line": 14,
            "side": "RIGHT",
            "severity": "critical",
            "category": "security",
            "title": "SQL injection",
            "body": "Use parameterized queries.",
            "suggestion_patch": "```suggestion\n    db.execute('SELECT * FROM tokens WHERE val = %s', (token,))\n```",
            "confidence": 0.96,
            "learning_ids": ["mem42"],
        },
        # 2. Hallucinated file not in diff -> dropped
        {
            "file": "backend/nonexistent.py",
            "start_line": 1,
            "end_line": 1,
            "side": "RIGHT",
            "severity": "major",
            "category": "bug",
            "title": "Ghost bug",
            "body": "Not in diff.",
            "confidence": 0.99,
        },
        # 3. Hallucinated line far outside diff hunks -> dropped
        {
            "file": "backend/auth.py",
            "start_line": 250,
            "end_line": 250,
            "side": "RIGHT",
            "severity": "major",
            "category": "bug",
            "title": "Out of hunk line",
            "body": "Line 250 does not exist in diff.",
            "confidence": 0.95,
        },
        # 4. Out-of-diff line WITH suggestion_patch (off by 1 from line 44 -> 45):
        #    MUST be dropped rather than snapping suggestion onto the wrong line!
        {
            "file": "backend/auth.py",
            "start_line": 45,
            "end_line": 45,
            "side": "RIGHT",
            "severity": "minor",
            "category": "bug",
            "title": "Bad suggestion line",
            "body": "Wrong target.",
            "suggestion_patch": "    return y",
            "confidence": 0.9,
        },
        # 5. Low confidence for chill (0.60 < 0.70), passes assertive (>= 0.50)
        {
            "file": "backend/auth.py",
            "start_line": 15,
            "end_line": 15,
            "side": "RIGHT",
            "severity": "major",
            "category": "security",
            "title": "Use constant-time comparison",
            "body": "Use hmac.compare_digest.",
            "confidence": 0.60,
        },
        # 6. Matches dismissed pattern -> suppressed
        {
            "file": "src/new_file.py",
            "start_line": 1,
            "end_line": 1,
            "side": "RIGHT",
            "severity": "minor",
            "category": "docs",
            "title": "Missing docstring on greet",
            "body": "Add a docstring explaining greet.",
            "confidence": 0.9,
        },
        # 7. Duplicate of already-open finding -> deduped
        {
            "file": "src/new_file.py",
            "start_line": 2,
            "end_line": 2,
            "side": "RIGHT",
            "severity": "minor",
            "category": "maintainability",
            "title": "Empty name check",
            "body": "Repeated finding.",
            "confidence": 0.85,
        },
        # 8-10. Three nits (chill caps at 2, assertive keeps up to 6)
        {
            "file": "src/new_file.py",
            "start_line": 3,
            "end_line": 3,
            "side": "RIGHT",
            "severity": "nit",
            "category": "style",
            "title": "Nit one",
            "body": "Small nit 1.",
            "confidence": 0.91,
        },
        {
            "file": "src/new_file.py",
            "start_line": 4,
            "end_line": 4,
            "side": "RIGHT",
            "severity": "nit",
            "category": "style",
            "title": "Nit two",
            "body": "Small nit 2.",
            "confidence": 0.88,
        },
        {
            "file": "backend/auth.py",
            "start_line": 43,
            "end_line": 43,
            "side": "RIGHT",
            "severity": "nit",
            "category": "style",
            "title": "Nit three unused y",
            "body": "Small nit 3.",
            "confidence": 0.72,
        },
    ]

    memories = {
        "dismissed": [
            {"text": "Ignore missing docstrings", "scope_globs": ["src/**"]},
        ]
    }

    chill_stats: dict[str, int] = {}
    chill_kept = verify_and_filter_findings(
        raw_findings,
        parsed,
        profile="chill",
        memories=memories,
        existing_findings=existing_findings,
        stats=chill_stats,
    )
    assert [f["title"] for f in chill_kept] == ["SQL injection", "Nit one", "Nit two"]
    # Fence stripped cleanly while preserving 4-space indentation
    assert chill_kept[0]["suggestion_patch"] == "    db.execute('SELECT * FROM tokens WHERE val = %s', (token,))"
    assert chill_kept[0]["fingerprint"]
    assert chill_stats["dropped_out_of_diff"] == 3
    assert chill_stats["dropped_low_confidence"] == 1
    assert chill_stats["dropped_dismissed"] == 1
    assert chill_stats["dropped_duplicate"] == 1
    assert chill_stats["dropped_nit_cap"] == 1

    # Assertive keeps the 0.60-confidence major AND all 3 nits (cap 6)
    assertive_kept = verify_and_filter_findings(
        raw_findings,
        parsed,
        profile="assertive",
        memories=memories,
        existing_findings=existing_findings,
    )
    titles = [f["title"] for f in assertive_kept]
    assert "Use constant-time comparison" in titles
    assert "Nit three unused y" in titles
    assert "Missing docstring on greet" not in titles  # still suppressed by dismissed memory


def test_pass_c_flips_deleted_line_side_and_clamps_cross_hunk_span():
    parsed = parse_unified_diff(SAMPLE_DIFF)
    # Deleted line 2 in old_removed.py accidentally tagged side=RIGHT -> flipped to LEFT
    # Multi-line span crossing from line 10 (hunk 1) to line 43 (hunk 2) -> clamped inside hunk 2
    raw = [
        {
            "file": "old_removed.py",
            "start_line": 2,
            "end_line": 2,
            "side": "RIGHT",
            "severity": "major",
            "category": "bug",
            "title": "Removed legacy entrypoint",
            "body": "Callers still import legacy().",
            "confidence": 0.9,
        },
        {
            "file": "backend/auth.py",
            "start_line": 10,
            "end_line": 43,
            "side": "RIGHT",
            "severity": "major",
            "category": "bug",
            "title": "Unused variable y",
            "body": "y is never returned.",
            "confidence": 0.9,
        },
    ]
    kept = verify_and_filter_findings(raw, parsed, profile="chill")
    assert len(kept) == 2
    by_title = {f["title"]: f for f in kept}
    assert by_title["Removed legacy entrypoint"]["side"] == "LEFT"
    # Clamped into hunk 2 valid lines (42..43)
    assert by_title["Unused variable y"]["start_line"] == 42
    assert by_title["Unused variable y"]["end_line"] == 43


def test_call_llm_openrouter_fallback_on_429(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-token")
    monkeypatch.setenv("LLM_MODEL", "vendor/primary-model:free")

    models_seen: list[str] = []

    def handler(method, url, headers, params, body):
        models_seen.append(body["model"])
        if body["model"] != OPENROUTER_FALLBACK_MODEL:
            return _FakeResp(429, {"error": {"message": "rate limited"}})
        return _FakeResp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})

    logs: list[str] = []
    out = asyncio.run(
        call_llm(
            [{"role": "user", "content": "hi"}],
            repo="o/r",
            pr_number=1,
            client=_MockGitHubClient(handler),
            logs=logs,
        )
    )
    assert out == '{"ok": true}'
    assert models_seen == ["vendor/primary-model:free", OPENROUTER_FALLBACK_MODEL]
    assert any(OPENROUTER_FALLBACK_MODEL in l for l in logs)


# ---------------------------------------------------------------------------
# 4. Publisher, inline suggestions, Redis lifecycle & HTTP 422 fallback
# ---------------------------------------------------------------------------


def test_inline_and_summary_rendering():
    finding = {
        "file": "backend/auth.py",
        "start_line": 13,
        "end_line": 14,
        "side": "RIGHT",
        "severity": "critical",
        "category": "security",
        "title": "SQL injection",
        "body": "Use parameterized query.",
        "suggestion_patch": "    db.execute('SELECT 1', (token,))",
        "learning_ids": ["42", "99"],
        "fingerprint": "0123456789abcdef0123456789abcdef",
    }
    inline = format_inline_comment(finding)
    assert "```suggestion\n    db.execute('SELECT 1', (token,))\n```" in inline
    assert "_Based on repo learning #42_" in inline
    assert "_Based on repo learning #99_" in inline
    assert "<!-- pr-agent-finding:0123456789abcdef0123456789abcdef -->" in inline

    # Suggestion patch containing triple backticks safely upgrades to 4-backtick fence
    nested = format_inline_comment({**finding, "suggestion_patch": 'doc = """```md\nhi\n```"""'})
    assert "````suggestion\n" in nested

    wt = {
        "summary": "Hardens auth token check.",
        "effort": 3,
        "file_table": [{"file": "backend/auth.py", "change_summary": "Token check"}],
        "sequence_diagram": "sequenceDiagram\nClient->>API: verify",
    }
    summary = render_summary_comment(
        wt,
        [finding],
        agent_key="rev-1",
        agent_name="Code Reviewer",
        head_sha="bbbb2222999",
        incremental_from="aaaa1111888",
        applied_learnings=[{"id": "42", "text": "Never use f-strings in SQL"}],
    )
    assert summary.startswith(review_marker("rev-1"))
    assert "Effort:**" in summary and "3/5" in summary
    assert "`aaaa111..bbbb222`" in summary
    assert "```mermaid\nsequenceDiagram\nClient->>API: verify\n```" in summary
    assert "| `backend/auth.py` | Token check |" in summary
    assert "`backend/auth.py:13-14`" in summary
    assert "**#42**: Never use f-strings in SQL" in summary


def test_redis_finding_lifecycle_and_outdated_marking():
    fp1 = dm.finding_fingerprint("a.py", "x = 1", "bug", "Bug one")
    fp2 = dm.finding_fingerprint("b.py", "y = 2", "major", "Bug two")
    save_pr_findings(
        "Acme/Repo",
        5,
        [
            {"fingerprint": fp1, "file": "a.py", "start_line": 10, "end_line": 12, "severity": "major", "category": "bug", "title": "Bug one"},
            {"fingerprint": fp2, "file": "b.py", "start_line": 5, "end_line": 5, "severity": "minor", "category": "style", "title": "Bug two"},
        ],
        head_sha="sha111",
    )
    assert store.rget_json(findings_index_key("Acme/Repo", 5)) == [fp1, fp2]
    assert len(list_pr_findings("Acme/Repo", 5)) == 2

    # Changing line 11 in a.py marks fp1 outdated, leaves fp2 open
    outdated = mark_outdated_findings("Acme/Repo", 5, {"a.py": {11, 12}})
    assert outdated == [fp1]
    assert [f["fingerprint"] for f in list_pr_findings("Acme/Repo", 5, state="open")] == [fp2]
    assert [f["fingerprint"] for f in list_pr_findings("Acme/Repo", 5, state="outdated")] == [fp1]

    # Set finding state to dismissed
    updated = set_finding_state("Acme/Repo", 5, fp2, "dismissed", reason="intentional", by="alice")
    assert updated is not None and updated["state"] == "dismissed" and updated["state_reason"] == "intentional"
    with pytest.raises(ValueError):
        set_finding_state("Acme/Repo", 5, fp2, "bogus")

    # Last reviewed SHA bookmark
    set_last_reviewed_sha("Acme/Repo", 5, "sha222")
    assert get_last_reviewed_sha("Acme/Repo", 5) == "sha222"
    assert store.rget(pr_last_reviewed_key("Acme/Repo", 5)) == "sha222"


def test_publish_review_records_comment_ids_and_handles_422_fallback():
    fp = dm.finding_fingerprint("backend/auth.py", "db.execute(query)", "security", "SQL injection")
    finding = {
        "fingerprint": fp,
        "file": "backend/auth.py",
        "start_line": 13,
        "end_line": 14,
        "side": "RIGHT",
        "severity": "critical",
        "category": "security",
        "title": "SQL injection",
        "body": "Use bind parameters.",
        "suggestion_patch": "    db.execute('SELECT 1', (token,))",
        "learning_ids": ["42"],
    }
    save_pr_findings("o/r", 9, [finding], head_sha="head999")
    wt = {"summary": "Auth change", "effort": 2, "file_table": [], "sequence_diagram": None}

    # Case 1: Happy path records inline comment IDs on stored findings
    def ok_handler(method, url, headers, params, body):
        if method == "POST" and url.endswith("/pulls/9/reviews"):
            assert body["event"] == "COMMENT"
            assert body["commit_id"] == "head999"
            assert body["comments"][0]["start_line"] == 13
            assert body["comments"][0]["line"] == 14
            return _FakeResp(201, {"id": 7001})
        if method == "GET" and url.endswith("/pulls/9/reviews/7001/comments"):
            return _FakeResp(
                200,
                [{
                    "id": 9001,
                    "path": "backend/auth.py",
                    "line": 14,
                    "side": "RIGHT",
                    "body": format_inline_comment(finding),
                    "html_url": "https://github.com/o/r/pull/9#discussion_r9001",
                }],
            )
        if method == "GET" and url.endswith("/issues/9/comments"):
            return _FakeResp(200, [])
        if method == "POST" and url.endswith("/issues/9/comments"):
            return _FakeResp(201, {"id": 8001})
        raise AssertionError((method, url))

    res = asyncio.run(
        publish_review(
            "o/r", 9, "head999", wt, [finding],
            agent_key="ag1", agent_name="PR Bot", github_token="tok",
            client=_MockGitHubClient(ok_handler),
        )
    )
    assert res["review_posted"] is True
    assert res["pr_comment_posted"] is True
    assert res["inline_fallback"] is False
    found = find_finding_by_comment_id("o/r", 9, 9001)
    assert found is not None and found["fingerprint"] == fp
    assert found["url"] == "https://github.com/o/r/pull/9#discussion_r9001"

    # Case 2: GitHub returns 422 on inline comments -> retries single-line, then moves into sticky summary
    review_attempts: list[dict] = []
    posted_summary_bodies: list[str] = []

    def handler_422(method, url, headers, params, body):
        if method == "POST" and url.endswith("/pulls/9/reviews"):
            review_attempts.append(body)
            return _FakeResp(422, {"message": "Line could not be resolved"})
        if method == "GET" and url.endswith("/issues/9/comments"):
            return _FakeResp(200, [{"id": 8001, "body": f"{review_marker('ag1')}\nold summary"}])
        if method == "PATCH" and url.endswith("/issues/comments/8001"):
            posted_summary_bodies.append(body["body"])
            return _FakeResp(200, {"id": 8001})
        raise AssertionError((method, url))

    res_422 = asyncio.run(
        publish_review(
            "o/r", 9, "head999", wt, [finding],
            agent_key="ag1", agent_name="PR Bot", github_token="tok",
            client=_MockGitHubClient(handler_422),
        )
    )
    assert len(review_attempts) == 2  # multi-line attempt + single-line retry
    assert "start_line" in review_attempts[0]["comments"][0]
    assert "start_line" not in review_attempts[1]["comments"][0]
    assert res_422["review_posted"] is False
    assert res_422["inline_fallback"] is True
    assert res_422["fallback_count"] == 1
    assert res_422["pr_comment_posted"] is True
    assert len(posted_summary_bodies) == 1
    assert "Unplaced inline findings" in posted_summary_bodies[0]
    assert "```suggestion" in posted_summary_bodies[0]
    assert "_Based on repo learning #42_" in posted_summary_bodies[0]


# ---------------------------------------------------------------------------
# 5. End-to-end golden test: Incremental review & dedupe across two pushes
# ---------------------------------------------------------------------------


def test_run_structured_review_incremental_dedupe_across_two_pushes():
    push1_diff = """\
diff --git a/backend/auth.py b/backend/auth.py
--- a/backend/auth.py
+++ b/backend/auth.py
@@ -10,3 +10,5 @@ def check(token):
     if not token:
         return False
+    db.execute(f"SELECT '{token}'")
+    return token == SECRET
"""
    # Push 2 fixes line 12 (SQLi) and adds a new bug in backend/billing.py,
    # while line 13 in auth.py stays unchanged (open finding must NOT be re-posted!).
    push2_inc_diff = """\
diff --git a/backend/auth.py b/backend/auth.py
--- a/backend/auth.py
+++ b/backend/auth.py
@@ -10,5 +10,5 @@ def check(token):
     if not token:
         return False
-    db.execute(f"SELECT '{token}'")
+    db.execute("SELECT %s", (token,))
     return token == SECRET
diff --git a/backend/billing.py b/backend/billing.py
new file mode 100644
--- /dev/null
+++ b/backend/billing.py
@@ -0,0 +1,3 @@
+def charge(amount):
+    assert amount > 0
+    return amount * 100
"""
    repo_yaml = "profile: assertive\nscore_threshold: 88\n"
    posted_reviews: list[dict] = []
    issue_comments: list[dict] = []
    next_id = {"n": 100}

    def gh_handler(method, url, headers, params, body):
        if method == "GET" and url.endswith("/contents/.pragent.yaml"):
            assert params.get("ref") == "main"
            return _FakeResp(200, {"sha": "cfgsha1", "content": base64.b64encode(repo_yaml.encode()).decode()})
        if method == "GET" and url.endswith("/pulls/42") and headers.get("Accept") == "application/vnd.github.diff":
            return _FakeResp(200, text=push1_diff)
        if method == "GET" and "/compare/sha_push_1...sha_push_2" in url:
            return _FakeResp(200, text=push2_inc_diff)
        if method == "POST" and url.endswith("/pulls/42/reviews"):
            next_id["n"] += 1
            rid = next_id["n"]
            posted_reviews.append(body)
            return _FakeResp(201, {"id": rid})
        if method == "GET" and "/pulls/42/reviews/" in url and url.endswith("/comments"):
            last_rev = posted_reviews[-1]
            return _FakeResp(
                200,
                [
                    {
                        "id": 5000 + i,
                        "path": c["path"],
                        "line": c["line"],
                        "side": c["side"],
                        "body": c["body"],
                        "html_url": f"https://github.com/acme/app/pull/42#discussion_r{5000 + i}",
                    }
                    for i, c in enumerate(last_rev["comments"])
                ],
            )
        if method == "GET" and url.endswith("/issues/42/comments"):
            return _FakeResp(200, list(issue_comments))
        if method == "POST" and url.endswith("/issues/42/comments"):
            next_id["n"] += 1
            rec = {"id": next_id["n"], "body": body["body"]}
            issue_comments.append(rec)
            return _FakeResp(201, rec)
        if method == "PATCH" and "/issues/comments/" in url:
            cid = int(url.rsplit("/", 1)[-1])
            for c in issue_comments:
                if c["id"] == cid:
                    c["body"] = body["body"]
                    return _FakeResp(200, c)
            return _FakeResp(404)
        raise AssertionError((method, url, headers, params))

    gh = _MockGitHubClient(gh_handler)
    agent = {"id": "cloud-rev", "name": "Cloud Reviewer", "profile": "chill"}
    memories = {
        "rules": [{"id": "rule7", "text": "Use hmac.compare_digest for secrets", "blocking": True}],
        "dismissed": [],
    }

    # --- Push 1 ---
    p1_replies = iter([
        json.dumps({
            "summary": "Adds token DB lookup and secret check.",
            "effort": 2,
            "file_table": [{"file": "backend/auth.py", "change_summary": "Checks token"}],
            "sequence_diagram": None,
        }),
        json.dumps({
            "findings": [
                {
                    "file": "backend/auth.py",
                    "start_line": 12,
                    "end_line": 12,
                    "side": "RIGHT",
                    "severity": "critical",
                    "category": "security",
                    "title": "SQL injection in check",
                    "body": "Parameterize SQL query.",
                    "suggestion_patch": '    db.execute("SELECT %s", (token,))',
                    "confidence": 0.95,
                },
                {
                    "file": "backend/auth.py",
                    "start_line": 13,
                    "end_line": 13,
                    "side": "RIGHT",
                    "severity": "major",
                    "category": "security",
                    "title": "Timing leak in token compare",
                    "body": "Use constant-time comparison.",
                    "confidence": 0.9,
                    "learning_ids": ["rule7"],
                },
            ]
        }),
    ])

    async def llm_p1(messages, json_mode):
        return next(p1_replies)

    res1 = asyncio.run(
        run_structured_review(
            agent,
            {
                "repo": "acme/app",
                "pr_number": 42,
                "sha": "sha_push_1",
                "base_ref": "main",
                "base_sha": "base000",
                "title": "Add token verification",
                "action": "opened",
                "github_token": "tok",
            },
            memories=memories,
            github_client=gh,
            llm_call=llm_p1,
        )
    )
    assert res1["provider"] == "review_only"
    assert res1["status"] == "completed"
    assert res1["cpu_seconds"] == 0.0
    assert res1["review_posted"] is True
    assert res1["pr_comment_posted"] is True
    assert res1["last_reviewed_sha"] == "sha_push_1"
    assert len(res1["new_findings"]) == 2
    assert len(res1["findings"]) == 2
    assert len(posted_reviews) == 1
    assert len(issue_comments) == 1
    assert "_Based on repo learning #rule7_" in posted_reviews[0]["comments"][1]["body"]

    # --- Push 2 (incremental) ---
    # LLM re-emits the unchanged timing-leak finding on line 13 + a genuinely new finding in billing.py
    p2_replies = iter([
        json.dumps({
            "summary": "Fixes SQL injection and adds billing charge helper.",
            "effort": 2,
            "file_table": [
                {"file": "backend/auth.py", "change_summary": "Parameterized SQL"},
                {"file": "backend/billing.py", "change_summary": "Added charge()"},
            ],
            "sequence_diagram": None,
        }),
        json.dumps({
            "findings": [
                {
                    "file": "backend/auth.py",
                    "start_line": 13,
                    "end_line": 13,
                    "side": "RIGHT",
                    "severity": "major",
                    "category": "security",
                    "title": "Timing leak in token compare",
                    "body": "Use constant-time comparison.",
                    "confidence": 0.9,
                    "learning_ids": ["rule7"],
                },
                {
                    "file": "backend/billing.py",
                    "start_line": 2,
                    "end_line": 2,
                    "side": "RIGHT",
                    "severity": "major",
                    "category": "bug",
                    "title": "Avoid assert for runtime validation",
                    "body": "Python -O strips assert statements.",
                    "suggestion_patch": "    if amount <= 0:\n        raise ValueError('amount must be positive')",
                    "confidence": 0.92,
                },
            ]
        }),
    ])

    async def llm_p2(messages, json_mode):
        return next(p2_replies)

    res2 = asyncio.run(
        run_structured_review(
            agent,
            {
                "repo": "acme/app",
                "pr_number": 42,
                "sha": "sha_push_2",
                "base_ref": "main",
                "base_sha": "base000",
                "title": "Add token verification",
                "action": "synchronize",
                "github_token": "tok",
            },
            memories=memories,
            github_client=gh,
            llm_call=llm_p2,
        )
    )
    assert res2["status"] == "completed"
    assert res2["last_reviewed_sha"] == "sha_push_2"
    # Old SQLi finding (line 12) was marked outdated!
    outdated = list_pr_findings("acme/app", 42, state="outdated")
    assert len(outdated) == 1 and outdated[0]["title"] == "SQL injection in check"
    # Unchanged timing-leak finding was deduped (NOT re-posted inline!), only billing.py finding is new!
    assert [f["title"] for f in res2["new_findings"]] == ["Avoid assert for runtime validation"]
    assert len(posted_reviews) == 2
    assert len(posted_reviews[1]["comments"]) == 1
    assert posted_reviews[1]["comments"][0]["path"] == "backend/billing.py"
    # All currently open findings = carried-over timing leak + new billing finding
    assert sorted(f["title"] for f in res2["findings"]) == [
        "Avoid assert for runtime validation",
        "Timing leak in token compare",
    ]
    # Sticky summary comment was PATCHed in place (still 1 sticky comment!) with incremental range
    assert len(issue_comments) == 1
    assert "`sha_pus..sha_pus`" in issue_comments[0]["body"] or "incremental" in issue_comments[0]["body"]


def test_publish_review_multiline_422_succeeds_on_single_line_retry():
    fp = dm.finding_fingerprint("backend/auth.py", "db.execute(query)", "security", "SQL injection")
    finding = {
        "fingerprint": fp,
        "file": "backend/auth.py",
        "start_line": 13,
        "end_line": 14,
        "side": "RIGHT",
        "severity": "critical",
        "category": "security",
        "title": "SQL injection",
        "body": "Use bind parameters.",
    }
    save_pr_findings("o/r", 15, [finding], head_sha="h15")
    wt = {"summary": "Fix", "effort": 1, "file_table": [], "sequence_diagram": None}
    attempts: list[dict] = []

    def handler(method, url, headers, params, body):
        if method == "POST" and url.endswith("/pulls/15/reviews"):
            attempts.append(body)
            if "start_line" in body["comments"][0]:
                return _FakeResp(422, {"message": "multi-line span invalid"})
            return _FakeResp(201, {"id": 333})
        if method == "GET" and url.endswith("/pulls/15/reviews/333/comments"):
            return _FakeResp(200, [{"id": 444, "path": "backend/auth.py", "line": 14, "side": "RIGHT", "body": format_inline_comment(finding)}])
        if method == "GET" and url.endswith("/issues/15/comments"):
            return _FakeResp(200, [])
        if method == "POST" and url.endswith("/issues/15/comments"):
            return _FakeResp(201, {"id": 555})
        raise AssertionError((method, url))

    res = asyncio.run(
        publish_review(
            "o/r", 15, "h15", wt, [finding],
            agent_key="a", agent_name="Bot", github_token="tok",
            client=_MockGitHubClient(handler),
        )
    )
    assert len(attempts) == 2
    assert "start_line" in attempts[0]["comments"][0]
    assert "start_line" not in attempts[1]["comments"][0]
    assert res["review_posted"] is True
    assert res["inline_fallback"] is False
    assert find_finding_by_comment_id("o/r", 15, 444)["fingerprint"] == fp


def test_pass_c_dismissed_scope_globs_and_stored_dismissed_dedupe():
    parsed = parse_unified_diff(SAMPLE_DIFF)
    auth = parsed.get_file("backend/auth.py")
    fp_dismissed = dm.finding_fingerprint("backend/auth.py", auth.code_span(43, 43, "RIGHT"), "style", "Unused y")
    raw = [
        {
            "file": "backend/auth.py",
            "start_line": 13,
            "end_line": 13,
            "side": "RIGHT",
            "severity": "major",
            "category": "docs",
            "title": "Missing docstring on verify_token",
            "body": "Add docstring.",
            "confidence": 0.9,
        },
        {
            "file": "backend/auth.py",
            "start_line": 43,
            "end_line": 43,
            "side": "RIGHT",
            "severity": "minor",
            "category": "style",
            "title": "Unused y",
            "body": "Remove unused y.",
            "confidence": 0.9,
        },
    ]
    # Dismissed pattern scoped ONLY to `src/**` must NOT suppress `backend/auth.py`!
    kept = verify_and_filter_findings(
        raw,
        parsed,
        profile="chill",
        memories={"dismissed": [{"text": "Ignore missing docstrings", "scope_globs": ["src/**"]}]},
        existing_findings=[{"fingerprint": fp_dismissed, "state": "dismissed"}],
    )
    assert [f["title"] for f in kept] == ["Missing docstring on verify_token"]


def test_run_structured_review_draft_skip_force_full_and_validation_errors():
    # Invalid repo fails cleanly
    bad = asyncio.run(run_structured_review({"id": "a"}, {"repo": "../evil", "pr_number": 1}, llm_call=lambda *a: None))
    assert bad["status"] == "failed"

    # Draft PR skipped by auto_review.drafts=false
    def draft_gh(method, url, headers, params, body):
        if "/contents/" in url:
            return _FakeResp(404)
        raise AssertionError(url)

    skipped = asyncio.run(
        run_structured_review(
            {"id": "a", "name": "Bot"},
            {
                "repo": "o/r",
                "pr_number": 3,
                "sha": "abc1111",
                "base_ref": "main",
                "draft": True,
                "action": "opened",
                "github_token": "tok",
            },
            github_client=_MockGitHubClient(draft_gh),
            llm_call=lambda *a: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
        )
    )
    assert skipped["status"] == "skipped"

    # force_full=True bypasses incremental compare even when last_reviewed_sha is set
    set_last_reviewed_sha("o/r", 3, "prev999")
    seen_urls: list[str] = []

    def full_gh(method, url, headers, params, body):
        seen_urls.append(url)
        if "/contents/" in url:
            return _FakeResp(404)
        if url.endswith("/pulls/3") and headers.get("Accept") == "application/vnd.github.diff":
            return _FakeResp(200, text=_file_diff("src/app.py", 3))
        if url.endswith("/issues/3/comments") and method == "GET":
            return _FakeResp(200, [])
        if url.endswith("/issues/3/comments") and method == "POST":
            return _FakeResp(201, {"id": 1})
        raise AssertionError((method, url))

    replies = iter([
        json.dumps({"summary": "Clean change.", "effort": 1, "file_table": [], "sequence_diagram": None}),
        json.dumps({"findings": []}),
    ])

    async def empty_llm(messages, json_mode):
        return next(replies)

    forced = asyncio.run(
        run_structured_review(
            {"id": "a", "name": "Bot"},
            {
                "repo": "o/r",
                "pr_number": 3,
                "sha": "head222",
                "base_ref": "main",
                "draft": True,
                "action": "synchronize",
                "github_token": "tok",
            },
            force_full=True,
            github_client=_MockGitHubClient(full_gh),
            llm_call=empty_llm,
        )
    )
    assert forced["status"] == "completed"
    assert forced["last_reviewed_sha"] == "head222"
    assert not any("/compare/" in u for u in seen_urls)
    assert any(u.endswith("/pulls/3") for u in seen_urls)

