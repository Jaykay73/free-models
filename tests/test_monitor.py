import argparse
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import httpx
import pytest
import yaml
from nimstatus import config, discover, history, main, publish, scoring
from nimstatus.probe import Budget, Prober, clean

NOW = datetime(2026, 11, 12, 14, tzinfo=timezone.utc)

def rec(kind="ok", age=0, latency=3000, ttft=None):
    return dict(ts=history.stamp(NOW-timedelta(hours=age)), ok=kind=="ok", error_class=kind,
                http_status=200 if kind=="ok" else 500, total_ms=latency, ttft_ms=ttft, canary_ok=kind=="ok")

def initial(cfg):
    state = history.fresh()
    state["catalog"] = list(cfg["models"])
    for mid in cfg["models"]:
        m = history.model_state()
        m["records"] = [rec()]
        m["capabilities"] = {c: {"ok": True, "ts": history.stamp(NOW)} for c in ("tools", "json")}
        state["models"][mid] = m
    return state

def args(tmp_path, cfg, fixture):
    conf = tmp_path / "models.yml"
    conf.write_text(yaml.safe_dump(cfg))
    offline = tmp_path / "offline"
    offline.mkdir(exist_ok=True)
    (offline / "responses.json").write_text(json.dumps(fixture))
    return argparse.Namespace(config=str(conf), data_dir=str(tmp_path/"data"), offline=str(offline), dry_run=False)

def seed(args, cfg):
    state = initial(cfg)
    value = publish.build(cfg, state, None, NOW, [], [], "test")
    history.atomic_write(Path(args.data_dir)/"history.json", state)
    history.atomic_write(Path(args.data_dir)/"status.json", value)
    return {p.name: p.read_bytes() for p in Path(args.data_dir).iterdir()}

def test_scoring(cfg):
    m = history.model_state()
    m["records"] = [rec(), rec("throttled"), rec("timeout")]
    result = scoring.metrics(m, .5, NOW, cfg["settings"])
    assert result["availability"] == pytest.approx(1.4/3)
    assert result["score"] == pytest.approx((1.4/3)**2*1.5)
    m["records"] = [rec(latency=20000)]
    assert scoring.metrics(m, 0, NOW, cfg["settings"])["latency_factor"] == .2
    m["records"] = [rec(latency=20000, ttft=100)]
    assert scoring.metrics(m, 0, NOW, cfg["settings"])["latency_factor"] == 1
    m["records"] = [rec(), rec("timeout", age=6), rec("timeout", age=25)]
    assert scoring.metrics(m, 0, NOW, cfg["settings"])["availability"] == pytest.approx(2/3)

def test_circuit(cfg):
    m = history.model_state()
    for run in range(1, 4):
        scoring.record(m, rec("timeout"), run, cfg["settings"])
    assert m["circuit"] == "open" and m["next_run"] == 5
    assert not scoring.due(m, 4, NOW)
    scoring.record(m, rec("server_error"), 5, cfg["settings"])
    assert m["next_run"] == 8
    scoring.record(m, rec("network"), 8, cfg["settings"])
    assert m["next_run"] == 13
    scoring.record(m, rec(), 13, cfg["settings"])
    assert m["circuit"] == "open"
    scoring.record(m, rec(), m["next_run"], cfg["settings"])
    assert m["circuit"] == "closed"
    scoring.record(m, rec("not_found"), 30, cfg["settings"])
    assert m["circuit"] == "unavailable"
    assert not scoring.due(m, 999, NOW+timedelta(hours=23))
    assert scoring.due(m, 999, NOW+timedelta(days=1))

def test_hysteresis(cfg):
    mids = list(cfg["models"])
    state = initial(cfg)
    models = {mid: dict(score=.7, latency_factor=1, availability=1, listed=True,
                        status="closed", capability_verified={}) for mid in mids}
    alias = cfg["aliases"]["fast"] | {"prefer_speed": False}
    state["aliases"]["fast"] = dict(primary=mids[0], challenger=None, streak=0)
    models[mids[1]]["score"] = .8
    for _ in range(5):
        assert scoring.rank_alias("fast", alias, cfg, state, models)["primary"] == mids[0]
    models[mids[1]]["score"] = .9
    for i in range(3):
        result = scoring.rank_alias("fast", alias, cfg, state, models)
        assert result["primary"] == (mids[1] if i == 2 else mids[0])
    models[mids[1]]["status"] = "open"
    assert scoring.rank_alias("fast", alias, cfg, state, models)["primary"] != mids[1]

@pytest.mark.parametrize("kind", ["network", "timeout"])
def test_global_rollback(tmp_path, cfg, recorded, kind, caplog):
    for model in recorded["models"].values():
        model["liveness"] = {"error": kind}
    a = args(tmp_path, cfg, recorded)
    before = seed(a, cfg)
    with pytest.raises(main.RunError, match="outage"):
        asyncio.run(main.run(a, now=NOW))
    assert main.main(["--config", a.config, "--data-dir", a.data_dir, "--offline", a.offline]) == 1
    assert "outage" in caplog.text
    assert before == {p.name: p.read_bytes() for p in Path(a.data_dir).iterdir()}

