"""Cloud settings saved from the desktop app, and the GitHub App health/repair endpoints."""
import asyncio

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import github_app
import main
import runtime_settings as rs
import store

TOKEN = "t" * 40
FAKE_KEY = "sk-or-test-1234567890"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_MODEL", "OPENROUTER_API_KEY", "COMMANDCODE_API_KEY", "CMD_API_KEY",
                 "OPENCODE_GO_API_KEY", "OPENCODE_API_KEY", "GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET",
                 "SETTINGS_ENCRYPTION_KEY", "BACKEND_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main, "BACKEND_API_TOKEN", "")


@pytest.fixture()
def secured(monkeypatch):
    monkeypatch.setenv("BACKEND_API_TOKEN", TOKEN)
    return {"Authorization": f"Bearer {TOKEN}"}


def _client():
    return TestClient(main.app)


def test_plain_settings_apply_without_a_token(monkeypatch):
    out = rs.update({"LLM_PROVIDER": "commandcode", "LLM_MODEL": "openai/Qwen/Qwen3.7-Max"})
    assert out["values"] == {"LLM_PROVIDER": "commandcode", "LLM_MODEL": "openai/Qwen/Qwen3.7-Max"}  # nosec B101
    assert main._get_llm_provider() == "commandcode"  # nosec B101
    assert main._get_llm_model() == "openai/Qwen/Qwen3.7-Max"  # nosec B101


def test_secrets_need_an_encryption_secret():
    with pytest.raises(rs.SettingsNotReady):
        rs.update({"OPENROUTER_API_KEY": FAKE_KEY})


def test_secret_is_encrypted_at_rest_and_never_described(secured):
    out = rs.update({"OPENROUTER_API_KEY": FAKE_KEY})
    assert main.os.environ["OPENROUTER_API_KEY"] == FAKE_KEY  # nosec B101
    raw = str(store.rget_json(rs.SETTINGS_KEY))
    assert FAKE_KEY not in raw and "enc" in raw  # nosec B101
    assert FAKE_KEY not in str(out)  # nosec B101
    assert out["secrets"]["OPENROUTER_API_KEY"] == {"set": True, "cloud": True}  # nosec B101


def test_other_worker_loads_stored_values_and_delete_restores_env(monkeypatch, secured):
    monkeypatch.setenv("LLM_MODEL", "from-process-env")
    rs.update({"LLM_MODEL": "stored/model", "OPENROUTER_API_KEY": FAKE_KEY})
    # A second worker only has the shared store.
    rs.reset_for_tests()
    monkeypatch.setenv("LLM_MODEL", "from-process-env")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    rs.refresh(force=True)
    assert main.os.environ["LLM_MODEL"] == "stored/model"  # stored wins over env  # nosec B101
    assert main.os.environ["OPENROUTER_API_KEY"] == FAKE_KEY  # nosec B101
    rs.update({"LLM_MODEL": "", "OPENROUTER_API_KEY": None})
    assert main.os.environ["LLM_MODEL"] == "from-process-env"  # nosec B101
    assert "OPENROUTER_API_KEY" not in main.os.environ  # nosec B101


def test_rotated_token_reports_undecryptable_instead_of_crashing(monkeypatch, secured):
    rs.update({"OPENROUTER_API_KEY": FAKE_KEY})
    rs.reset_for_tests()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("BACKEND_API_TOKEN", "x" * 40)
    rs.refresh(force=True)
    assert "OPENROUTER_API_KEY" not in main.os.environ  # nosec B101
    assert rs.describe()["errors"]  # nosec B101


@pytest.mark.parametrize(
    "changes",
    [
        {"LLM_PROVIDER": "nope"},
        {"LLM_MODEL": "bad model\nid"},
        {"GITHUB_APP_ID": "abc"},
        {"BACKEND_API_TOKEN": "x"},  # not remotely editable
        {"REDIS_URL": "redis://x"},  # chicken-and-egg: stays an env var
        {"OPENROUTER_API_KEY": "has space"},
        {"GITHUB_APP_PRIVATE_KEY": "not a pem"},
        {},
    ],
)
def test_invalid_changes_are_rejected(secured, changes):
    with pytest.raises(rs.SettingsError):
        rs.update(changes)


def test_pem_is_stored_on_one_line(secured):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    rs.update({"GITHUB_APP_ID": "42", "GITHUB_APP_PRIVATE_KEY": pem})
    assert "\n" not in main.os.environ["GITHUB_APP_PRIVATE_KEY"]  # nosec B101
    assert github_app.app_configured()  # nosec B101
    assert github_app.app_jwt()  # nosec B101  # the stored key really signs


def test_api_requires_auth_and_roundtrips(secured):
    c = _client()
    assert c.get("/config/settings").status_code == 401  # nosec B101
    assert c.put("/config/settings", json={"values": {"LLM_MODEL": "m/x"}}).status_code == 401  # nosec B101
    r = c.put("/config/settings", headers=secured, json={"values": {"LLM_PROVIDER": "opencode_go", "OPENCODE_GO_API_KEY": FAKE_KEY}})
    assert r.status_code == 200, r.text  # nosec B101
    assert FAKE_KEY not in r.text  # nosec B101
    st = c.get("/config/status", headers=secured).json()
    assert st["llm_provider"] == "opencode_go" and st["opencode_go"]["configured"] is True  # nosec B101
    assert "OPENCODE_GO_API_KEY" in st["settings"]["cloud_keys"]  # nosec B101
    assert c.put("/config/settings", headers=secured, json={"values": {"LLM_PROVIDER": "bogus"}}).status_code == 422  # nosec B101


