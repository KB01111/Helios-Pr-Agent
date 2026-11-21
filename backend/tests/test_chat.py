"""Tests for PR chat & feedback loop (`backend/chat.py`): command parsing,
collaborator authorization gate, remember/dismiss/resolve/review/readiness/help/ask
flows, and GitHub review-thread vs issue-comment reply routing."""
from __future__ import annotations

import asyncio

import pytest

import chat
import datamodel as dm
import memory
import store
from sandboxes import providers_review as pr

REPO = "acme/widget"
FAKE_LLM_KEY = "sk-test-secret-987654321"
SAMPLE_DIFF = (
    "diff --git a/backend/auth.py b/backend/auth.py\n"
    "--- a/backend/auth.py\n"
    "+++ b/backend/auth.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def verify(tok):\n"
    "+    if not tok:\n"
    "+        return False\n"
    "     return tok == 'ok'\n"
)


class _Resp:
    def __init__(self, status_code: int = 200, *, data=None, text: str = ""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


def _install_fake_httpx(
    monkeypatch,
    calls: list[dict],
    *,
    review_reply_status: int = 201,
    issue_comment_status: int = 201,
    llm_responses: list[_Resp] | None = None,
    pr_title: str = "Harden auth verification",
    pr_diff: str = SAMPLE_DIFF,
):
    queue = list(llm_responses or [_Resp(200, data={"choices": [{"message": {"content": "It guards empty tokens safely."}}]})])

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            calls.append({"method": "GET", "url": url, "headers": headers or {}})
            accept = (headers or {}).get("Accept", "")
            if accept == "application/vnd.github.diff":
                return _Resp(200, text=pr_diff)
            return _Resp(200, data={"title": pr_title, "number": 7})

        async def post(self, url, headers=None, json=None):
            calls.append({"method": "POST", "url": url, "headers": headers or {}, "json": json})
            if url.endswith("/chat/completions"):
                return queue.pop(0) if len(queue) > 1 else queue[0]
            if url.endswith("/replies"):
                return _Resp(review_reply_status, data={"id": 901})
            if url.endswith("/issues/7/comments") or url.endswith("/comments"):
                return _Resp(issue_comment_status, data={"id": 902})
            return _Resp(201, data={})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _forbid_network(monkeypatch):
    import httpx

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network must not be called")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "OPENROUTER_API_KEY",
        "COMMANDCODE_API_KEY",
        "CMD_API_KEY",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_API_KEY",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-pat-token")


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------


def test_parse_agent_command_all_variants(monkeypatch):
    assert chat.parse_agent_command("Just a normal comment") is None  # nosec B101
    assert chat.parse_agent_command("email me@pr-agent.dev please") is None  # nosec B101
    assert chat.parse_agent_command("@pr-agent") == ("help", "")  # nosec B101
    assert chat.parse_agent_command("@pr-agent help") == ("help", "")  # nosec B101
    assert chat.parse_agent_command("@pr-agent: remember Always use UTC") == ("remember", "Always use UTC")  # nosec B101
    assert chat.parse_agent_command("Hey @pr-agent, learn Never log tokens") == ("remember", "Never log tokens")  # nosec B101
    assert chat.parse_agent_command("@pr-agent dismiss intentional legacy API") == ("dismiss", "intentional legacy API")  # nosec B101
    assert chat.parse_agent_command("@pr-agent wontfix test fixture only") == ("dismiss", "test fixture only")  # nosec B101
    assert chat.parse_agent_command("@pr-agent won't fix intentional") == ("dismiss", "intentional")  # nosec B101
    assert chat.parse_agent_command("@pr-agent resolve") == ("resolve", "")  # nosec B101
    assert chat.parse_agent_command("@pr-agent /re-review") == ("review", "")  # nosec B101
    assert chat.parse_agent_command("@pr-agent review") == ("review", "")  # nosec B101
    assert chat.parse_agent_command("@pr-agent readiness") == ("readiness", "")  # nosec B101
    assert chat.parse_agent_command("@pr-agent score") == ("readiness", "")  # nosec B101
    assert chat.parse_agent_command("@pr-agent ask Why is this risky?") == ("ask", "Why is this risky?")  # nosec B101
    assert chat.parse_agent_command("@pr-agent Why is this risky?") == ("ask", "Why is this risky?")  # nosec B101

    # Custom GitHub App slug mention support
    monkeypatch.setenv("GITHUB_APP_SLUG", "helios-review")
    assert chat.parse_agent_command("@helios-review remember Use strict types") == ("remember", "Use strict types")  # nosec B101
    assert chat.parse_agent_command("@helios-review[bot] score") == ("readiness", "")  # nosec B101

    # Mentions inside self-reply markers, inline backticks, code fences, or blockquotes are ignored
    assert chat.parse_agent_command(f"@pr-agent help\n\n{chat.CHAT_REPLY_MARKER}") is None  # nosec B101
    assert chat.parse_agent_command("Use `@pr-agent remember foo` to teach the agent.") is None  # nosec B101
    assert chat.parse_agent_command("> @pr-agent review\nSounds good!") is None  # nosec B101
    assert chat.parse_agent_command("```\n@pr-agent dismiss bad\n```") is None  # nosec B101
    # Genuine commands preserve backticks inside their arguments
    assert chat.parse_agent_command(
        "> quoted context\n@pr-agent remember Never use `unwrap()` in `src-tauri`"
    ) == ("remember", "Never use `unwrap()` in `src-tauri`")  # nosec B101


