"""Idempotency, desktop hand-off, cloud fallback and escalation, end to end
through the real FastAPI app (in-memory store, sandbox dispatch stubbed)."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import decision_flow
import main
import store
from sandboxes import decider

SHA = "a" * 40


def F(path, a=0, dl=0, flags=()):
    return {"path": path, "status": "modified", "additions": a, "deletions": dl, "flags": list(flags)}


DOCS = {"title": "t", "files": [F("README.md", 3)], "truncated": False}
TINY = {"title": "t", "files": [F("src/main.js", 2, 1)], "truncated": False}
RUNTIME = {"title": "t", "files": [F("backend/api.py", 80, 20), F("backend/tests/test_api.py", 60)], "truncated": False}
SAFETY = {"title": "t", "files": [F(".github/workflows/ci.yml", 5)], "truncated": False}


@pytest.fixture()
def env(monkeypatch):
    store._MEM.clear()
    monkeypatch.delenv("BACKEND_API_TOKEN", raising=False)
    monkeypatch.delenv("DECIDER_ENABLED", raising=False)
    state = {"facts": TINY, "dispatched": []}

    async def fake_facts(repo, number, title):
        return state["facts"]

    monkeypatch.setattr(decision_flow, "fetch_facts", fake_facts)
    monkeypatch.setattr(main, "enqueue_dispatch", lambda agent, pr, run_id: state["dispatched"].append((agent, pr, run_id)))
    store.rset_json("agent:a1", {"id": "a1", "name": "a1", "trigger": "on_commit", "runtime_mode": "interactive", "target_repos": []})
    state["client"] = TestClient(main.app)
    return state


def hook(client, action="opened", sha=SHA, number=7, label=None):
    payload = {
        "action": action,
        "pull_request": {"number": number, "title": "t", "head": {"sha": sha, "ref": "feat"}},
        "repository": {"full_name": "o/r"},
        "sender": {"type": "User"},
    }
    if label:
        payload["label"] = {"name": label}
    return client.post("/webhooks/github", json=payload)


def heartbeat(client):
    resp = client.post("/daemon/heartbeat", json={"version": "t", "interval_secs": 30, "model": "rules"})
    assert resp.status_code == 200


def test_docs_only_is_skipped_and_redelivery_is_deduped(env):
    env["facts"] = DOCS
    first = hook(env["client"]).json()
    assert first["skipped"] == "DOCS_ONLY" and env["dispatched"] == []
    again = hook(env["client"]).json()
    assert again["deduped"] is True and again["id"] == first["id"]


def test_fast_review_downgrades_an_interactive_agent_and_dedupes_per_sha(env):
    body = hook(env["client"]).json()
    assert len(body["runs"]) == 1
    agent, _pr, _run_id = env["dispatched"][0]
    assert agent["runtime_mode"] == "review_only"  # a decision can only reduce spend
    run = store.rget_json(f"run:{body['runs'][0]}")
    assert run["eval_id"] == body["id"] and run["decision"]["decision"] == "FAST_REVIEW"
    assert hook(env["client"]).json()["deduped"] is True
    assert len(env["dispatched"]) == 1
    # a new push is a new head_sha -> evaluated again
    assert "runs" in hook(env["client"], sha="b" * 40, action="synchronize").json()
    assert len(env["dispatched"]) == 2


def test_labeled_events_bypass_the_gate(env):
    store.rset_json("agent:lab", {"id": "lab", "name": "lab", "trigger": "on_label", "trigger_label": "review-me",
                                  "runtime_mode": "review_only", "target_repos": []})
    hook(env["client"])  # evaluated + dispatched for a1
    resp = hook(env["client"], action="labeled", label="review-me").json()
    assert resp.get("runs") and "deduped" not in resp


def test_sandbox_decisions_wait_for_a_human_under_assist(env):
    env["facts"] = RUNTIME
    body = hook(env["client"]).json()
    assert body["escalated"] is True and env["dispatched"] == []
    inbox = env["client"].get("/decisions", params={"state": "escalated"}).json()
    assert [r["id"] for r in inbox] == [body["id"]] and "facts" not in inbox[0]
    resolved = env["client"].post(f"/decisions/{body['id']}/resolve", json={"action": "fast_review"}).json()
    assert len(resolved["runs"]) == 1 and env["dispatched"][0][0]["runtime_mode"] == "review_only"
    assert env["client"].post(f"/decisions/{body['id']}/resolve", json={"action": "skip"}).status_code == 409
    assert env["client"].get("/decisions/stats").json()["feedback"]["SANDBOX_TEST"]["override"] == 1


def test_resolving_with_sandbox_keeps_the_agents_own_mode(env):
    env["facts"] = RUNTIME
    body = hook(env["client"]).json()
    env["client"].post(f"/decisions/{body['id']}/resolve", json={"action": "sandbox"})
    assert env["dispatched"][0][0]["runtime_mode"] == "interactive"
    assert env["client"].get("/decisions/stats").json()["feedback"]["SANDBOX_TEST"]["agree"] == 1


def test_auto_level_runs_confident_sandbox_decisions_with_the_right_provider(env):
    env["client"].put("/autonomy", json={"level": "auto", "conf_auto": 0.75, "conf_floor": 0.55})
    env["facts"] = RUNTIME
    body = hook(env["client"]).json()
    assert body["decision"]["provider"] == "box" and len(body["runs"]) == 1
    assert env["dispatched"][0][0]["runtime_mode"] == "interactive"


def test_safety_flagged_always_escalates_even_in_auto(env):
    env["client"].put("/autonomy", json={"level": "auto", "conf_auto": 0.75, "conf_floor": 0.55})
    env["facts"] = SAFETY
    body = hook(env["client"]).json()
    assert body["escalated"] and body["decision"]["reason_code"] == "SAFETY_FLAGGED"
    assert env["dispatched"] == []


def test_human_choices_on_escalations_are_recorded_as_labels_not_overrides(env):
    env["facts"] = SAFETY
    body = hook(env["client"]).json()
    assert env["client"].post(f"/decisions/{body['id']}/resolve", json={"action": "skip"}).json()["dismissed"]
    fb = env["client"].get("/decisions/stats").json()["feedback"]["ESCALATE_HUMAN"]
    assert fb == {"chose_fast_review": 0, "chose_sandbox": 0, "chose_skip": 1}


def test_observe_mode_records_but_keeps_legacy_behaviour(env):
    env["client"].put("/autonomy", json={"level": "observe", "conf_auto": 0.75, "conf_floor": 0.55})
    env["facts"] = DOCS
    body = hook(env["client"]).json()
    assert len(body["runs"]) == 1 and env["dispatched"][0][0]["runtime_mode"] == "interactive"
    assert store.eval_get(body["id"])["decision"]["reason_code"] == "DOCS_ONLY"


def test_desktop_online_defers_then_accepts_the_desktops_decision_once(env):
    heartbeat(env["client"])
    first = hook(env["client"]).json()
    assert first["deferred_to_desktop"] is True and env["dispatched"] == []
    items = env["client"].get("/decisions/pending").json()["items"]
    assert [i["id"] for i in items] == [first["id"]] and items[0]["facts"]["files"]
    decision = decider.decide(items[0]["facts"])
    done = env["client"].post(f"/decisions/{first['id']}/submit", json={"decision": decision}).json()
    assert len(done["runs"]) == 1
    assert env["client"].post(f"/decisions/{first['id']}/submit", json={"decision": decision}).status_code == 409
    assert hook(env["client"]).json()["deduped"] is True
    assert env["client"].post("/decisions/sweep").json()["decided"] == 0
    assert len(env["dispatched"]) == 1


def test_a_stopping_desktop_stops_receiving_events_immediately(env):
    heartbeat(env["client"])
    assert env["client"].get("/daemon/status").json()["desktop_online"] is True
    assert env["client"].post("/daemon/offline").json()["ok"] is True
    status = env["client"].get("/daemon/status").json()
    assert status["desktop_online"] is False and status["fallback_active"] is True
    assert "runs" in hook(env["client"]).json()  # cloud decides right away, nothing deferred


def test_expired_deferral_falls_back_to_the_cloud_decider(env):
    heartbeat(env["client"])
    first = hook(env["client"]).json()
    assert env["client"].post("/decisions/sweep").json()["decided"] == 0  # still within grace
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.eval_update(first["id"], {"defer_until": past})
    assert env["client"].post("/decisions/sweep").json()["decided"] == 1
    rec = store.eval_get(first["id"])
    assert rec["state"] == "dispatched" and rec["decided_by"] == "cloud-fallback"
    assert len(env["dispatched"]) == 1
    # a late desktop submit loses
    late = env["client"].post(f"/decisions/{first['id']}/submit", json={"decision": rec["decision"]})
    assert late.status_code == 409


def test_submit_rejects_malformed_decisions(env):
    heartbeat(env["client"])
    first = hook(env["client"]).json()
    bad = {"decision": "FAST_REVIEW", "confidence": 2, "provider": "review",
           "reason_code": "STATIC_DIFF_OK", "policy_version": "v1", "latency_ms": 1}
    assert env["client"].post(f"/decisions/{first['id']}/submit", json={"decision": bad}).status_code == 422
    assert env["client"].post("/decisions/nothex/submit", json={"decision": bad}).status_code == 404


def test_missing_pr_files_fails_open_to_the_legacy_path(env, monkeypatch):
    async def none(*_args):
        return None

    monkeypatch.setattr(decision_flow, "fetch_facts", none)
    body = hook(env["client"]).json()
    assert len(body["runs"]) == 1 and env["dispatched"][0][0]["runtime_mode"] == "interactive"


def test_external_claims_dedupe_against_the_webhook_path(env):
    claim = {"repo": "o/r", "pr_number": 7, "head_sha": SHA, "owner": "box-cron"}
    got = env["client"].post("/decisions/claim", json=claim).json()
    assert got["claimed"] is True
    assert env["client"].post("/decisions/claim", json=claim).json()["claimed"] is False
    assert hook(env["client"]).json()["deduped"] is True  # webhook sees the box's claim
    wrong = env["client"].post("/decisions/claim", json={**claim, "action": "release", "claim_id": "wrong"})
    assert wrong.json()["ok"] is False
    right = env["client"].post("/decisions/claim", json={**claim, "action": "release", "claim_id": got["claim_id"]})
    assert right.json()["ok"] is True
    assert "runs" in hook(env["client"]).json()  # released -> normal evaluation proceeds
    assert env["client"].post("/decisions/claim", json={**claim, "repo": "../x"}).status_code == 400


def test_register_is_atomic_under_concurrency():
    store._MEM.clear()
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(store.eval_register("o/r", 1, SHA, {"state": "pending"})[0])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_stuck_evaluations_are_failed_so_a_human_can_retry(env):
    pr = {"repo": "o/r", "pr_number": 9, "sha": SHA, "action": "opened", "label": None, "title": "", "head_ref": ""}
    _created, rec = store.eval_register("o/r", 9, SHA, {"state": "pending", "agent_ids": ["a1"], "pr": pr})
    raw = store.eval_get(rec["id"])
    raw["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    store.rset_json(f"eval:{rec['id']}", raw)
    assert env["client"].post("/decisions/sweep").json()["failed"] == 1
    assert store.eval_get(rec["id"])["state"] == "failed"
    retry = env["client"].post(f"/decisions/{rec['id']}/resolve", json={"action": "fast_review"}).json()
    assert len(retry["runs"]) == 1


def test_disabled_layer_is_a_noop(env, monkeypatch):
    monkeypatch.setenv("DECIDER_ENABLED", "0")
    env["facts"] = DOCS
    body = hook(env["client"]).json()
    assert len(body["runs"]) == 1 and "skipped" not in body


def test_autonomy_validation(env):
    assert env["client"].put("/autonomy", json={"level": "yolo"}).status_code == 422
    assert env["client"].put("/autonomy", json={"level": "auto", "conf_auto": 0.6, "conf_floor": 0.9}).status_code == 422
    assert env["client"].get("/autonomy").json()["level"] == "assist"


def test_desktop_cannot_skip_or_downgrade_what_the_guardrails_protect(env):
    heartbeat(env["client"])
    env["facts"] = SAFETY
    first = hook(env["client"]).json()
    skip = {"decision": "SKIP", "confidence": 0.99, "provider": "none",
            "reason_code": "DOCS_ONLY", "policy_version": "v1", "latency_ms": 1}
    out = env["client"].post(f"/decisions/{first['id']}/submit", json={"decision": skip}).json()
    assert out["escalated"] is True and out["decision"]["reason_code"] == "SAFETY_FLAGGED"
    rec = store.eval_get(first["id"])
    assert rec["decided_by"] == "desktop-overridden" and env["dispatched"] == []


def test_a_desktop_skip_is_refused_when_the_cloud_policy_would_not_skip(env):
    heartbeat(env["client"])
    first = hook(env["client"]).json()  # TINY source change
    skip = {"decision": "SKIP", "confidence": 0.99, "provider": "none",
            "reason_code": "DOCS_ONLY", "policy_version": "v1", "latency_ms": 1}
    out = env["client"].post(f"/decisions/{first['id']}/submit", json={"decision": skip}).json()
    assert out["decision"]["decision"] == "FAST_REVIEW" and len(out["runs"]) == 1


def test_a_full_run_queue_marks_the_evaluation_failed_not_dispatched(env, monkeypatch):
    monkeypatch.setattr(main, "enqueue_dispatch", lambda agent, pr, run_id: main._fail_run(run_id, "run queue is full"))
    resp = env["client"].post("/webhooks/github", json={
        "action": "opened",
        "pull_request": {"number": 7, "title": "t", "head": {"sha": SHA, "ref": "f"}},
        "repository": {"full_name": "o/r"}, "sender": {"type": "User"},
    })
    assert resp.json()["failed"] == "dispatch"
    rec = [r for r in store.eval_list() if r["pr_number"] == 7][0]
    assert rec["state"] == "failed"


def test_bypass_releases_the_registration_so_a_redelivery_is_evaluated(env, monkeypatch):
    async def none(*_args):
        return None

    monkeypatch.setattr(decision_flow, "fetch_facts", none)
    hook(env["client"])
    assert store.eval_list() == []
    monkeypatch.setattr(decision_flow, "fetch_facts", lambda *a: _facts(env))
    assert "runs" in hook(env["client"]).json()


async def _facts(env):
    return env["facts"]


def test_a_stale_failure_never_overwrites_a_completed_dispatch(env):
    _created, rec = store.eval_register("o/r", 5, SHA, {"state": "pending"})
    store.eval_update(rec["id"], {"state": "dispatched"})
    decision_flow._mark_failed(rec["id"], "late", ("pending", "dispatching"))
    assert store.eval_get(rec["id"])["state"] == "dispatched"


def test_lock_release_is_compare_and_delete(env):
    with store.eval_lock("abc") as token:
        assert token
        store.rset("evallock:abc", "someone-else")  # lease expired and was re-acquired
    assert store.rget("evallock:abc") == "someone-else"
    assert store.rdelete_if_equal("evallock:abc", "someone-else") is True
    assert store.rget("evallock:abc") is None


def test_eval_ids_are_case_insensitive_for_repo_and_sha():
    assert store.eval_id("KB01111/Helios-Pr-Agent", 3, "ABCDEF1") == store.eval_id("kb01111/helios-pr-agent", 3, "abcdef1")


def test_open_index_finds_old_unresolved_items_beyond_any_recency_window(env):
    env["facts"] = RUNTIME
    old = hook(env["client"], number=1).json()  # escalated
    for n in range(2, 12):  # many newer, already-settled evaluations
        store.eval_register("o/r", 100 + n, SHA, {"state": "dispatched"})
    escalated = env["client"].get("/decisions", params={"state": "escalated", "limit": 5}).json()
    assert [r["id"] for r in escalated] == [old["id"]]
    env["client"].post(f"/decisions/{old['id']}/resolve", json={"action": "skip"})
    assert env["client"].get("/decisions", params={"state": "escalated"}).json() == []
    assert store.eval_list_open() == []  # terminal states leave the open index
