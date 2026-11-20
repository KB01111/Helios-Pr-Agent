"""Agent config v2: defaults, migration of stored v1 agents, validation, and
PUTs from older clients that do not know the new fields."""
import pytest
from fastapi.testclient import TestClient

import main
import store


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("BACKEND_API_TOKEN", raising=False)
    return TestClient(main.app)


def test_new_agent_gets_v2_defaults(client):
    agent = client.post("/agents", json={"name": "Rev"}).json()
    assert agent["schema_version"] == 2 and agent["profile"] == "chill"  # nosec B101
    assert agent["memory_enabled"] is True and agent["merge_gate"] == "advisory"  # nosec B101
    assert agent["score_threshold"] == 85 and agent["autofix"] == "suggest"  # nosec B101


def test_stored_v1_agents_read_back_with_defaults(client):
    store.rset_json("agent:old", {"id": "old", "name": "Old", "trigger": "on_commit", "runtime_mode": "review_only"})
    got = client.get("/agents/old").json()
    assert got["merge_gate"] == "advisory" and got["path_instructions"] == []  # nosec B101
    assert client.get("/agents").json()[0]["schema_version"] == 2  # nosec B101


def test_v1_client_put_keeps_stored_v2_settings(client):
    created = client.post("/agents", json={
        "name": "Rev", "merge_gate": "required", "score_threshold": 70,
        "path_instructions": [{"path": "src/**", "instructions": "No unwrap()"}],
    }).json()
    # An older desktop build sends only the v1 fields.
    v1 = {k: created[k] for k in ("name", "system_prompt", "target_repos", "trigger", "trigger_label", "runtime_mode")}
    v1["name"] = "Renamed"
    updated = client.put(f"/agents/{created['id']}", json=v1).json()
    assert updated["name"] == "Renamed"  # nosec B101
    assert updated["merge_gate"] == "required" and updated["score_threshold"] == 70  # nosec B101
    assert updated["path_instructions"][0]["instructions"] == "No unwrap()"  # nosec B101
    # An explicit value still wins.
    again = client.put(f"/agents/{created['id']}", json={**v1, "merge_gate": "off"}).json()
    assert again["merge_gate"] == "off"  # nosec B101


@pytest.mark.parametrize("bad", [
    {"score_threshold": 101},
    {"merge_gate": "maybe"},
    {"tools": ["Ruff; rm -rf /"]},
    {"path_instructions": [{"path": "", "instructions": "x"}]},
    {"autofix": "force"},
])
def test_invalid_v2_settings_are_rejected(client, bad):
    assert client.post("/agents", json={"name": "x", **bad}).status_code == 422  # nosec B101


def test_config_status_reports_github_auth_mode(client, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    gh = client.get("/config/status").json()["github"]
    assert gh["auth_mode"] == "pat" and gh["app_configured"] is False  # nosec B101
    assert "pull_request_review_thread" in gh["events"]  # nosec B101
