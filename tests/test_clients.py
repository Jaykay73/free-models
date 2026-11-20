from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
import urllib.error
import pytest
import nimstatus_client as client


def test_directory_fetch_deadline_covers_blocked_body(monkeypatch):
    release_body = Event()
    finished = Event()

    class SlowBody(Response):
        def read(self, n):
            release_body.wait(2)
            return super().read(n)

        def __exit__(self, *args):
            finished.set()

    monkeypatch.setattr(client, "_FETCH_TIMEOUT", 0.05)
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *a, **kw: SlowBody({}))
    try:
        assert client._fetch_status("https://example.test/status.json") is None
        assert not finished.is_set()
    finally:
        release_body.set()
        finished.wait(2)

class Response:
    def __init__(self, data):
        self.data = json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, n):
        return self.data[:n]

def test_python_client(monkeypatch, cfg):
    client._cache.clear()
    mids = list(cfg["models"])
    clock = [0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    data = dict(schema_version=1, valid_until=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                aliases={"fast": dict(primary=mids[0], fallbacks=mids[1:], stale=False)})
    calls = []
    def fetch(url, timeout):
        calls.append((url, timeout))
        return Response(data)
    monkeypatch.setattr(client.urllib.request, "urlopen", fetch)
    opts = dict(status_url="https://example.test/status.json", defaults={"fast": mids})
    assert client.resolve("fast", opts) == mids
    clock[0] = 299
    assert client.resolve("fast", opts) == mids and len(calls) == 1
    clock[0] = 300
    client.resolve("fast", opts)
    assert len(calls) == 2 and calls[0][1] == 3
    def fail(*args, **kwargs):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(client.urllib.request, "urlopen", fail)
    clock[0] = 600
    with pytest.warns(RuntimeWarning):
        assert client.resolve("fast", opts) == mids
    client._cache.clear()
    with pytest.warns(RuntimeWarning):
        assert client.resolve("fast", opts) == mids
    monkeypatch.setattr(client.urllib.request, "urlopen", fetch)
    client._cache.clear()
    data["valid_until"] = "2000-01-01T00:00:00Z"
    with pytest.warns(RuntimeWarning):
        assert client.resolve("fast", opts) == mids
    data["valid_until"] = "2099-01-01T00:00:00Z"
    client._cache.clear()
    attempts = []
    def invoke(model):
        attempts.append(model)
        if len(attempts) == 1:
            raise TimeoutError()
        return model
    assert client.call_with_fallback("fast", invoke, opts, retryable=lambda e: isinstance(e, TimeoutError)) == mids[1]
    assert attempts == mids[:2]
    attempts.clear()
    with pytest.raises(TimeoutError):
        client.call_with_fallback("fast", invoke, opts, retryable=lambda e: False)
    assert len(attempts) == 1
    attempts.clear()
    with pytest.raises(TimeoutError):
        client.call_with_fallback("fast", invoke, opts, retryable=lambda e: True, max_attempts=1)
    assert len(attempts) == 1

def test_js_client():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node 18+ required to test the JavaScript helper")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run([node, "--test", "tests/client.test.mjs"], cwd=root, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_router_tracks_remote_model_changes_without_app_ids(monkeypatch, cfg):
    client._cache.clear()
    mids = list(cfg["models"])
    clock, primary, fetches = [0], [0], []
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    def fetch(url, timeout):
        fetches.append(url)
        return Response(dict(schema_version=1, valid_until="2099-01-01T00:00:00Z",
            aliases={"fast": dict(primary=mids[primary[0]], fallbacks=[], stale=False)}))
    monkeypatch.setattr(client.urllib.request, "urlopen", fetch)
    router = client.ModelRouter()
    assert not fetches  # Construction never blocks on the network.
    assert router.call("fast", lambda model: model) == mids[0]
    primary[0] = 1
    clock[0] = 299
    assert router.primary() == mids[0]
    clock[0] = 300
    assert router.call("fast", lambda model: model) == mids[1]
    assert fetches == [client.DEFAULT_STATUS_URL] * 2


def test_router_automatic_fallback_and_auth_abort(monkeypatch, cfg):
    mids = list(cfg["models"])
    monkeypatch.setattr(client, "resolve", lambda *args, **kwargs: mids)
    class HTTPError(Exception):
        def __init__(self, status):
            self.status_code = status
    attempted = []
    def infer(model):
        attempted.append(model)
        if model == mids[0]:
            raise HTTPError(429)
        return "answer"
    router = client.ModelRouter(max_attempts=2)
    assert router.call("fast", infer) == "answer"
    assert attempted == mids[:2]
    attempted.clear()
    def auth(model):
        attempted.append(model)
        raise HTTPError(401)
    with pytest.raises(HTTPError):
        router.call("fast", auth)
    assert attempted == mids[:1]
    attempted.clear()
    def unavailable(model):
        attempted.append(model)
        raise HTTPError(503)
    with pytest.raises(HTTPError):
        router.call("fast", unavailable)
    assert attempted == mids[:2]


def test_retry_classifier_recognizes_optional_sdk(monkeypatch):
    class APIConnectionError(Exception):
        pass
    class APITimeoutError(APIConnectionError):
        pass
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(APIConnectionError=APIConnectionError))
    assert client.is_retryable(APITimeoutError())
    assert client.is_retryable(TimeoutError())
    assert not client.is_retryable(ValueError())
    for status in (400, 401, 403, 404, 600):
        assert not client.is_retryable(SimpleNamespace(status_code=status))
    for status in (429, 500, 503, 599):
        assert client.is_retryable(SimpleNamespace(status_code=status))


def test_router_first_fetch_failure_without_model_defaults(monkeypatch):
    client._cache.clear()
    def fail(*args, **kwargs):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(client.urllib.request, "urlopen", fail)
    router = client.ModelRouter()
    with pytest.warns(RuntimeWarning), pytest.raises(client.NoModelsAvailable):
        router.call("fast", lambda model: pytest.fail("Inference must not start"))


@pytest.mark.parametrize("options", [{"max_attempts": 0}, {"ttl_s": 1}, {"defaults": {"fast": "not-a-list"}}, {"retryable": None}])
def test_router_rejects_invalid_options(options):
    with pytest.raises(ValueError):
        client.ModelRouter(**options)