# ---------------------------------------------------------------------------
# Authorization gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("association", ["NONE", "FIRST_TIME_CONTRIBUTOR", "CONTRIBUTOR", "MANNEQUIN", ""])
def test_untrusted_associations_are_strictly_rejected(monkeypatch, association):
    _forbid_network(monkeypatch)
    triggered = []
    res = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent remember Malicious prompt injection",
            author="stranger",
            association=association,
            trigger_review_fn=lambda *a: triggered.append(a),
        )
    )
    assert res["handled"] is False  # nosec B101
    assert res["authorized"] is False  # nosec B101
    assert res["reason"] == "unauthorized_association"  # nosec B101
    assert memory.list_memories(REPO) == []  # nosec B101
    assert triggered == []  # nosec B101


def test_bot_author_rejected_even_with_collaborator_association(monkeypatch):
    _forbid_network(monkeypatch)
    res = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent remember Never do X",
            author="some-helper[bot]",
            association="COLLABORATOR",
        )
    )
    assert res["handled"] is False and res["authorized"] is False  # nosec B101
    assert res["reason"] == "bot_author"  # nosec B101
    assert memory.list_memories(REPO) == []  # nosec B101


# ---------------------------------------------------------------------------
# Remember / Learn flow (inline review thread + fallback to issue comment)
# ---------------------------------------------------------------------------


def test_remember_on_review_thread_scopes_to_path_and_replies_inline(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls)

    res = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent remember Always check HMAC in constant time",
            author="alice",
            association="OWNER",
            comment_id=555,
            in_reply_to=500,
            path="backend/auth.py",
            is_review_comment=True,
        )
    )
    assert res["handled"] is True and res["command"] == "remember"  # nosec B101
    assert res["reply_posted"] is True and res["reply_kind"] == "review_comment_reply"  # nosec B101
    mem = res["memory"]
    assert mem["scope_globs"] == ["backend/auth.py"]  # nosec B101
    assert mem["source"] == "chat" and mem["author"] == "alice" and mem["status"] == "active"  # nosec B101
    assert mem["id"] in res["reply"] and "backend/auth.py" in res["reply"]  # nosec B101

    post = next(c for c in calls if c["method"] == "POST")
    assert post["url"].endswith(f"/repos/{REPO}/pulls/7/comments/500/replies")  # nosec B101


