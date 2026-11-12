"""Stdlib-only status resolution. Never accepts inference credentials."""
from datetime import datetime, timezone
import json
import math
import time
import urllib.request
import warnings

_cache = {}

def _validate(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("aliases"), dict):
        raise ValueError("Invalid status document")
    expiry = datetime.fromisoformat(data["valid_until"].replace("Z", "+00:00"))
    if expiry.tzinfo is None:
        raise ValueError("Missing expiry timezone")
    for entry in data["aliases"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("fallbacks"), list) or type(entry.get("stale")) is not bool:
            raise ValueError("Invalid alias")
        if entry.get("primary") is not None and not isinstance(entry["primary"], str):
            raise ValueError("Invalid primary")
        if any(not isinstance(m, str) or not m for m in entry["fallbacks"]):
            raise ValueError("Invalid fallback")
    return data

def resolve(alias, options=None, *, status_url=None, ttl_s=300, defaults=None):
    """Return ordered names; warnings flag expired, retained or fallback routing.

    Accepts an options dict for parity with JavaScript, or Python keyword args.
    Cache is per URL. Failed refreshes are also delayed by TTL to avoid a storm.
    """
    options = options or {}
    status_url = options.get("status_url", status_url)
    ttl_s = options.get("ttl_s", ttl_s)
    defaults = options.get("defaults", defaults) or {}
    if not status_url or not math.isfinite(ttl_s) or ttl_s < 300:
        raise ValueError("status_url required; ttl_s must be at least 300")
    now = time.monotonic()
    cached = _cache.get(status_url)
    if cached is None or now - cached["checked"] >= ttl_s:
        try:
            with urllib.request.urlopen(status_url, timeout=3) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("Status document too large")
                data = _validate(json.loads(raw))
            cached = {"data": data, "checked": now, "failed": False}
        except Exception:
            cached = {"data": cached["data"] if cached else None, "checked": now, "failed": True}
        _cache[status_url] = cached
    data = cached["data"]
    entry = (data or {}).get("aliases", {}).get(alias, {})
    names = [entry.get("primary"), *entry.get("fallbacks", [])] if entry.get("primary") else []
    if cached["failed"] or not names or entry.get("stale") or (data and datetime.now(timezone.utc) > datetime.fromisoformat(data["valid_until"].replace("Z", "+00:00"))):
        warnings.warn("nimstatus: stale status or caller defaults in use", RuntimeWarning, stacklevel=2)
    return list(dict.fromkeys(names or defaults.get(alias, [])))

def call_with_fallback(alias, fn, options=None, *, retryable, max_attempts=5, **resolve_options):
    """Caller classifies exceptions; auth and bad requests should return False."""
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    models = resolve(alias, options, **resolve_options)[:max_attempts]
    if not models:
        raise RuntimeError("No models available for alias")
    for index, model in enumerate(models):
        try:
            return fn(model)
        except Exception as exc:
            if not retryable(exc) or index == len(models)-1:
                raise
