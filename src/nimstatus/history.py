"""Bounded rolling state and crash-safe single-file writes."""
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import tempfile

ERROR_CLASSES = ("ok", "timeout", "throttled", "server_error", "not_found", "auth", "client_error", "network", "malformed")

def pack(value):
    """Column-free compact rows keep 48h/40 models comfortably below 500 KB.

    Rows: seconds since base, ok, HTTP status, error enum index, TTFT ms,
    total ms, canary. Durations are rounded to integer milliseconds on disk.
    """
    import copy
    value = copy.deepcopy(value)
    for model in value["models"].values():
        records = model["records"]
        base = int(parse(records[0]["ts"]).timestamp()) if records else 0
        rows = [[int(parse(r["ts"]).timestamp())-base, int(r["ok"]), r["http_status"],
                 ERROR_CLASSES.index(r["error_class"]),
                 round(r["ttft_ms"]) if r["ttft_ms"] is not None else None,
                 round(r["total_ms"]) if r["total_ms"] is not None else None,
                 int(r["canary_ok"])] for r in records]
        model["records"] = {"base": base, "rows": rows}
    return value

def unpack(value):
    for model in value["models"].values():
        wire = model.get("records")
        if isinstance(wire, dict):
            records = []
            for row in wire["rows"]:
                delta, ok, status, error, ttft, total, canary = row
                if ok not in (0, 1) or canary not in (0, 1) or type(error) is not int or not 0 <= error < len(ERROR_CLASSES):
                    raise ValueError()
                records.append(dict(ts=stamp(datetime.fromtimestamp(wire["base"]+delta, timezone.utc)),
                    ok=bool(ok), http_status=status, error_class=ERROR_CLASSES[error], ttft_ms=ttft,
                    total_ms=total, canary_ok=bool(canary)))
            model["records"] = records
    return value

def stamp(now):
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def parse(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp requires timezone")
    return result

def fresh():
    return {"schema_version": 1, "models": {}, "aliases": {}, "catalog": [], "run": 0}

def load(path):
    try:
        value = unpack(json.loads(Path(path).read_text(encoding="utf-8")))
        if value["schema_version"] != 1 or type(value["run"]) is not int:
            raise ValueError()
        if not isinstance(value["models"], dict) or not isinstance(value["aliases"], dict):
            raise ValueError()
        for alias in value["aliases"].values():
            if not isinstance(alias, dict) or type(alias["streak"]) is not int or alias["streak"] < 0:
                raise ValueError()
            if any(alias.get(key) is not None and not isinstance(alias[key], str) for key in ("primary", "challenger")):
                raise ValueError()
        if not isinstance(value["catalog"], list) or not all(isinstance(x, str) for x in value["catalog"]):
            raise ValueError()
        for model in value["models"].values():
            if model.get("last_probed"):
                parse(model["last_probed"])
            if model["circuit"] not in ("closed", "open", "unavailable") or not isinstance(model["records"], list):
                raise ValueError()
            if not isinstance(model["capabilities"], dict) or set(model["capabilities"]) - {"tools", "json"}:
                raise ValueError()
            for key in ("hard", "recovery", "backoff", "next_run"):
                if type(model[key]) is not int or model[key] < 0:
                    raise ValueError()
            for rec in model["records"]:
                parse(rec["ts"])
                if type(rec["canary_ok"]) is not bool or (rec["http_status"] is not None and type(rec["http_status"]) is not int):
                    raise ValueError()
                if type(rec["ok"]) is not bool or rec["error_class"] not in {"ok", "timeout", "throttled", "server_error", "not_found", "auth", "client_error", "network", "malformed"}:
                    raise ValueError()
                for key in ("ttft_ms", "total_ms"):
                    if rec[key] is not None and (type(rec[key]) not in (int, float) or not math.isfinite(rec[key]) or rec[key] < 0):
                        raise ValueError()
            aggregate = model["aggregate"]
            if any(type(aggregate[k]) is not int or aggregate[k] < 0 for k in ("count", "ok")) or not math.isfinite(aggregate["ewma"]):
                raise ValueError()
            for cap in model.get("capabilities", {}).values():
                parse(cap["ts"])
                if type(cap["ok"]) is not bool:
                    raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError):
        logging.warning("History missing or corrupt; starting fresh")
        return fresh()

def model_state():
    return dict(records=[], circuit="closed", hard=0, recovery=0, backoff=0,
                next_run=0, capabilities={}, aggregate={"count": 0, "ok": 0, "ewma": 0.0})

def prune(state, now):
    for model in state["models"].values():
        keep = []
        aggregate = model.setdefault("aggregate", {"count": 0, "ok": 0, "ewma": 0.0})
        for record in model["records"]:
            if (now - parse(record["ts"])).total_seconds() <= 48 * 3600:
                keep.append(record)
            else:
                aggregate["count"] += 1
                aggregate["ok"] += int(record["ok"])
                aggregate["ewma"] = .95 * aggregate["ewma"] + .05 * int(record["ok"])
        model["records"] = keep

def atomic_write(path, value):
    path = Path(path)
    if "run" in value and "catalog" in value:
        value = pack(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(value, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
