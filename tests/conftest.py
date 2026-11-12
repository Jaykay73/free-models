import json
from pathlib import Path
import pytest
import httpx
from nimstatus.config import load

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def denied(*args, **kwargs):
        raise AssertionError("Real network forbidden")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)

@pytest.fixture
def cfg():
    fixture_models = ROOT / "tests/fixtures/healthy/models.yml"
    return load(fixture_models if fixture_models.exists() else ROOT / "models.yml")

@pytest.fixture
def recorded():
    return json.loads((ROOT / "tests/fixtures/healthy/responses.json").read_text())
