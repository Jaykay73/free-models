"""Public schema, last-known-good protection and publication gating."""
from datetime import timedelta
import json
import math
from pathlib import Path
from .history import parse, stamp
from .scoring import metrics, rank_alias

def read_status(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        validate(value)
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None

def validate(value):
    def require(test):
        if not test:
            raise ValueError("Invalid status.json schema")
    require(isinstance(value, dict) and value.get("schema_version") == 1)
    for key in ("generated_at", "valid_until"):
        require(isinstance(value.get(key), str))
        parse(value[key])
    require(parse(value["valid_until"]) > parse(value["generated_at"]))
    require(value.get("health") in ("ok", "degraded", "stale") and isinstance(value.get("run_id"), str))
    require(isinstance(value.get("aliases"), dict) and isinstance(value.get("models"), dict))
    for name, entry in value["aliases"].items():
        require(isinstance(name, str) and isinstance(entry, dict))
        require(entry.get("primary") is None or isinstance(entry["primary"], str) and bool(entry["primary"]))
        require(isinstance(entry.get("fallbacks"), list) and len(entry["fallbacks"]) <= 4 and all(isinstance(x, str) and x for x in entry["fallbacks"]))
        require(len(set(entry["fallbacks"])) == len(entry["fallbacks"]) and entry.get("primary") not in entry["fallbacks"])
        require(type(entry.get("stale")) is bool)
        if entry.get("primary") is None:
            require(isinstance(entry.get("reason"), str) and not entry["fallbacks"])
    for mid, entry in value["models"].items():
        require(isinstance(mid, str) and isinstance(entry, dict))
        require(entry.get("status") in ("closed", "open", "unavailable"))
        require(type(entry.get("listed")) is bool)
        for key in ("score", "availability"):
            require(type(entry.get(key)) in (int, float) and math.isfinite(entry[key]) and entry[key] >= 0)
        require(entry["availability"] <= 1)
        for key in ("p50_total_ms", "p50_ttft_ms"):
            require(key in entry and (entry[key] is None or type(entry[key]) in (int, float) and math.isfinite(entry[key]) and entry[key] >= 0))
        require(isinstance(entry.get("capability_verified"), dict) and all(k in ("tools", "json") and type(v) is bool for k, v in entry["capability_verified"].items()))
        require("last_probed" in entry)
        if entry["last_probed"] is not None:
            parse(entry["last_probed"])
    for key in ("unclassified", "notes"):
        require(isinstance(value.get(key), list) and all(isinstance(x, str) for x in value[key]))
    # Also rejects NaN/Infinity anywhere in extensions.
    json.dumps(value, allow_nan=False)

def build(config, state, previous, now, unknown, notes, run_id):
    models = {}
    for mid, spec in config["models"].items():
        m = state["models"][mid]
        models[mid] = metrics(m, spec["preference"], now, config["settings"]) | dict(
            status=m["circuit"], listed=mid in state["catalog"],
            capability_verified={c: v["ok"] for c, v in m["capabilities"].items()}, last_probed=m.get("last_probed"))
    aliases = {name: rank_alias(name, alias, config, state, models) for name, alias in config["aliases"].items()}
    for name, entry in aliases.items():
        old = (previous or {}).get("aliases", {}).get(name, {})
        if entry["primary"] is None and old.get("primary"):
            aliases[name] = old | {"stale": True}
            notes.append(f"Alias {name} retains last-known-good routing")
    if not any(a["primary"] for a in aliases.values()):
        raise ValueError("Refusing publication: all aliases are empty")
    stale = sum(a["stale"] for a in aliases.values())
    value = dict(schema_version=1, generated_at=stamp(now),
        valid_until=stamp(now+timedelta(minutes=4*config["settings"]["interval_minutes"])),
        health="stale" if stale == len(aliases) else "degraded" if stale or notes or any(a["primary"] is None for a in aliases.values()) else "ok",
        run_id=str(run_id), aliases=aliases, models={k: {x: y for x, y in m.items() if x != "latency_factor"} for k, m in models.items()},
        unclassified=unknown, notes=notes)
    validate(value)
    return value

def meaningful(value):
    return dict(aliases=value["aliases"], unclassified=value["unclassified"], health=value["health"], notes=value["notes"],
                models={k: {key: m[key] for key in ("status", "listed", "capability_verified")} for k, m in value["models"].items()})

def should_publish(previous, value, now):
    return previous is None or meaningful(previous) != meaningful(value) or (now-parse(previous["generated_at"])).total_seconds() >= 21600
