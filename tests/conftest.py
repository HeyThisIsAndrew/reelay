import os
import sys
from pathlib import Path

import pytest

# Headless Qt for GUI tests; must be set before any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test's database, cache and logs inside a temp folder."""
    home = tmp_path / "reelay_home"
    monkeypatch.setenv("REELAY_HOME", str(home))
    return home


class FakeResponse:
    def __init__(self, status=200, json_data=None, headers=None, text=""):
        self.status_code = status
        self._json = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Scripted stand-in for requests.Session.

    Register handlers with .on(method, url_contains, response_or_callable).
    Each call uses the first matching handler; handlers are consumed unless
    registered with repeat=True. Every call is recorded in .calls.
    """

    def __init__(self):
        self.handlers = []
        self.calls = []

    def on(self, method, url_contains, response, repeat=False):
        self.handlers.append([method.upper(), url_contains, response, repeat])
        return self

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method.upper(), "url": url, **kwargs})
        for i, (m, frag, response, repeat) in enumerate(self.handlers):
            if m == method.upper() and frag in url:
                if not repeat:
                    self.handlers.pop(i)
                return response(method, url, kwargs) if callable(response) else response
        raise AssertionError(f"unexpected request: {method} {url}")

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


@pytest.fixture
def fake_session():
    return FakeSession()


def pytest_addoption(parser):
    parser.addoption("--run-network", action="store_true", help="run tests that hit real TikTok/YouTube")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-network"):
        return
    skip = pytest.mark.skip(reason="needs --run-network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
