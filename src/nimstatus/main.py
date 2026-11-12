"""Run one transaction. Failed runs leave both durable files untouched."""
import argparse
import asyncio
import copy
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import httpx
from . import config, discover, history, publish, scoring
from .probe import Budget, Prober

class RunError(Exception):
    """Only constant, credential-free operational messages are allowed here."""

def offline_transport(directory):
    """Fixtures contain discovery + per-model liveness/tools/json HTTP responses."""
    fixture = json.loads((Path(directory) / "responses.json").read_text(encoding="utf-8"))
    def handle(request):
        if request.method == "GET":
            item = fixture["discovery"]
        else:
            body = json.loads(request.content)
            kind = "tools" if "tools" in body else "json" if "JSON" in body["messages"][0]["content"] else "liveness"
            item = fixture["models"][body["model"]][kind]
        if item.get("error") == "network":
            raise httpx.ConnectError("Offline simulated failure", request=request)
        if item.get("error") == "timeout":
            raise httpx.ReadTimeout("Offline simulated timeout", request=request)
        if "sse" in item:
            return httpx.Response(item.get("status", 200), text=item["sse"], headers={"content-type": "text/event-stream"})
        return httpx.Response(item.get("status", 200), json=item.get("body", {}))
    return httpx.MockTransport(handle)

def global_outage(results):
    return bool(results) and sum(r["error_class"] in {"network", "auth", "timeout"} for r in results) / len(results) >= .8

async def run(args, *, transport=None, now=None):
    now = now or datetime.now(timezone.utc)
    cfg = config.load(args.config)
    settings = cfg["settings"]
    directory = Path(args.data_dir)
    state = copy.deepcopy(history.load(directory / "history.json"))
    previous = publish.read_status(directory / "status.json")
    key = os.environ.get("NVIDIA_API_KEY", "")
    if args.offline:
        transport = offline_transport(args.offline)
        key = "offline-placeholder"
    elif transport is None and not key:
        raise discover.AuthError()
    # No debug logging, request/response dumps, redirects, or exception interpolation.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    budget = Budget(settings["max_models_per_run"]+10, jitter=transport is None)
    notes, results = [], []
    state["run"] += 1
    for mid in cfg["models"]:
        state["models"].setdefault(mid, history.model_state())
    # Removed configuration entries have no remaining scheduling purpose.
    state["models"] = {mid: state["models"][mid] for mid in cfg["models"]}
    try:
        async with httpx.AsyncClient(base_url=settings["base_url"].rstrip("/")+"/",
                headers={settings["auth_header"]: settings["auth_scheme"]+" "+key,
                         "User-Agent": "nim-model-status/1.0 (health probe)"},
                timeout=settings["probe_timeout_s"], transport=transport, follow_redirects=False,
                limits=httpx.Limits(max_connections=5), trust_env=False) as client:
            try:
                async with asyncio.timeout(settings["probe_timeout_s"]):
                    state["catalog"] = await discover.discover(client)
            except discover.AuthError:
                raise
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
                if not state["catalog"] and previous:
                    state["catalog"] = [mid for mid, m in previous["models"].items() if m.get("listed")]
                if not state["catalog"]:
                    raise RunError("Discovery failed and no previous catalog exists") from None
                notes.append("Discovery failed; using previous catalog")
            for mid, m in state["models"].items():
                if mid not in state["catalog"]:
                    m["circuit"] = "unavailable"
            eligible = [mid for mid, m in state["models"].items() if scoring.due(m, state["run"], now)]
            eligible.sort(key=lambda mid: (state["models"][mid].get("last_probed", ""), mid))
            selected = eligible[:settings["max_models_per_run"]]
            prober = Prober(client, settings, budget)
            # Complete liveness first so capability requests cannot starve rotation.
            async def live(mid):
                return mid, await prober.liveness(mid, now)
            batch = await asyncio.gather(*(live(mid) for mid in selected))
            for mid, rec in batch:
                if rec:
                    results.append(rec)
                    scoring.record(state["models"][mid], rec, state["run"], settings)
            if global_outage(results):
                raise RunError("Global/probe-side outage (>=80% network/auth/timeout); previous files retained")
            pending = []
            for mid, rec in batch:
                if rec is None or not rec["ok"]:
                    continue
                for cap in sorted(set(cfg["models"][mid]["capabilities"]) & {"tools", "json"}):
                    old = state["models"][mid]["capabilities"].get(cap)
                    if old is None or (now-history.parse(old["ts"])).total_seconds() >= 21600:
                        pending.append((old["ts"] if old else "", mid, cap))
            # Least-recent verification first prevents starvation across runs.
            for _, mid, cap in sorted(pending):
                result = await prober.capability(mid, cap, now)
                if result:
                    state["models"][mid]["capabilities"][cap] = result
                    results.append(result)
            if global_outage(results):
                raise RunError("Global/probe-side outage; previous files retained")
    finally:
        logging.info("Probe requests: %d / %d (discovery: 1 additional GET)", budget.count, budget.limit)
    history.prune(state, now)
    try:
        value = publish.build(cfg, state, previous, now, discover.unclassified(state["catalog"], cfg), notes,
                              os.environ.get("GITHUB_RUN_ID", "local"))
    except ValueError:
        raise RunError("Refusing publication: all aliases empty or invalid status schema; previous files retained") from None
    changed = publish.should_publish(previous, value, now)
    print(f"{'Dry run' if args.dry_run else 'Run'}: {len(selected)} models selected; {budget.count}/{budget.limit} probe requests; health={value['health']}; status {'updated' if changed else 'unchanged'}")
    for alias, entry in value["aliases"].items():
        print(f"  {alias}: {entry['primary'] or '(empty)'}; fallbacks={len(entry['fallbacks'])}; stale={entry['stale']}")
    if not args.dry_run:
        # Prepare and validate everything before any writes. Each file is atomic.
        history.atomic_write(directory / "history.json", state)
        if changed:
            history.atomic_write(directory / "status.json", value)
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--offline", metavar="FIXTURE_DIR")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--config", default="models.yml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        return asyncio.run(run(args))
    except discover.AuthError:
        logging.error("NVIDIA_API_KEY missing, invalid, or expired; previous files retained")
    except RunError as error:
        logging.error("%s", error)
    except Exception:
        # Even third-party exception text might contain credentials. Never print it.
        logging.error("Run failed; previous data retained. Check configuration, discovery, outage guard and alias eligibility. See README troubleshooting.")
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