def test_api_conflict_when_backend_cannot_encrypt():
    r = _client().put("/config/settings", json={"values": {"OPENROUTER_API_KEY": FAKE_KEY}})
    assert r.status_code == 409  # nosec B101


def test_webhook_signature_uses_cloud_stored_secret(secured):
    import hashlib
    import hmac

    rs.update({"GITHUB_APP_WEBHOOK_SECRET": "app-secret-value"})
    body = b'{"zen":"ok"}'
    sig = "sha256=" + hmac.new(b"app-secret-value", body, hashlib.sha256).hexdigest()
    r = _client().post("/webhooks/github", content=body, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"})
    assert r.status_code != 401  # nosec B101
    bad = _client().post("/webhooks/github", content=body, headers={"X-Hub-Signature-256": "sha256=00"})
    assert bad.status_code == 401  # nosec B101


# ---------------------------------------------------------------------------
# GitHub App health / webhook repair
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class _Client:
    def __init__(self, routes, calls):
        self.routes, self.calls = routes, calls

    async def aclose(self):
        return None

    async def _do(self, method, url, headers=None, **kw):
        self.calls.append((method, url, kw))
        return self.routes[(method, url)]

    async def get(self, url, headers=None, **kw):
        return await self._do("GET", url, headers, **kw)

    async def patch(self, url, headers=None, **kw):
        return await self._do("PATCH", url, headers, **kw)


@pytest.fixture()
def app_creds(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    monkeypatch.setenv("GITHUB_APP_ID", "77")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem.replace("\n", "\\n"))


def _routes(permissions=None, events=None, hook_url="https://api.example.org/webhooks/github", installs=None):
    perms = {"metadata": "read", "contents": "read", "pull_requests": "write", "issues": "write",
             "checks": "write", "statuses": "write"}
    return {
        ("GET", "https://api.github.com/app"): _Resp(200, {
            "slug": "pr-agent", "name": "PR Agent", "html_url": "https://github.com/apps/pr-agent",
            "permissions": perms if permissions is None else permissions,
            "events": list(github_app.REQUIRED_EVENTS) if events is None else events,
        }),
        ("GET", "https://api.github.com/app/hook/config"): _Resp(200, {"url": hook_url}),
        ("GET", "https://api.github.com/app/installations"): _Resp(
            200, [{"id": 1, "account": {"login": "me"}, "repository_selection": "all"}] if installs is None else installs),
    }


def test_health_ok(monkeypatch, app_creds):
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(github_app.httpx, "AsyncClient", lambda *a, **k: _Client(_routes(), []))
    out = asyncio.run(github_app.health())
    assert out["ok"] is True and out["problems"] == []  # nosec B101
    assert out["installations"][0]["account"] == "me"  # nosec B101


def test_health_reports_every_gap(monkeypatch, app_creds):
    routes = _routes(
        permissions={"metadata": "read", "contents": "read", "pull_requests": "read", "checks": "write"},
        events=["pull_request"], hook_url="https://example.com/webhooks/github", installs=[],
    )
    monkeypatch.setattr(github_app.httpx, "AsyncClient", lambda *a, **k: _Client(routes, []))
    out = asyncio.run(github_app.health())
    text = " | ".join(out["problems"])
    assert out["ok"] is False  # nosec B101
    for expected in ("pull_requests: needs write", "issues: needs write", "statuses: needs write",
                     "not subscribed to events", "placeholder", "GITHUB_APP_WEBHOOK_SECRET", "not installed"):
        assert expected in text, expected  # nosec B101


def test_health_without_credentials():
    out = asyncio.run(github_app.health())
    assert out["configured"] is False and out["ok"] is False  # nosec B101


def test_set_webhook_url_patches_hook_config(monkeypatch, app_creds):
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "s3cret")
    calls = []
    routes = {("PATCH", "https://api.github.com/app/hook/config"): _Resp(200, {})}
    monkeypatch.setattr(github_app.httpx, "AsyncClient", lambda *a, **k: _Client(routes, calls))
    out = asyncio.run(github_app.set_webhook_url("https://api.example.org/"))
    assert out == {"url": "https://api.example.org/webhooks/github", "secret_synced": True}  # nosec B101
    sent = calls[0][2]["json"]
    assert sent["url"] == out["url"] and sent["secret"] == "s3cret" and sent["content_type"] == "json"  # nosec B101


@pytest.mark.parametrize("url", ["http://x.example.org", "https://localhost:8000", "https://127.0.0.1",
                                 "https://10.0.0.5", "https://a.example.org?x=1", "https://u:p@a.example.org", ""])
def test_set_webhook_url_refuses_unreachable_targets(url):
    with pytest.raises(ValueError):
        github_app.public_https_url(url)


def test_health_endpoints_need_auth(secured, monkeypatch, app_creds):
    c = _client()
    assert c.get("/github-app/health").status_code == 401  # nosec B101
    assert c.post("/github-app/webhook", json={"base_url": "https://a.example.org"}).status_code == 401  # nosec B101
    monkeypatch.setattr(github_app.httpx, "AsyncClient", lambda *a, **k: _Client(_routes(), []))
    assert c.get("/github-app/health", headers=secured).json()["configured"] is True  # nosec B101
    assert c.post("/github-app/webhook", headers=secured, json={"base_url": "http://nope"}).status_code == 422  # nosec B101
