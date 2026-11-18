"""prepare_diff keeps every reviewable file in budget; one huge or generated
file must not starve the rest."""
from sandboxes import providers_review as pr


def _file(path, body_lines):
    body = "".join(f"+line {i}\n" for i in range(body_lines))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


def test_small_diff_is_untouched():
    diff = _file("a.py", 3)
    assert pr.prepare_diff(diff, 10_000) == (diff, [])


def test_lockfiles_are_dropped_and_reported():
    diff = _file("package-lock.json", 4000) + _file("src/app.py", 20)
    text, notes = pr.prepare_diff(diff, 3000)
    assert "src/app.py" in text and "package-lock.json" not in text
    assert any("package-lock.json" in n for n in notes)


def test_huge_file_does_not_starve_small_ones():
    diff = _file("big.py", 5000) + _file("small_a.py", 10) + _file("small_b.py", 10)
    text, notes = pr.prepare_diff(diff, 3000)
    assert _file("small_a.py", 10) in text and _file("small_b.py", 10) in text  # kept whole
    assert "[file truncated]" in text
    assert len(text) <= 3000 + 100
    assert any(n.startswith("truncated big.py") for n in notes)


def test_original_file_order_is_preserved():
    diff = _file("z_big.py", 3000) + _file("a_small.py", 5)
    text, _ = pr.prepare_diff(diff, 2000)
    assert text.index("z_big.py") < text.index("a_small.py")


def test_budget_env_override(monkeypatch):
    monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "5000")
    assert pr.diff_budget() == 5000
    monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "nonsense")
    assert pr.diff_budget() == pr.DEFAULT_DIFF_BUDGET
