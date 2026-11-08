import sys
from pathlib import Path

import pytest
import requests

# Lets the tests do "import app" from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def no_real_requests(monkeypatch):
    """
    Tests use fake data and must never call the real FPL sites. If a test forgets to fake a feed, the call
    fails loudly here instead of quietly going online (slow, and different from day to day).
    """
    def refuse(url, *args, **kwargs):
        raise AssertionError(f"A test tried to call the real site: {url}")

    monkeypatch.setattr(requests, "get", refuse)
