"""prepare_diff keeps every reviewable file in budget; one huge or generated
file must not starve the rest."""
from sandboxes import providers_review as pr


def _file(path, body_lines):
    body = "".join(f"+line {i}\n" for i in range(body_lines))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


def test_small_diff_is_untouched():
    diff = _file("a.py", 3)
    assert pr.prepare_diff(diff, 10_000) == (diff, [])  # nosec B101


def test_lockfiles_are_dropped_and_reported():
    diff = _file("package-lock.json", 4000) + _file("src/app.py", 20)
    text, notes = pr.prepare_diff(diff, 3000)
    assert "src/app.py" in text and "package-lock.json" not in text  # nosec B101
    assert any("package-lock.json" in n for n in notes)  # nosec B101


def test_huge_file_does_not_starve_small_ones():
    diff = _file("big.py", 5000) + _file("small_a.py", 10) + _file("small_b.py", 10)
    text, notes = pr.prepare_diff(diff, 3000)
    assert _file("small_a.py", 10) in text and _file("small_b.py", 10) in text  # kept whole  # nosec B101
    assert "[file truncated]" in text  # nosec B101
    assert len(text) <= 3000  # nosec B101
    assert any(n.startswith("truncated big.py") for n in notes)  # nosec B101


def test_original_file_order_is_preserved():
    diff = _file("z_big.py", 3000) + _file("a_small.py", 5)
    text, _ = pr.prepare_diff(diff, 2000)
    assert text.index("z_big.py") < text.index("a_small.py")  # nosec B101


def test_budget_env_override(monkeypatch):
    monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "5000")
    assert pr.diff_budget() == 5000  # nosec B101
    monkeypatch.setenv("REVIEW_DIFF_MAX_CHARS", "nonsense")
    assert pr.diff_budget() == pr.DEFAULT_DIFF_BUDGET  # nosec B101


def test_noisy_files_are_dropped_even_from_small_diffs():
    diff = _file("package-lock.json", 5) + _file("src/app.py", 5)
    text, notes = pr.prepare_diff(diff, 100_000)
    assert "package-lock.json" not in text and "src/app.py" in text  # nosec B101
    assert any("package-lock.json" in n for n in notes)  # nosec B101


def test_lockfile_only_diff_is_still_reviewed():
    diff = _file("package-lock.json", 50)
    text, notes = pr.prepare_diff(diff, 100_000)
    assert "package-lock.json" in text and notes  # nosec B101


def test_many_truncated_files_stay_within_budget():
    diff = "".join(_file(f"f{i}.py", 400) for i in range(30))
    text, _ = pr.prepare_diff(diff, 5000)
    assert len(text) <= 5000  # nosec B101


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class _PagedClient:
    """Serves 250 comments in pages of 100; the marker is on the last page."""

    def __init__(self, marker_at):
        self.marker_at, self.pages = marker_at, []

    async def get(self, url, headers=None, params=None):
        page = params["page"]
        self.pages.append(page)
        start = (page - 1) * 100
        rows = [{"id": i, "body": pr.review_marker("agent-a") if i == self.marker_at else "x"} for i in range(start, min(start + 100, 250))]
        return _Resp(200, rows)


def test_previous_review_is_found_past_the_first_page():
    import asyncio

    client = _PagedClient(marker_at=230)
    found = asyncio.run(pr._find_previous_review(client, "u", {}, pr.review_marker("agent-a")))
    assert found == 230  # nosec B101
    assert client.pages == [1, 2, 3]  # nosec B101


def test_head_moved_detects_a_newer_push(monkeypatch):
    import asyncio

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp(200, {"head": {"sha": "newsha"}})

    monkeypatch.setattr(pr.httpx, "AsyncClient", _Client)
    assert asyncio.run(pr._head_moved("o/r", 1, "oldsha", "t")) is True  # nosec B101
    assert asyncio.run(pr._head_moved("o/r", 1, "newsha", "t")) is False  # nosec B101
    assert asyncio.run(pr._head_moved("o/r", 1, "", "t")) is False  # nosec B101


def test_each_agent_finds_only_its_own_review():
    import asyncio

    client = _PagedClient(marker_at=10)  # carries agent-a's marker
    other = asyncio.run(pr._find_previous_review(client, "u", {}, pr.review_marker("agent-b")))
    assert other is None  # nosec B101
    assert pr.review_marker("agent-a") != pr.review_marker("agent-b")  # nosec B101
    assert "-->" not in pr.review_marker("a>b -->x")[:-4]  # nosec B101  # input cannot close the comment early
