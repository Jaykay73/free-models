"""Stdlib-only boundary between probe artifacts and the write-enabled publisher.

Executed with python -I from the trusted workflow revision, never from artifacts.
No imports from the data branch, package installs, eval, or artifact execution.
"""
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime

ID = re.compile(r"[A-Za-z0-9_.:/-]{1,256}\Z")
ERRORS = {"ok", "timeout", "throttled", "server_error", "not_found", "auth", "client_error", "network", "malformed"}

def require(condition):
    if not condition:
        raise ValueError("Publication rejected: invalid artifact structure or content")

def fields(value, required, optional=()):
    require(isinstance(value, dict))
    require(set(required) <= value.keys() <= set(required) | set(optional))

def number(value, minimum=0, maximum=1e15):
    require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum)

def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value) is not None)

def timestamp(value):
    require(isinstance(value, str) and len(value) <= 40)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None)
    return result

def flags(value):
    require(isinstance(value, dict) and value.keys() <= {"tools", "json"})
    require(all(type(v) is bool for v in value.values()))

def validate_status(value):
    fields(value, {"schema_version", "generated_at", "valid_until", "health", "run_id", "aliases", "models", "unclassified", "notes"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(timestamp(value["valid_until"]) > timestamp(value["generated_at"]))
    require(value["health"] in {"ok", "degraded", "stale"})
    identifier(value["run_id"])
    require(isinstance(value["aliases"], dict) and 0 < len(value["aliases"]) <= 100)
    for name, alias in value["aliases"].items():
        identifier(name)
        fields(alias, {"primary", "fallbacks", "stale"}, {"reason"})
        require(type(alias["stale"]) is bool)
        require(isinstance(alias["fallbacks"], list) and len(alias["fallbacks"]) <= 4)
        for mid in alias["fallbacks"]:
            identifier(mid)
        require(len(set(alias["fallbacks"])) == len(alias["fallbacks"]))
        if alias["primary"] is None:
            require(not alias["fallbacks"] and isinstance(alias.get("reason"), str))
        else:
            identifier(alias["primary"])
            require(alias["primary"] not in alias["fallbacks"])
        if "reason" in alias:
            require(isinstance(alias["reason"], str) and len(alias["reason"]) <= 2000)
    require(any(a["primary"] for a in value["aliases"].values()))
    require(isinstance(value["models"], dict) and len(value["models"]) <= 1000)
    for mid, model in value["models"].items():
        identifier(mid)
        fields(model, {"status", "listed", "score", "availability", "p50_total_ms", "p50_ttft_ms", "capability_verified", "last_probed"})
        require(model["status"] in {"closed", "open", "unavailable"} and type(model["listed"]) is bool)
        number(model["score"], maximum=2)
        number(model["availability"], maximum=1)
        for key in ("p50_total_ms", "p50_ttft_ms"):
            if model[key] is not None:
                number(model[key])
        flags(model["capability_verified"])
        if model["last_probed"] is not None:
            timestamp(model["last_probed"])
    require(isinstance(value["unclassified"], list) and len(value["unclassified"]) <= 10000)
    for mid in value["unclassified"]:
        identifier(mid)
    require(isinstance(value["notes"], list) and len(value["notes"]) <= 200)
    require(all(isinstance(n, str) and len(n) <= 2000 for n in value["notes"]))

def validate_history(value):
    fields(value, {"schema_version", "run", "models", "aliases", "catalog"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(type(value["run"]) is int)
    number(value["run"])
    require(isinstance(value["catalog"], list) and len(value["catalog"]) <= 10000)
    for mid in value["catalog"]:
        identifier(mid)
    require(isinstance(value["aliases"], dict) and len(value["aliases"]) <= 100)
    for name, alias in value["aliases"].items():
        identifier(name)
        fields(alias, {"primary", "challenger", "streak"})
        for key in ("primary", "challenger"):
            if alias[key] is not None:
                identifier(alias[key])
        require(type(alias["streak"]) is int)
        number(alias["streak"])
    require(isinstance(value["models"], dict) and len(value["models"]) <= 1000)
    for mid, model in value["models"].items():
        identifier(mid)
        fields(model, {"records", "circuit", "hard", "recovery", "backoff", "next_run", "capabilities", "aggregate"}, {"last_probed"})
        require(model["circuit"] in {"closed", "open", "unavailable"})
        for key in ("hard", "recovery", "backoff", "next_run"):
            require(type(model[key]) is int)
            number(model[key])
        if "last_probed" in model:
            timestamp(model["last_probed"])
        fields(model["aggregate"], {"count", "ok", "ewma"})
        for key in ("count", "ok"):
            require(type(model["aggregate"][key]) is int)
            number(model["aggregate"][key])
        number(model["aggregate"]["ewma"], maximum=1)
        fields(model["records"], {"base", "rows"})
        require(type(model["records"]["base"]) is int)
        number(model["records"]["base"])
        rows = model["records"]["rows"]
        require(isinstance(rows, list) and len(rows) <= 10000)
        for row in rows:
            require(isinstance(row, list) and len(row) == 7)
            for item in row:
                require(item is None or type(item) is int)
                if item is not None:
                    number(item)
            require(row[0] is not None and row[1] in (0, 1) and row[3] in range(9) and row[6] in (0, 1))
            require(row[2] is None or 100 <= row[2] <= 599)
        require(isinstance(model["capabilities"], dict) and model["capabilities"].keys() <= {"tools", "json"})
        for capability in model["capabilities"].values():
            fields(capability, {"ok", "ts"}, {"error_class"})
            require(type(capability["ok"]) is bool)
            timestamp(capability["ts"])
            require(capability.get("error_class", "ok") in ERRORS)

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result

def read_json(path, limit):
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= limit)
    text = path.read_text(encoding="utf-8")
    require(not re.search(r"nvapi-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{25,}|github_pat_", text))
    return json.loads(text, object_pairs_hook=unique_object)

def publish(incoming, destination, expected_revision):
    incoming, destination = Path(incoming), Path(destination)
    expected = {"base-revision.txt", "data", "data/history.json", "data/status.json"}
    require(not incoming.is_symlink() and not destination.is_symlink())
    entries = list(incoming.rglob("*"))
    require({p.relative_to(incoming).as_posix() for p in entries} == expected)
    require(not any(p.is_symlink() for p in entries))
    revision = incoming / "base-revision.txt"
    require(revision.stat().st_size <= 100)
    base = revision.read_text(encoding="ascii").strip()
    require(re.fullmatch(r"[0-9a-f]{40}", base) is not None and base == expected_revision)
    status = read_json(incoming / "data/status.json", 1_000_000)
    history = read_json(incoming / "data/history.json", 8_000_000)
    validate_status(status)
    validate_history(history)
    for name in ("status.json", "history.json"):
        require(not (destination / name).is_symlink())
    # Only validated JSON is reserialized. No artifact path is used as a command.
    for name, value in (("history.json", history), ("status.json", status)):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination, delete=False) as stream:
                temporary = stream.name
                json.dump(value, stream, separators=(",", ":"), allow_nan=False)
                stream.write("\n")
            os.replace(temporary, destination / name)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

if __name__ == "__main__":
    try:
        revision = subprocess.check_output(["git", "-C", sys.argv[2], "rev-parse", "HEAD"], text=True).strip()
        publish(sys.argv[1], sys.argv[2], revision)
    except Exception:
        # Artifact contents and exception text are never printed.
        sys.exit("Publication rejected; invalid artifact or data branch changed. Rerun the probe.")
