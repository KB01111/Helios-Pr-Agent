"""Make `import store`, `import main`, `import decision_flow` work from any cwd."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _in_memory_store(monkeypatch):
    """Tests must never touch a real Redis/Upstash, whatever the environment configures."""
    import store

    monkeypatch.setattr(store, "_REDIS", None)
    monkeypatch.setattr(store, "_SDK", None)
    monkeypatch.setattr(store, "_REST_URL", "")
    monkeypatch.setattr(store, "_REST_TOKEN", "")
    monkeypatch.setattr(store, "_REDIS_CONFIGURED", False)
    store._MEM.clear()