def test_review_comment_reply_falls_back_to_issue_comment_on_error(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls, review_reply_status=404, issue_comment_status=201)

    res = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent learn Prefer structured logging",
            author="bob",
            association="MEMBER",
            comment_id=777,
            is_review_comment=True,
        )
    )
    assert res["reply_posted"] is True and res["reply_kind"] == "issue_comment"  # nosec B101
    assert res["memory"]["scope_globs"] == ["**"]  # nosec B101
    urls = [c["url"] for c in calls if c["method"] == "POST"]
    assert urls[0].endswith(f"/repos/{REPO}/pulls/7/comments/777/replies")  # nosec B101
    assert urls[1].endswith(f"/repos/{REPO}/issues/7/comments")  # nosec B101


# ---------------------------------------------------------------------------
# Dismiss & Resolve flows
# ---------------------------------------------------------------------------


def test_dismiss_matches_finding_by_in_reply_to_and_learns_pattern(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls)

    fp = dm.finding_fingerprint("backend/auth.py", "tok == 'ok'", "security", "Use constant-time comparison")
    store.rset_json(
        dm.finding_key(REPO, 7, fp),
        {
            "fingerprint": fp,
            "file": "backend/auth.py",
            "start_line": 4,
            "end_line": 4,
            "severity": "major",
            "category": "security",
            "title": "Use constant-time comparison",
            "state": "open",
            "comment_id": 888,
        },
        ex=dm.FINDING_TTL_S,
    )

    res = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent wontfix test-only dummy token",
            author="alice",
            association="COLLABORATOR",
            comment_id=899,
            in_reply_to=888,
            path="backend/auth.py",
            is_review_comment=True,
        )
    )
    assert res["command"] == "dismiss" and res["reply_posted"] is True  # nosec B101
    assert res["dismissal"]["finding_updated"] is True  # nosec B101
    assert res["dismissal"]["pattern_created"] is True  # nosec B101
    stored = store.rget_json(dm.finding_key(REPO, 7, fp))
    assert isinstance(stored, dict) and stored["state"] == "dismissed"  # nosec B101
    assert res["dismissal"]["memory"]["id"] in res["reply"]  # nosec B101


def test_resolve_marks_finding_resolved_by_path(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls)

    fp = dm.finding_fingerprint("backend/auth.py", "if not tok", "bug", "Handle None token")
    store.rset_json(
        dm.finding_key(REPO, 7, fp),
        {
            "fingerprint": fp,
            "file": "backend/auth.py",
            "start_line": 2,
            "end_line": 2,
            "severity": "minor",
            "category": "bug",
            "title": "Handle None token",
            "state": "open",
        },
        ex=dm.FINDING_TTL_S,
    )

    res = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent resolve",
            author="alice",
            association="OWNER",
            path="backend/auth.py",
        )
    )
    assert res["command"] == "resolve" and res["resolved"] is True  # nosec B101
    stored = store.rget_json(dm.finding_key(REPO, 7, fp))
    assert isinstance(stored, dict) and stored["state"] == "resolved"  # nosec B101
    assert store.rget(dm.dirty_key(REPO, 7)) is not None  # nosec B101


# ---------------------------------------------------------------------------
# Review, Readiness & Help flows
# ---------------------------------------------------------------------------


def test_review_readiness_and_help_commands(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls)

    review_invocations: list[tuple[str, int]] = []

    async def trigger_review(repo, pr_num):
        review_invocations.append((repo, pr_num))
        return {"status": "queued", "run_id": "run_123"}

    res_rev = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent re-review",
            author="alice",
            association="OWNER",
            trigger_review_fn=trigger_review,
        )
    )
    assert review_invocations == [(REPO, 7)]  # nosec B101
    assert "run_123" in res_rev["reply"] and res_rev["reply_posted"] is True  # nosec B101

    def recompute_readiness(repo, pr_num):
        return dm.ReadinessReport(
            repo=repo,
            pr_number=pr_num,
            head_sha="a" * 40,
            score=42,
            band="blocked",
            items=[dm.ReadinessItem(kind="blocker", code="ci_fail", text="CI test suite failed")],
        )

    res_ready = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent score",
            author="alice",
            association="MEMBER",
            recompute_readiness_fn=recompute_readiness,
        )
    )
    assert "42/100" in res_ready["reply"] and "blocked" in res_ready["reply"]  # nosec B101
    assert "CI test suite failed" in res_ready["reply"]  # nosec B101

    res_help = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent help",
            author="alice",
            association="COLLABORATOR",
        )
    )
    assert res_help["command"] == "help" and "remember" in res_help["reply"] and "dismiss" in res_help["reply"]  # nosec B101


