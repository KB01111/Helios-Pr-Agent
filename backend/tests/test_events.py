"""Event router: non-review GitHub events are recorded, deduped and never break
the existing pull_request review path."""
import pytest
from fastapi.testclient import TestClient

import datamodel as dm
import events
import main
import store

REPO = "o/r"
SHA = "c" * 40


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("BACKEND_API_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(main, "GITHUB_WEBHOOK_SECRET", "")
    return TestClient(main.app)


def send(client, event, payload, delivery=None):
    headers = {"X-GitHub-Event": event}
    if delivery:
        headers["X-GitHub-Delivery"] = delivery
    return client.post("/webhooks/github", json=payload, headers=headers)


def pr_payload(action, number=7, sha=SHA, **extra):
    pr = {"number": number, "title": "t", "head": {"sha": sha, "ref": "feat"}, **extra}
    return {"action": action, "pull_request": pr, "repository": {"full_name": REPO}, "sender": {"type": "User"}}


def test_ping_and_unhandled_events_are_acked(client):
    assert send(client, "ping", {"zen": "x"}).json() == {"pong": True}  # nosec B101
    body = send(client, "star", {"action": "created", "repository": {"full_name": REPO}}).json()
    assert body["skipped"] == "unhandled-event"  # nosec B101


def test_closed_pr_is_recorded_and_clears_the_readiness_marker(client):
    store.rset(dm.dirty_key(REPO, 7), "x")
    body = send(client, "pull_request", pr_payload("closed", merged=True, merge_commit_sha="m" * 40), "d1").json()
    assert body["skipped"] == "closed" and body["recorded"] == 1  # nosec B101
    act = events.pr_activity(REPO, 7)
    assert act[0]["kind"] == "pull_request" and act[0]["merged"] is True  # nosec B101
    assert not events.is_dirty(REPO, 7)  # nosec B101


def test_review_thread_and_check_events_mark_the_pr_dirty(client):
    review = {**pr_payload("submitted"), "review": {"id": 1, "state": "APPROVED", "user": {"login": "amy"}}}
    assert send(client, "pull_request_review", review, "d2").json()["recorded"] == 1  # nosec B101
    thread = {**pr_payload("resolved"), "thread": {"node_id": "T1", "comments": [{"id": 9, "path": "a.py"}]}}
    assert send(client, "pull_request_review_thread", thread, "d3").json()["recorded"] == 1  # nosec B101
    suite = {
        "action": "completed",
        "check_suite": {"conclusion": "failure", "head_sha": SHA, "app": {"slug": "github-actions"},
                        "pull_requests": [{"number": 7}, {"number": 8}]},
        "repository": {"full_name": REPO},
        "sender": {"type": "Bot"},  # CI results come from bots and must still count
    }
    assert send(client, "check_suite", suite, "d4").json()["prs"] == [7, 8]  # nosec B101
    kinds = [a["kind"] for a in events.pr_activity(REPO, 7)]
    assert kinds == ["check_suite", "thread", "review"]  # newest first  # nosec B101
    assert events.pr_activity(REPO, 7)[2]["state"] == "approved"  # nosec B101
    assert events.is_dirty(REPO, 7) and events.is_dirty(REPO, 8)  # nosec B101


def test_own_check_runs_do_not_loop(client, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "my-agent")
    run = {
        "action": "completed",
        "check_run": {"name": "PR Agent · Merge readiness", "app": {"slug": "my-agent"},
                      "pull_requests": [{"number": 7}]},
        "repository": {"full_name": REPO},
    }
    assert send(client, "check_run", run, "d5").json()["recorded"] == 0  # nosec B101
    assert not events.is_dirty(REPO, 7)  # nosec B101


def test_status_events_map_to_the_pr_through_its_head_sha(client):
    send(client, "pull_request", pr_payload("ready_for_review"), "d6")  # records head -> PR
    status = {"sha": SHA, "state": "failure", "context": "ci/lint", "repository": {"full_name": REPO}}
    assert send(client, "status", status, "d7").json()["prs"] == [7]  # nosec B101
    unknown = {**status, "sha": "f" * 40}
    assert send(client, "status", unknown, "d8").json()["skipped"] == "unknown-head"  # nosec B101


def test_comments_detect_mentions_and_skip_bots_and_plain_issues(client, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_SLUG", "helios-review")
    base = {"action": "created", "repository": {"full_name": REPO}, "sender": {"type": "User"}}
    on_pr = {**base, "issue": {"number": 7, "pull_request": {}},
             "comment": {"id": 1, "body": "@helios-review please re-check", "user": {"login": "amy"}}}
    assert send(client, "issue_comment", on_pr, "d9").json()["mentions_agent"] is True  # nosec B101
    plain = {**on_pr, "comment": {"id": 2, "body": "email me@pr-agent.dev", "user": {"login": "amy"}}}
    assert send(client, "issue_comment", plain, "d10").json()["mentions_agent"] is False  # nosec B101
    issue = {**base, "issue": {"number": 3}, "comment": {"id": 3, "body": "@pr-agent hi"}}
    assert send(client, "issue_comment", issue, "d11").json()["skipped"] == "not-a-pull-request"  # nosec B101
    bot = {**on_pr, "sender": {"type": "Bot"}}
    assert send(client, "issue_comment", bot, "d12").json()["skipped"] == "bot"  # nosec B101
    assert not events.is_dirty(REPO, 7)  # comments alone do not change readiness  # nosec B101


def test_redeliveries_are_recorded_once(client):
    review = {**pr_payload("submitted"), "review": {"id": 1, "state": "commented"}}
    send(client, "pull_request_review", review, "same")
    assert send(client, "pull_request_review", review, "same").json()["duplicate"] is True  # nosec B101
    assert len(events.pr_activity(REPO, 7)) == 1  # nosec B101


def test_installation_events_maintain_the_installation_cache(client):
    created = {"action": "created", "installation": {"id": 42},
               "repositories": [{"full_name": "o/a"}, {"full_name": "o/b"}]}
    send(client, "installation", created, "i1")
    assert store.rget("ghinst:o/a") == "42" and store.rget("ghinst:o/b") == "42"  # nosec B101
    change = {"action": "removed", "installation": {"id": 42},
              "repositories_added": [{"full_name": "o/c"}], "repositories_removed": [{"full_name": "o/a"}]}
    send(client, "installation_repositories", change, "i2")
    assert store.rget("ghinst:o/a") is None and store.rget("ghinst:o/c") == "42"  # nosec B101
    # Any App delivery refreshes the cache for its repo.
    review = {**pr_payload("submitted"), "installation": {"id": 77}, "review": {"state": "commented"}}
    send(client, "pull_request_review", review, "i3")
    assert store.rget("ghinst:o/r") == "77"  # nosec B101


def test_default_branch_push_touching_config_drops_the_cache(client):
    store.rset_json(dm.repo_config_key(REPO), {"cached": True})
    push = {"ref": "refs/heads/main", "repository": {"full_name": REPO, "default_branch": "main"},
            "commits": [{"modified": ["src/a.py"]}]}
    assert send(client, "push", push, "p1").json()["config_invalidated"] is False  # nosec B101
    push["commits"].append({"added": [".pragent.yaml"]})
    assert send(client, "push", push, "p2").json()["config_invalidated"] is True  # nosec B101
    assert store.rget(dm.repo_config_key(REPO)) is None  # nosec B101
    other = {**push, "ref": "refs/heads/feat"}
    assert send(client, "push", other, "p3").json()["skipped"] == "not-default-branch"  # nosec B101


def test_a_failing_handler_reports_instead_of_500(client, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(events, "record_pr_activity", boom)
    review = {**pr_payload("submitted"), "review": {"state": "commented"}}
    resp = send(client, "pull_request_review", review, "e1")
    assert resp.status_code == 202 and resp.json()["errors"] == ["RuntimeError"]  # nosec B101


def test_activity_endpoint(client):
    review = {**pr_payload("submitted"), "review": {"id": 1, "state": "approved"}}
    send(client, "pull_request_review", review, "a1")
    body = client.get(f"/repos/{REPO}/pulls/7/activity").json()
    assert body["readiness_stale"] is True and body["activity"][0]["kind"] == "review"  # nosec B101
    assert client.get("/repos/o/r/pulls/0/activity").status_code == 400  # nosec B101


def test_activity_log_is_capped():
    for i in range(dm.ACTIVITY_CAP + 5):
        events.record_pr_activity(REPO, 1, {"kind": "x", "i": i})
    rows = events.pr_activity(REPO, 1, limit=500)
    assert len(rows) == dm.ACTIVITY_CAP and rows[0]["i"] == dm.ACTIVITY_CAP + 4  # nosec B101


def test_webhooks_signed_with_either_the_repo_or_the_app_secret_are_accepted(monkeypatch):
    import hashlib
    import hmac
    import json as _json

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "repo-secret")
    monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "app-secret")
    client = TestClient(main.app)
    raw = _json.dumps({"zen": "x"}).encode()

    def post(secret):
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return client.post("/webhooks/github", content=raw,
                           headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sig,
                                    "Content-Type": "application/json"})

    assert post("repo-secret").status_code == 202  # nosec B101
    assert post("app-secret").status_code == 202  # nosec B101
    assert post("other").status_code == 401  # nosec B101
