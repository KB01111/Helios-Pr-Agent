"""GitHub App credentials: JWT, installation-token minting/caching, PAT fallback."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import datamodel as dm
import github_app
import store


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, key.public_key()


@pytest.fixture()
def app_env(monkeypatch, keypair):
    pem, _pub = keypair
    monkeypatch.setenv("GITHUB_APP_ID", "1234")
    # One-line PEM with literal \n, as .env files carry it.
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem.replace("\n", "\\n"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, routes, calls):
        self.routes, self.calls = routes, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        # Nothing to release: the fake client holds no connections.
        return None

    async def _do(self, method, url, headers=None, **_):
        self.calls.append((method, url, headers or {}))
        return self.routes[(method, url)]

    async def get(self, url, headers=None, **kw):
        return await self._do("GET", url, headers, **kw)

    async def post(self, url, headers=None, **kw):
        return await self._do("POST", url, headers, **kw)


def install_fake(monkeypatch, routes):
    calls = []
    monkeypatch.setattr(github_app.httpx, "AsyncClient", lambda *a, **k: FakeClient(routes, calls))
    return calls


def test_auth_mode(monkeypatch, app_env):
    assert github_app.auth_mode() == "app"  # nosec B101
    monkeypatch.delenv("GITHUB_APP_ID")
    assert github_app.auth_mode() == "none"  # nosec B101
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    assert github_app.auth_mode() == "pat"  # nosec B101


def test_app_jwt_is_rs256_with_backdated_iat(app_env, keypair):
    now = time.time()
    token = github_app.app_jwt(now)
    claims = jwt.decode(token, keypair[1], algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "1234"  # nosec B101
    assert claims["iat"] == int(now) - 60 and claims["exp"] - claims["iat"] <= 600  # nosec B101


def test_bad_key_raises_without_leaking(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----")
    with pytest.raises(github_app.GitHubAppError) as err:
        github_app.app_jwt()
    assert "not-a-key" not in str(err.value)  # nosec B101


def test_installation_token_is_minted_once_and_cached(monkeypatch, app_env):
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    routes = {
        ("GET", "https://api.github.com/repos/o/r/installation"): FakeResp(200, {"id": 99}),
        ("POST", "https://api.github.com/app/installations/99/access_tokens"):
            FakeResp(201, {"token": "ghs_abc", "expires_at": expires}),
    }
    calls = install_fake(monkeypatch, routes)
    assert asyncio.run(github_app.token_for_repo("o/r")) == ("ghs_abc", "app")  # nosec B101
    assert asyncio.run(github_app.token_for_repo("O/R")) == ("ghs_abc", "app")  # nosec B101
    assert [c[0] for c in calls] == ["GET", "POST"]  # installation + token, both cached  # nosec B101
    assert calls[0][2]["Authorization"].startswith("Bearer ey")  # App JWT, never the token  # nosec B101
    assert store.rget("ghinst:o/r") == "99"  # nosec B101
    assert "ghs_abc" not in str(store._MEM)  # tokens stay out of the shared store  # nosec B101


def test_expiring_token_is_refreshed(monkeypatch, app_env):
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
    github_app.remember_installation("o/r", 5)
    routes = {("POST", "https://api.github.com/app/installations/5/access_tokens"):
              FakeResp(201, {"token": "ghs_1", "expires_at": soon})}
    calls = install_fake(monkeypatch, routes)
    asyncio.run(github_app.token_for_repo("o/r"))
    asyncio.run(github_app.token_for_repo("o/r"))
    assert len(calls) == 2  # inside the refresh skew -> minted again  # nosec B101


def test_not_installed_or_refused_falls_back_to_pat(monkeypatch, app_env):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")
    routes = {("GET", "https://api.github.com/repos/o/r/installation"): FakeResp(404, {})}
    install_fake(monkeypatch, routes)
    assert asyncio.run(github_app.token_for_repo("o/r")) == ("ghp_fallback", "pat")  # nosec B101
    github_app.remember_installation("o/r", 7)
    routes[("POST", "https://api.github.com/app/installations/7/access_tokens")] = FakeResp(403, {})
    assert asyncio.run(github_app.token_for_repo("o/r")) == ("ghp_fallback", "pat")  # nosec B101
    monkeypatch.delenv("GITHUB_TOKEN")
    assert asyncio.run(github_app.token_for_repo("o/r")) == ("", "none")  # nosec B101


def test_installation_cache_keys_are_case_insensitive():
    github_app.remember_installation("Owner/Repo", 3)
    assert github_app.cached_installation("owner/repo") == 3  # nosec B101
    github_app.forget_installation("OWNER/REPO")
    assert github_app.cached_installation("owner/repo") is None  # nosec B101
    github_app.remember_installation("o/r", "junk")
    assert github_app.cached_installation("o/r") is None  # nosec B101


def test_finding_fingerprint_is_stable_across_whitespace_and_wording_tail():
    a = dm.finding_fingerprint("a.py", "x = foo( 1 )\n", "bug", "Possible None dereference of foo result here")
    b = dm.finding_fingerprint("a.py", "x  =  foo( 1 )", "bug", "possible none dereference of foo result here!")
    c = dm.finding_fingerprint("b.py", "x = foo( 1 )", "bug", "Possible None dereference of foo result here")
    assert a == b != c and len(a) == 32  # nosec B101
    assert dm.finding_key("o/r", 3, a).startswith("finding:o/r:3:")  # nosec B101
    with pytest.raises(ValueError):
        dm.finding_key("o/r", 3, "../x")
    with pytest.raises(ValueError):
        dm.activity_key("../r", 1)


def test_installation_cache_failure_falls_back_to_pat(monkeypatch, app_env):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")

    def broken(_key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(github_app.store, "rget", broken)
    assert asyncio.run(github_app.token_for_repo("o/r")) == ("ghp_fallback", "pat")  # nosec B101
