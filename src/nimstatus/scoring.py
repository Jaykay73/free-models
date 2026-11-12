"""Recent weighted availability, circuit scheduling and persistent alias hysteresis."""
import statistics
from .config import TIERS
from .history import parse

HARD = {"timeout", "server_error", "network"}

def due(model, run, now):
    if model["circuit"] == "unavailable":
        return not model.get("last_probed") or (now - parse(model["last_probed"])).total_seconds() >= 86400
    return run >= model["next_run"]

def record(model, probe, run, settings):
    model["records"].append(probe)
    model["last_probed"] = probe["ts"]
    kind = probe["error_class"]
    if kind == "not_found":
        model.update(circuit="unavailable", hard=0, recovery=0)
        return
    if model["circuit"] == "unavailable":
        model.update(circuit="open", recovery=0)
    model["hard"] = model["hard"] + 1 if kind in HARD else 0
    if model["circuit"] == "closed" and model["hard"] >= 3:
        model.update(circuit="open", backoff=0, recovery=0)
    if model["circuit"] == "open":
        model["recovery"] = model["recovery"] + 1 if probe["ok"] else 0
        if model["recovery"] >= 2:
            model.update(circuit="closed", hard=0, recovery=0, backoff=0, next_run=run+1)
        else:
            skip = min(2 ** min(model["backoff"], 16), max(1, int(360 / settings["interval_minutes"])-1))
            model["next_run"] = run + skip + 1
            model["backoff"] += 1

def metrics(model, preference, now, settings):
    records = [r for r in model["records"] if 0 <= (now - parse(r["ts"])).total_seconds() <= 86400]
    weights = [2 ** (-(now - parse(r["ts"])).total_seconds() / (settings["half_life_hours"] * 3600)) for r in records]
    availability = sum(w * (1 if r["ok"] else .4 if r["error_class"] == "throttled" else 0) for r, w in zip(records, weights)) / sum(weights) if weights else 0
    def median(key):
        values = [r[key] for r in records if r["ok"] and r.get(key) is not None]
        return round(statistics.median(values), 2) if values else None
    total, ttft = median("total_ms"), median("ttft_ms")
    latency = ttft if ttft is not None else total
    factor = .2 if latency is None else max(.2, min(1, 1 - .8 * (latency-settings["latency_target_ms"]) / (settings["latency_ceiling_ms"]-settings["latency_target_ms"])))
    return dict(score=max(0, availability**2 * factor * (1+preference)), availability=availability,
                latency_factor=factor, p50_total_ms=total, p50_ttft_ms=ttft)

def rank_alias(name, alias, config, state, models):
    def score(mid):
        m = models[mid]
        return m["score"] * (m["latency_factor"] if alias.get("prefer_speed") else 1)
    eligible = []
    for mid, spec in config["models"].items():
        m = models[mid]
        required = set(alias["require"])
        if (m["listed"] and m["status"] == "closed" and m["availability"] > 0 and
            TIERS[spec["tier"]] >= TIERS[alias["min_tier"]] and required <= set(spec["capabilities"]) and
            all(m["capability_verified"].get(c) is True for c in required & {"tools", "json"})):
            eligible.append(mid)
    eligible.sort(key=lambda m: (-score(m), m))
    previous = state["aliases"].get(name, {})
    primary = previous.get("primary")
    challenger, streak = None, 0
    if not eligible:
        state["aliases"][name] = dict(primary=None, challenger=None, streak=0)
        return dict(primary=None, fallbacks=[], stale=False, reason="No eligible healthy classified models")
    best = eligible[0]
    if primary not in eligible or score(primary) < .5:
        primary = best
    elif best != primary and score(best) - score(primary) >= .15 - 1e-12:
        challenger = best
        streak = previous.get("streak", 0) + 1 if previous.get("challenger") == best else 1
        if streak >= 3:
            primary, challenger, streak = best, None, 0
    state["aliases"][name] = dict(primary=primary, challenger=challenger, streak=streak)
    return dict(primary=primary, fallbacks=[m for m in eligible if m != primary][:4], stale=False)