# ---------------------------------------------------------------------------
# Ask flow (LLM grounded in PR diff + repo memory + OpenRouter fallback)
# ---------------------------------------------------------------------------


def test_ask_grounds_in_pr_diff_and_memory_with_openrouter_fallback(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(
        monkeypatch,
        calls,
        llm_responses=[
            _Resp(429, data={"error": {"message": f"rate limited for {FAKE_LLM_KEY}"}}),
            _Resp(200, data={"choices": [{"message": {"content": "Yes, empty tokens now return False cleanly."}}]}),
        ],
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_LLM_KEY)
    monkeypatch.setenv("LLM_MODEL", "openrouter/thinkingmachines/inkling-2-small:free")

    mem = memory.add_memory(
        REPO,
        {
            "text": "Auth helpers must fail closed when tokens are empty.",
            "scope_globs": ["backend/**"],
        },
    )

    res = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent Does this handle empty tokens safely?",
            author="alice",
            association="OWNER",
            path="backend/auth.py",
        )
    )
    assert res["command"] == "ask" and res["ask"]["ok"] is True  # nosec B101
    assert res["ask"]["model"] == pr.OPENROUTER_FALLBACK_MODEL  # nosec B101
    assert mem["id"] in res["ask"]["memory_ids"]  # nosec B101
    assert res["reply"] == "Yes, empty tokens now return False cleanly."  # nosec B101
    assert res["reply_posted"] is True  # nosec B101
    assert FAKE_LLM_KEY not in res["reply"]  # nosec B101

    llm_calls = [c for c in calls if c["method"] == "POST" and c["url"].endswith("/chat/completions")]
    assert [c["json"]["model"] for c in llm_calls] == [
        "thinkingmachines/inkling-2-small:free",
        pr.OPENROUTER_FALLBACK_MODEL,
    ]  # nosec B101
    user_prompt = llm_calls[0]["json"]["messages"][1]["content"]
    assert "Harden auth verification" in user_prompt  # nosec B101
    assert "Auth helpers must fail closed" in user_prompt  # nosec B101
    assert "if not tok:" in user_prompt  # nosec B101


