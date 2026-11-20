import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
import yaml
from nimstatus import history, main, publish
from scripts.validate_publication import publish as validate_and_publish

REVISION = "a" * 40

@pytest.fixture
def artifact(tmp_path, cfg):
    incoming, destination = tmp_path / "incoming", tmp_path / "destination"
    (incoming / "data").mkdir(parents=True)
    destination.mkdir()
    (incoming / "base-revision.txt").write_text(REVISION + "\n")
    now = datetime.now(timezone.utc)
    state = history.fresh()
    state["catalog"] = list(cfg["models"])
    for mid in cfg["models"]:
        model = history.model_state()
        model["records"] = [dict(ts=history.stamp(now), ok=True, http_status=200, error_class="ok", ttft_ms=10, total_ms=20, canary_ok=True)]
        model["capabilities"] = {c: {"ok": True, "ts": history.stamp(now)} for c in ("tools", "json")}
        state["models"][mid] = model
    status = publish.build(cfg, state, None, now, [], [], "test")
    history.atomic_write(incoming / "data/history.json", state)
    history.atomic_write(incoming / "data/status.json", status)
    (destination / "status.json").write_text("previous-status")
    (destination / "history.json").write_text("previous-history")
    return incoming, destination

def test_valid_artifact_is_reserialized(artifact):
    incoming, destination = artifact
    validate_and_publish(incoming, destination, REVISION)
    publish.validate(json.loads((destination / "status.json").read_text()))
    assert history.load(destination / "history.json")["models"]

@pytest.mark.parametrize("attack", ["extra_file", "revision", "duplicate_key", "oversize", "secret", "invalid_schema", "nonfinite", "invalid_history"])
def test_bad_artifact_never_replaces_previous_files(artifact, attack):
    incoming, destination = artifact
    status = incoming / "data/status.json"
    if attack == "extra_file":
        (incoming / "run.py").write_text("raise RuntimeError('must never execute')")
    elif attack == "revision":
        (incoming / "base-revision.txt").write_text("b" * 40)
    elif attack == "duplicate_key":
        status.write_text('{"schema_version":1,"schema_version":1}')
    elif attack == "oversize":
        status.write_text(" " * 1_000_001)
    elif attack == "secret":
        value = json.loads(status.read_text())
        value["notes"] = ["nvapi-" + "X" * 40]
        status.write_text(json.dumps(value))
    elif attack == "invalid_schema":
        value = json.loads(status.read_text())
        value["run_shell"] = "unexpected"
        status.write_text(json.dumps(value))
    elif attack == "nonfinite":
        value = json.loads(status.read_text())
        next(iter(value["models"].values()))["score"] = float("nan")
        status.write_text(json.dumps(value))
    else:
        (incoming / "data/history.json").write_text('{"schema_version":1}')
    with pytest.raises((ValueError, TypeError, KeyError)):
        validate_and_publish(incoming, destination, REVISION)
    assert (destination / "status.json").read_text() == "previous-status"
    assert (destination / "history.json").read_text() == "previous-history"

def test_artifact_symlink_rejected(artifact, tmp_path):
    incoming, destination = artifact
    external = tmp_path / "external.json"
    external.write_text("{}")
    link = incoming / "data/status.json"
    link.unlink()
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("Host does not permit creating symlinks")
    with pytest.raises(ValueError):
        validate_and_publish(incoming, destination, REVISION)

def test_workflow_security_boundaries():
    root = Path(__file__).resolve().parents[1]
    for path in (root / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        for job in workflow["jobs"].values():
            write = job.get("permissions", workflow.get("permissions", {})).get("contents") == "write"
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"(?:actions/[a-z-]+|pypa/gh-action-pypi-publish)@[0-9a-f]{40}", step["uses"])
                if write:
                    assert "NVIDIA_API_KEY" not in step.get("env", {})
                    assert "pip install" not in step.get("run", "")
                elif step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] == "false"
    ci = yaml.load((root / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    assert "pull_request_target" not in ci["on"]
    assert ci["jobs"]["test"]["name"] == "Offline tests"

def test_origin_guard_before_secret_or_network(tmp_path, cfg, monkeypatch):
    cfg = copy.deepcopy(cfg)
    cfg["settings"]["base_url"] = "https://example.invalid/v1"
    conf = tmp_path / "models.yml"
    conf.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("NIMSTATUS_ALLOWED_ORIGIN", "https://integrate.api.nvidia.com")
    args = SimpleNamespace(config=conf, offline=None)
    with pytest.raises(main.RunError, match="origin"):
        asyncio.run(main.run(args))