def test_auth_rollback(tmp_path, cfg, recorded, caplog, capsys, monkeypatch):
    secret = "nvapi-DO-NOT-EXPOSE-TEST-SECRET"
    monkeypatch.setenv("NVIDIA_API_KEY", secret)
    for model in recorded["models"].values():
        model["liveness"] = {"status": 401, "body": {"error": secret}}
    a = args(tmp_path, cfg, recorded)
    before = seed(a, cfg)
    assert main.main(["--config", a.config, "--data-dir", a.data_dir, "--offline", a.offline]) == 1
    assert "NVIDIA_API_KEY missing, invalid, or expired" in caplog.text
    assert secret not in caplog.text + str(capsys.readouterr())
    assert before == {p.name: p.read_bytes() for p in Path(a.data_dir).iterdir()}

def test_discovery_fallback(tmp_path, cfg, recorded):
    recorded["discovery"] = {"status": 503}
    a = args(tmp_path, cfg, recorded)
    seed(a, cfg)
    assert asyncio.run(main.run(a, now=NOW)) == 0
    value = json.loads((Path(a.data_dir)/"status.json").read_text())
    assert "Discovery failed" in value["notes"][0]
    assert all(m["listed"] for m in value["models"].values())

def test_sanity(cfg):
    state = initial(cfg)
    previous = publish.build(cfg, state, None, NOW, [], [], "t")
    for model in state["models"].values():
        model["circuit"] = "open"
    with pytest.raises(ValueError, match="all aliases"):
        publish.build(cfg, state, None, NOW, [], [], "t")
    value = publish.build(cfg, state, previous, NOW, [], [], "t")
    assert value["health"] == "stale" and all(a["stale"] for a in value["aliases"].values())
    state = initial(cfg)
    for mid, spec in cfg["models"].items():
        if "tools" in spec["capabilities"]:
            state["models"][mid]["capabilities"]["tools"]["ok"] = False
    value = publish.build(cfg, state, previous, NOW, [], [], "t")
    assert value["aliases"]["tools"]["stale"]
    assert not value["aliases"]["fast"]["stale"]
    value = publish.build(cfg, state, None, NOW, [], [], "t")
    assert value["aliases"]["tools"]["primary"] is None

def test_unclassified(cfg, recorded):
    ids = [m["id"] for m in recorded["discovery"]["body"]["data"]]
    unknown = discover.unclassified(ids, cfg)
    assert len(unknown) == 1 and "embed" not in unknown[0]

def test_schema_and_publication(cfg):
    value = publish.build(cfg, initial(cfg), None, NOW, [], [], "t")
    publish.validate(value)
    copy_value = copy.deepcopy(value)
    copy_value["models"][next(iter(cfg["models"]))]["score"] = .123
    copy_value["generated_at"] = history.stamp(NOW+timedelta(minutes=10))
    assert not publish.should_publish(value, copy_value, NOW+timedelta(minutes=10))
    assert publish.should_publish(value, copy_value, NOW+timedelta(hours=6))
    for change in ("status", "capability_verified", "listed"):
        changed = copy.deepcopy(value)
        m = changed["models"][next(iter(cfg["models"]))]
        m[change] = {"status": "open", "capability_verified": {"json": False}, "listed": False}[change]
        assert publish.should_publish(value, changed, NOW)
    for bad in ({"schema_version": 2}, {"health": "unknown"}, {"valid_until": "yesterday"}, {"aliases": []}):
        with pytest.raises((ValueError, TypeError)):
            publish.validate(value | bad)

def test_budget_rotation(tmp_path, cfg, recorded, caplog):
    cfg["settings"]["max_models_per_run"] = 1
    a = args(tmp_path, cfg, recorded)
    seen = set()
    for index in range(6):
        assert asyncio.run(main.run(a, now=NOW+timedelta(minutes=10*index))) == 0
        state = history.load(Path(a.data_dir)/"history.json")
        seen.update(mid for mid, m in state["models"].items() if m.get("last_probed"))
    assert seen == set(cfg["models"])
    assert len(history.load(Path(a.data_dir)/"history.json")["models"]) == 3

def test_hard_budget_and_concurrency(cfg):
    mid = next(iter(cfg["models"]))
    async def scenario():
        count, active, peak = 0, 0, 0
        async def handler(request):
            nonlocal count, active, peak
            count += 1
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.001)
            active -= 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "PONG"}, "finish_reason": "stop"}]})
        async with httpx.AsyncClient(base_url="https://example.test/", transport=httpx.MockTransport(handler)) as client:
            budget = Budget(cfg["settings"]["max_models_per_run"]+10, jitter=False)
            prober = Prober(client, cfg["settings"], budget)
            results = await asyncio.gather(*(prober.liveness(mid, NOW) for _ in range(100)))
            assert count == budget.count == budget.limit
            assert peak <= 5
            assert sum(r is not None for r in results) == budget.limit
    asyncio.run(scenario())