def test_edge_cases_empty_remember_cached_readiness_missing_llm_key_and_no_mention(monkeypatch):
    # No mention -> untouched, no network
    _forbid_network(monkeypatch)
    no_mention = asyncio.run(
        chat.handle_pr_mention(REPO, 7, body="Just chatting here", author="alice", association="OWNER")
    )
    assert no_mention == {"handled": False, "reason": "no_mention"}  # nosec B101

    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls)

    # Empty `remember` does not create memory
    empty_rem = asyncio.run(
        chat.handle_pr_mention(REPO, 7, body="@pr-agent remember", author="alice", association="OWNER")
    )
    assert empty_rem["command"] == "remember" and empty_rem["created"] is False  # nosec B101
    assert memory.list_memories(REPO) == []  # nosec B101

    # `dismiss` without stored finding still records pattern when reason is provided
    synth_dis = asyncio.run(
        chat.handle_pr_mention(
            REPO,
            7,
            body="@pr-agent dismiss intentional legacy shim",
            author="alice",
            association="OWNER",
            path="backend/legacy.py",
        )
    )
    assert synth_dis["dismissal"]["pattern_created"] is True  # nosec B101

    # `readiness` without callback reads cached merge report from Redis
    store.rset_json(
        dm.merge_report_key(REPO, 7),
        {"repo": REPO, "pr_number": 7, "head_sha": "a" * 40, "score": 91, "band": "ready", "items": []},
    )
    cached_ready = asyncio.run(
        chat.handle_pr_mention(REPO, 7, body="@pr-agent readiness", author="alice", association="OWNER")
    )
    assert "91/100" in cached_ready["reply"] and "ready" in cached_ready["reply"]  # nosec B101

    # `ask` without LLM key replies gracefully naming the env var
    missing_key = asyncio.run(
        chat.handle_pr_mention(REPO, 7, body="@pr-agent ask what changed?", author="alice", association="OWNER")
    )
    assert missing_key["ask"]["ok"] is False and "OPENROUTER_API_KEY" in missing_key["reply"]  # nosec B101

    # `ask` with opencode_go sends stable session header and stripped model id
    monkeypatch.setenv("LLM_PROVIDER", "opencode_go")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", FAKE_LLM_KEY)
    monkeypatch.setenv("LLM_MODEL", "openai/kimi-k3")
    oc_res = asyncio.run(
        chat.handle_pr_mention(REPO, 7, body="@pr-agent ask explain verify()", author="alice", association="OWNER")
    )
    assert oc_res["ask"]["ok"] is True and oc_res["ask"]["provider"] == "opencode_go"  # nosec B101
    oc_post = next(c for c in calls if c["method"] == "POST" and c["url"].endswith("/chat/completions"))
    assert oc_post["json"]["model"] == "kimi-k3"  # nosec B101
    assert oc_post["headers"]["x-opencode-session"] == pr.opencode_session_id(REPO, 7)  # nosec B101


def test_pat_mode_self_reply_loop_empty_ask_and_sender_is_bot(monkeypatch):
    calls: list[dict] = []
    _install_fake_httpx(monkeypatch, calls)

    # 1. `help` reply carries invisible self-reply marker in GitHub POST payload
    res_help = asyncio.run(
        chat.handle_pr_mention(REPO, 7, body="@pr-agent help", author="alice", association="OWNER")
    )
    assert res_help["reply_posted"] is True  # nosec B101
    posted_body = calls[-1]["json"]["body"]
    assert chat.CHAT_REPLY_MARKER in posted_body  # nosec B101

    # 2. Feeding the posted reply back as a PAT-mode OWNER webhook MUST NOT re-trigger!
    _forbid_network(monkeypatch)
    echoed = asyncio.run(
        chat.handle_pr_mention(REPO, 7, body=posted_body, author="alice", association="OWNER")
    )
    assert echoed == {"handled": False, "authorized": False, "reason": "self_reply"}  # nosec B101
    assert memory.list_memories(REPO) == []  # nosec B101

    # 3. `sender_is_bot=True` rejects bot accounts even without `[bot]` suffix
    bot_res = asyncio.run(
        chat.handle_pr_mention(
            REPO, 7, body="@pr-agent remember Never do Y", author="ci-automation", association="OWNER", sender_is_bot=True
        )
    )
    assert bot_res["handled"] is False and bot_res["reason"] == "bot_author"  # nosec B101
    assert memory.list_memories(REPO) == []  # nosec B101

    # 4. Empty `@pr-agent ask` short-circuits without fetching PR diff or calling LLM
    ask_calls: list[dict] = []
    _install_fake_httpx(monkeypatch, ask_calls)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_LLM_KEY)
    empty_ask = asyncio.run(
        chat.handle_pr_mention(REPO, 7, body="@pr-agent ask", author="alice", association="OWNER")
    )
    assert empty_ask["command"] == "ask" and empty_ask["ask"] == {"ok": False, "reason": "empty_question"}  # nosec B101
    assert not any(c["method"] == "GET" for c in ask_calls)  # nosec B101
    assert not any(c["url"].endswith("/chat/completions") for c in ask_calls)  # nosec B101
    assert empty_ask["reply_posted"] is True  # nosec B101
    assert chat.CHAT_REPLY_MARKER in ask_calls[-1]["json"]["body"]  # nosec B101


