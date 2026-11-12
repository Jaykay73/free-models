from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import urllib.error
import pytest
import nimstatus_client as client

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