def test_capability_cadence(tmp_path, cfg, recorded, caplog):
    caplog.set_level("INFO")
    a = args(tmp_path, cfg, recorded)
    asyncio.run(main.run(a, now=NOW))
    assert "Probe requests: 6 / 50" in caplog.text
    caplog.clear()
    asyncio.run(main.run(a, now=NOW+timedelta(minutes=10)))
    assert "Probe requests: 3 / 50" in caplog.text
    caplog.clear()
    asyncio.run(main.run(a, now=NOW+timedelta(hours=6)))
    assert "Probe requests: 6 / 50" in caplog.text

@pytest.mark.parametrize("status, expected", [(429, "throttled"), (503, "server_error"), (404, "not_found"), (410, "not_found"), (400, "client_error")])
def test_http_failure_no_retry(cfg, status, expected):
    async def scenario():
        def handler(request):
            return httpx.Response(status, json={"error": "failure"})
        async with httpx.AsyncClient(base_url="https://example.test/", transport=httpx.MockTransport(handler)) as client:
            budget = Budget(50, jitter=False)
            result = await Prober(client, cfg["settings"], budget).liveness(next(iter(cfg["models"])), NOW)
            assert result["error_class"] == expected and not result["ok"]
            assert budget.count == 1
    asyncio.run(scenario())

def test_guard_threshold():
    assert main.global_outage([{"error_class": "network"}]*4 + [{"error_class": "ok"}])
    assert not main.global_outage([{"error_class": "timeout"}]*3 + [{"error_class": "ok"}]*2)

def test_history_size(tmp_path, cfg):
    state = initial(cfg)
    # Forty model histories at ten-minute cadence over 48h.
    sample = next(iter(state["models"].values()))
    sample["records"] = [rec(age=i/6, latency=19999, ttft=2999) for i in reversed(range(288))]
    state["models"] = {str(index): copy.deepcopy(sample) for index in range(40)}
    path = tmp_path/"history.json"
    history.atomic_write(path, state)
    assert path.stat().st_size < 500_000
    assert len(history.load(path)["models"]["0"]["records"]) == 288

def test_workflow_constraints():
    # BaseLoader preserves YAML 'on' as a string (SafeLoader uses YAML 1.1).
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root/".github/workflows/probe.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "write"}
    steps = workflow["jobs"]["probe"]["steps"]
    assert sum("NVIDIA_API_KEY" in s.get("env", {}) for s in steps) == 1

def test_dry_run(tmp_path, cfg, recorded, capsys):
    a = args(tmp_path, cfg, recorded)
    a.dry_run = True
    assert asyncio.run(main.run(a, now=NOW)) == 0
    assert not Path(a.data_dir).exists()
    assert "Dry run:" in capsys.readouterr().out

def test_history_corrupt_prune(tmp_path, cfg):
    path = tmp_path/"history.json"
    path.write_text('{"bad": true}')
    assert history.load(path) == history.fresh()
    state = initial(cfg)
    m = next(iter(state["models"].values()))
    m["records"].append(rec(age=49))
    history.prune(state, NOW)
    assert len(m["records"]) == 1 and m["aggregate"]["count"] == 1
    history.atomic_write(path, state)
    assert history.load(path) == state

@pytest.mark.parametrize("mutation", [
    lambda c: c["settings"].update(max_models_per_run=0),
    lambda c: c["settings"].update(base_url="https://user:pass@example.com"),
    lambda c: next(iter(c["models"].values())).update(tier="huge"),
    lambda c: next(iter(c["models"].values())).update(preference=2),
    lambda c: c["aliases"]["fast"].update(require=["unknown"]),
])
def test_invalid_config(tmp_path, cfg, mutation):
    mutation(cfg)
    path = tmp_path/"bad.yml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):
        config.load(path)

def test_probe_protocol(cfg):
    mid = next(iter(cfg["models"]))
    async def scenario():
        calls = []
        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            if body["stream"]:
                return httpx.Response(400, json={"error": "stream unsupported"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "<think>PONG</think>Hello"}, "finish_reason": "stop"}]})
        async with httpx.AsyncClient(base_url="https://example.test/", transport=httpx.MockTransport(handler)) as client:
            budget = Budget(2, jitter=False)
            result = await Prober(client, cfg["settings"], budget).liveness(mid, NOW)
            assert result["ok"] and not result["canary_ok"] and result["ttft_ms"] is None
            assert len(calls) == budget.count == 2
            assert await Prober(client, cfg["settings"], budget).liveness(mid, NOW) is None
    asyncio.run(scenario())
    assert clean("<think>PONG") == ""

def test_tools_failure(tmp_path, cfg, recorded):
    for model in recorded["models"].values():
        if "tools" in model:
            model["tools"] = {"body": {"choices": [{"message": {"content": "no tool"}}]}}
    a = args(tmp_path, cfg, recorded)
    assert asyncio.run(main.run(a, now=NOW)) == 0
    value = publish.read_status(Path(a.data_dir)/"status.json")
    assert value["aliases"]["tools"]["primary"] is None

def test_root_models_config_valid():
    root = Path(__file__).resolve().parents[1]
    loaded = config.load(root / "models.yml")
    assert bool(loaded["models"])
    assert bool(loaded["aliases"])

