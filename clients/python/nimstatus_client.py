"""Stdlib-only status resolution. Never accepts inference credentials."""
from datetime import datetime, timezone
import json
import math
import sys
import time
import urllib.request
import warnings

__version__ = "0.1.0"
DEFAULT_STATUS_URL = "https://jaykay73.github.io/free-models/status.json"
_cache = {}


class NoModelsAvailable(RuntimeError):
    """Neither the directory, cache nor optional defaults supplied a model."""


def is_retryable(error):
    """Retry 429/5xx and connection/timeouts; never retry auth or bad requests.

    Recognizes OpenAI SDK connection/timeout exceptions if the caller has
    imported that SDK. The SDK is not a dependency of this library.
    """
    status = getattr(error, "status_code", getattr(error, "code", None))
    if type(status) is int:
        return status == 429 or 500 <= status <= 599
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    sdk = sys.modules.get("openai")
    connection_error = getattr(sdk, "APIConnectionError", None)
    return isinstance(connection_error, type) and isinstance(error, connection_error)

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

def resolve(alias, options=None, *, status_url=DEFAULT_STATUS_URL, ttl_s=300, defaults=None):
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

def call_with_fallback(alias, fn, options=None, *, retryable=is_retryable, max_attempts=5, **resolve_options):
    """Try directory-selected models; optionally override error classification."""
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    models = resolve(alias, options, **resolve_options)[:max_attempts]
    if not models:
        raise NoModelsAvailable("No models available: directory/cache/defaults have no route for the requested alias")
    for index, model in enumerate(models):
        try:
            return fn(model)
        except Exception as exc:
            if not retryable(exc) or index == len(models)-1:
                raise


class ModelRouter:
    """Choose aliases once; refresh model names automatically through the cache.

    No API keys or inference SDK objects are accepted. `call` invokes caller
    code with a model name, leaving inference and credentials in the app.
    Instances share the process-local per-URL cache. Construction does no I/O.
    """

    def __init__(self, *, status_url=DEFAULT_STATUS_URL, ttl_s=300, defaults=None,
                 max_attempts=5, retryable=is_retryable):
        if not isinstance(status_url, str) or not status_url:
            raise ValueError("status_url must be a nonempty string")
        if type(ttl_s) not in (int, float) or not math.isfinite(ttl_s) or ttl_s < 300:
            raise ValueError("ttl_s must be at least 300")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if not callable(retryable):
            raise ValueError("retryable must be callable")
        if defaults is not None and (not isinstance(defaults, dict) or any(
            not isinstance(alias, str) or not isinstance(models, (list, tuple)) or
            any(not isinstance(model, str) or not model for model in models)
            for alias, models in defaults.items()
        )):
            raise ValueError("defaults must map aliases to lists of model names")
        self.options = dict(status_url=status_url, ttl_s=ttl_s,
                            defaults={a: list(m) for a, m in (defaults or {}).items()})
        self.max_attempts, self.retryable = max_attempts, retryable

    def resolve(self, alias="fast"):
        """Return the primary and fallbacks, refreshing at most once per TTL."""
        return resolve(alias, self.options)

    def primary(self, alias="fast"):
        """Return the current primary. Prefer `call` for automatic fallback."""
        models = self.resolve(alias)
        if not models:
            raise NoModelsAvailable("No models available for the requested alias")
        return models[0]

    def call(self, alias, fn):
        """Run fn(model_name), falling back on retryable failures.

        Synchronous only. Exceptions while consuming a returned stream occur
        outside this call and must be handled by the application.
        """
        return call_with_fallback(alias, fn, self.options, retryable=self.retryable,
                                  max_attempts=self.max_attempts)
