"""Command Code / OpenCode Go provider selection, status flags, and the
review-only request shape. No test touches the network: httpx is replaced."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import main
from sandboxes import providers_review as pr

KEY_ENVS = (
    "LLM_PROVIDER", "LLM_MODEL", "OPENROUTER_API_KEY", "COMMANDCODE_API_KEY",
    "CMD_API_KEY", "OPENCODE_GO_API_KEY", "OPENCODE_API_KEY", "GITHUB_TOKEN",
    "BACKEND_API_TOKEN", "REVIEW_ENGINE_MODE",
)
FAKE_KEY = "test-key-1234567890"
DIFF = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -0,0 +1 @@\n+print(1)\n"


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main, "BACKEND_API_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")


class _Resp:
    def __init__(self, status_code=200, *, text="", data=None):
        self.status_code, self.text, self._data = status_code, text, data

    def json(self):
        return self._data


def _install_fake_httpx(monkeypatch, calls, *, llm_status=200):
    """Replace httpx.AsyncClient with a recorder. Every request is appended to `calls`."""
    structured_reply = '{"summary":"LGTM","effort":1,"file_table":[{"file":"src/app.py","change_summary":"Prints 1"}],"sequence_diagram":null,"findings":[]}'

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, params=None):
            calls.append({"method": "GET", "url": url, "headers": headers or {}})
            if "/contents/" in url:
                return _Resp(404, data={"message": "Not Found"})
            if url.endswith("/comments"):
                return _Resp(200, data=[])
            if url.endswith("/models"):
                return _Resp(llm_status)
            if url.endswith("/check-runs"):
                return _Resp(200, data={"check_runs": []})
            if url.endswith("/status"):
                return _Resp(200, data={"statuses": []})
            if url.endswith("/reviews"):
                return _Resp(200, data=[])
            return _Resp(200, text=DIFF, data={"head": {"sha": "abc"}, "base": {"ref": "main", "sha": "base0"}})

        async def post(self, url, headers=None, json=None):
            calls.append({"method": "POST", "url": url, "headers": headers or {}, "json": json})
            if url.endswith("/chat/completions"):
                return _Resp(llm_status, data={"choices": [{"message": {"content": structured_reply}}]})
            return _Resp(201, data={})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


def _forbid_network(monkeypatch):
    import httpx

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("network must not be used")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)


def _review(pr_number=7):
    agent = {"id": "agent-a", "name": "Agent A", "system_prompt": "sys"}
    ctx = {"repo": "owner/repo", "pr_number": pr_number, "sha": "abc", "action": "opened", "title": "T"}
    return asyncio.run(pr.ReviewOnlyProvider().run(agent, ctx))


def _llm_post(calls):
    return next(c for c in calls if c["method"] == "POST" and c["url"].endswith("/chat/completions"))


# --- provider resolution (no network) ---------------------------------------


def test_llm_provider_defaults_to_openrouter(monkeypatch):
    assert main._get_llm_provider() == "openrouter"  # nosec B101
    monkeypatch.setenv("LLM_PROVIDER", "  OpenCode_Go ")
    assert main._get_llm_provider() == "opencode_go"  # nosec B101
    monkeypatch.setenv("LLM_PROVIDER", "not-a-provider")
    assert main._get_llm_provider() == "openrouter"  # nosec B101


def test_key_resolution_uses_primary_then_alias(monkeypatch):
    assert pr.llm_key_for("commandcode") == ("", "COMMANDCODE_API_KEY")  # nosec B101
    monkeypatch.setenv("CMD_API_KEY", " alias-key ")
    assert pr.llm_key_for("commandcode") == ("alias-key", "CMD_API_KEY")  # nosec B101
    monkeypatch.setenv("COMMANDCODE_API_KEY", "primary-key")
    assert pr.llm_key_for("commandcode") == ("primary-key", "COMMANDCODE_API_KEY")  # nosec B101
    monkeypatch.setenv("OPENCODE_API_KEY", "oc-alias")
    assert pr.llm_key_for("opencode_go") == ("oc-alias", "OPENCODE_API_KEY")  # nosec B101


def test_status_model_uses_provider_default(monkeypatch):
    _forbid_network(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "commandcode")
    client = TestClient(main.app)
    body = client.get("/config/status").json()
    assert body["model"] == "openai/deepseek/deepseek-v4-flash"  # nosec B101

    # A stale OpenRouter route must not be reported (or sent) after switching.
    monkeypatch.setenv("LLM_MODEL", "openrouter/thinkingmachines/inkling-2-small:free")
    body = client.get("/config/status").json()
    assert body["model"] == "openai/deepseek/deepseek-v4-flash"  # nosec B101

    monkeypatch.setenv("LLM_MODEL", "openai/Qwen/Qwen3.7-Max")
    body = client.get("/config/status").json()
    assert body["model"] == "openai/Qwen/Qwen3.7-Max"  # nosec B101


def test_config_status_flags_without_network(monkeypatch):
    _forbid_network(monkeypatch)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_PROVIDER", "opencode_go")
    body = TestClient(main.app).get("/config/status").json()
    assert body["opencode_go"] == {"configured": True}  # nosec B101
    assert body["commandcode"] == {"configured": False}  # nosec B101
    assert body["openrouter"] == {"configured": False}  # nosec B101
    assert body["llm_provider"] == "opencode_go"  # nosec B101
    assert FAKE_KEY not in str(body)  # nosec B101


# --- POST /config/test (fake httpx) -----------------------------------------


def test_config_test_commandcode_checks_models_with_bearer(monkeypatch):
    calls = []
    _install_fake_httpx(monkeypatch, calls)
    monkeypatch.setenv("CMD_API_KEY", FAKE_KEY)
    body = TestClient(main.app).post("/config/test", json={"provider": "commandcode"}).json()
    assert body["ok"] is True and body["configured"] is True  # nosec B101
    assert calls == [{  # nosec B101
        "method": "GET",
        "url": "https://api.commandcode.ai/provider/v1/models",
        "headers": {"Authorization": f"Bearer {FAKE_KEY}"},
    }]
    assert FAKE_KEY not in str(body)  # nosec B101


def test_config_test_opencode_go_rejected_key(monkeypatch):
    calls = []
    _install_fake_httpx(monkeypatch, calls, llm_status=401)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", FAKE_KEY)
    body = TestClient(main.app).post("/config/test", json={"provider": "opencode_go"}).json()
    assert calls[0]["url"] == "https://opencode.ai/inference/go/v1/models"  # nosec B101
    assert body["ok"] is False and body["configured"] is True  # nosec B101
    assert "rejected" in body["details"] and FAKE_KEY not in str(body)  # nosec B101


def test_config_test_missing_key_names_env_var_and_skips_network(monkeypatch):
    _forbid_network(monkeypatch)
    client = TestClient(main.app)
    cc = client.post("/config/test", json={"provider": "commandcode"}).json()
    oc = client.post("/config/test", json={"provider": "opencode_go"}).json()
    assert cc["configured"] is False and "COMMANDCODE_API_KEY" in cc["details"]  # nosec B101
    assert oc["configured"] is False and "OPENCODE_GO_API_KEY" in oc["details"]  # nosec B101


# --- review-only request shape (fake httpx) ----------------------------------


def test_review_openrouter_request_is_unchanged(monkeypatch):
    calls = []
    _install_fake_httpx(monkeypatch, calls)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o")  # OpenRouter ids keep their prefix
    logs = _review()["logs"]
    post = _llm_post(calls)
    assert post["url"] == pr.OPENROUTER_URL  # nosec B101
    assert post["json"]["model"] == "openai/gpt-4o"  # nosec B101
    assert post["headers"] == {"Authorization": f"Bearer {FAKE_KEY}", "Content-Type": "application/json"}  # nosec B101
    assert not any(FAKE_KEY in line for line in logs)  # nosec B101


def test_review_commandcode_strips_prefix_and_uses_alias_key(monkeypatch):
    calls = []
    _install_fake_httpx(monkeypatch, calls)
    monkeypatch.setenv("LLM_PROVIDER", "commandcode")
    monkeypatch.setenv("CMD_API_KEY", FAKE_KEY)
    result = _review()
    post = _llm_post(calls)
    assert post["url"] == "https://api.commandcode.ai/provider/v1/chat/completions"  # nosec B101
    assert post["json"]["model"] == "deepseek/deepseek-v4-flash"  # nosec B101
    assert post["headers"] == {"Authorization": f"Bearer {FAKE_KEY}", "Content-Type": "application/json"}  # nosec B101
    assert result["status"] == "completed"  # nosec B101


def test_review_opencode_go_sends_stable_session_header(monkeypatch):
    calls = []
    _install_fake_httpx(monkeypatch, calls)
    monkeypatch.setenv("LLM_PROVIDER", "opencode_go")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "openai/kimi-k3")
    _review(pr_number=7)
    post = _llm_post(calls)
    assert post["url"] == "https://opencode.ai/inference/go/openai/v1/chat/completions"  # nosec B101
    assert post["json"]["model"] == "kimi-k3"  # nosec B101
    session = post["headers"]["x-opencode-session"]
    assert session == pr.opencode_session_id("owner/repo", 7)  # nosec B101
    assert session.startswith("ses_") and len(session) == 4 + 24  # nosec B101
    assert pr.opencode_session_id("owner/repo", 8) != session  # nosec B101


def test_missing_key_error_names_env_var_not_value(monkeypatch):
    _forbid_network(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "opencode_go")
    result = _review()
    assert result["status"] == "failed"  # nosec B101
    assert any("OPENCODE_GO_API_KEY missing" in line for line in result["logs"])  # nosec B101


@pytest.mark.parametrize("provider", ["openrouter", "commandcode", "opencode_go"])
@pytest.mark.parametrize("model", [pr.DEFAULT_LLM_MODEL, "openrouter/custom", "openai/custom"])
def test_review_saved_model_fallback(monkeypatch, provider, model):
    calls = []
    _install_fake_httpx(monkeypatch, calls)
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv(pr.LLM_PROVIDERS[provider]["key_envs"][0], FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", model)
    _review()
    expected = model
    if provider != "openrouter":
        if model == pr.DEFAULT_LLM_MODEL or model.startswith("openrouter/"):
            expected = pr.LLM_PROVIDERS[provider]["default_model"]
        expected = expected.removeprefix("openai/")
    assert _llm_post(calls)["json"]["model"] == expected  # nosec B101


def test_openrouter_vendor_prefix_is_stripped_while_routers_stay():
    assert pr.normalize_openrouter_model("openrouter/thinkingmachines/inkling-2-small:free") == "thinkingmachines/inkling-2-small:free"  # nosec B101
    assert pr.normalize_openrouter_model("openrouter/free") == "openrouter/free"  # nosec B101
    assert pr.normalize_openrouter_model("openrouter/auto") == "openrouter/auto"  # nosec B101
    assert pr.normalize_openrouter_model("openai/gpt-4o") == "openai/gpt-4o"  # nosec B101


def test_glob_matches_preserves_dot_prefixed_paths_and_supports_brackets():
    assert pr._glob_matches(".github/workflows/ci.yml", ".github/**")  # nosec B101
    assert not pr._glob_matches("github/workflows/ci.yml", ".github/**")  # nosec B101
    assert pr._glob_matches("./.github/workflows/ci.yml", "./.github/**")  # nosec B101
    assert pr._glob_matches("src/a.py", "src/[ab].py")  # nosec B101
    assert not pr._glob_matches("src/c.py", "src/[ab].py")  # nosec B101
    assert pr._glob_matches("src/c.py", "src/[!ab].py")  # nosec B101


def test_review_normalizes_openrouter_model_uses_default_prompt_and_formats_heading(monkeypatch):
    calls = []
    _install_fake_httpx(monkeypatch, calls)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "openrouter/thinkingmachines/inkling-2-small:free")
    agent = {
        "id": "agent-a",
        "name": "Agent A",
        "system_prompt": "",
        "profile": "assertive",
        "path_instructions": [
            {"path": "src/**/*.py", "instructions": "Always validate inputs."},
            {"path": "docs/**", "instructions": "Ignore me."},
        ],
    }
    ctx = {"repo": "owner/repo", "pr_number": 7, "sha": "abc", "action": "opened", "title": "T"}
    result = asyncio.run(pr.ReviewOnlyProvider().run(agent, ctx))
    assert result["status"] == "completed"  # nosec B101
    llm_posts = [c for c in calls if c["method"] == "POST" and c["url"].endswith("/chat/completions")]
    assert len(llm_posts) == 2  # Pass A walkthrough + Pass B findings  # nosec B101
    for post in llm_posts:
        assert post["json"]["model"] == "thinkingmachines/inkling-2-small:free"  # nosec B101
    pass_b_sys = llm_posts[1]["json"]["messages"][0]["content"]
    assert pr.DEFAULT_SYSTEM_PROMPT in pass_b_sys  # nosec B101
    assert "assertive" in pass_b_sys  # nosec B101
    assert "Always validate inputs." in pass_b_sys  # nosec B101
    assert "Ignore me." not in pass_b_sys  # nosec B101
    review_comment_post = next(
        c for c in calls
        if c["method"] == "POST" and c["url"].endswith("/comments") and pr.review_marker("agent-a") in c["json"]["body"]
    )
    assert "## 🤖 Agent A — Code Review" in review_comment_post["json"]["body"]  # nosec B101


def test_review_legacy_single_pass_compatibility(monkeypatch):
    calls = []
    _install_fake_httpx(monkeypatch, calls)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "openrouter/thinkingmachines/inkling-2-small:free")
    agent = {
        "id": "agent-a",
        "name": "Agent A",
        "system_prompt": "",
        "profile": "assertive",
        "legacy_single_pass": True,
        "path_instructions": [
            {"path": "src/**/*.py", "instructions": "Always validate inputs."},
            {"path": "docs/**", "instructions": "Ignore me."},
        ],
    }
    ctx = {"repo": "owner/repo", "pr_number": 7, "sha": "abc", "action": "opened", "title": "T"}
    result = asyncio.run(pr.ReviewOnlyProvider().run(agent, ctx))
    assert result["status"] == "completed"  # nosec B101
    post = _llm_post(calls)
    assert post["json"]["messages"][0]["content"] == pr.DEFAULT_SYSTEM_PROMPT  # nosec B101
    user_msg = post["json"]["messages"][1]["content"]
    assert "assertive" in user_msg and "Always validate inputs." in user_msg and "Ignore me." not in user_msg  # nosec B101
    comment_post = next(c for c in calls if c["method"] == "POST" and c["url"].endswith("/comments"))
    assert "\n## 🤖 Agent A (review_only, 0 CPU-sec)\n" in comment_post["json"]["body"]  # nosec B101


def test_review_openrouter_retries_with_free_router_on_429(monkeypatch):
    calls = []
    structured_reply = '{"summary":"Looks good","effort":1,"file_table":[],"sequence_diagram":null,"findings":[]}'

    class _FallbackClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None, params=None):
            calls.append(("GET", url, headers or {}))
            if "/contents/" in url:
                return _Resp(404, data={})
            if url.endswith("/comments"):
                return _Resp(200, data=[])
            return _Resp(200, text=DIFF, data={"head": {"sha": "abc"}, "base": {"ref": "main"}})

        async def post(self, url, headers=None, json=None):
            calls.append(("POST", url, json))
            if url.endswith("/chat/completions"):
                if json["model"] != pr.OPENROUTER_FALLBACK_MODEL:
                    return _Resp(429, text='{"error":{"message":"rate limited"}}', data={"error": {"message": "rate limited"}})
                return _Resp(200, data={"choices": [{"message": {"content": structured_reply}}]})
            return _Resp(201, data={})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FallbackClient)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("LLM_MODEL", "openrouter/thinkingmachines/inkling-2-small:free")
    result = _review()
    assert result["status"] == "completed"  # nosec B101
    llm_posts = [c[2]["model"] for c in calls if c[0] == "POST" and c[1].endswith("/chat/completions")]
    assert llm_posts == [
        "thinkingmachines/inkling-2-small:free",
        "openrouter/free",
        "thinkingmachines/inkling-2-small:free",
        "openrouter/free",
    ]  # nosec B101


def test_review_falls_back_to_pr_files_on_406_large_diff(monkeypatch):
    calls = []

    class _LargeDiffClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            calls.append(("GET", url, headers or {}))
            if url.endswith("/pulls/7") and (headers or {}).get("Accept") == "application/vnd.github.diff":
                return _Resp(406, text="diff too large")
            if url.endswith("/pulls/7/files"):
                return _Resp(200, data=[{"filename": "src/main.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x = 1"}])
            if url.endswith("/comments"):
                return _Resp(200, data=[])
            return _Resp(200, data={"head": {"sha": "abc"}})

        async def post(self, url, headers=None, json=None):
            calls.append(("POST", url, json))
            if url.endswith("/chat/completions"):
                return _Resp(
                    200,
                    data={
                        "choices": [
                            {
                                "message": {
                                    "content": '{"summary":"Reviewed files","effort":1,"file_table":[],"sequence_diagram":null,"findings":[]}'
                                }
                            }
                        ]
                    },
                )
            return _Resp(201, data={})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _LargeDiffClient)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    result = _review()
    assert result["status"] == "completed"  # nosec B101
    assert "src/main.py" in result["diff"]  # nosec B101
    assert any("falling back to PR files patches" in line for line in result["logs"])  # nosec B101
