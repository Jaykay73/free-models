"""Strict configuration validation; identifiers belong in YAML, never code."""
from pathlib import Path
import math
import re
import yaml

TIERS = {"small": 0, "medium": 1, "large": 2}
CAPABILITIES = {"chat", "code", "tools", "json"}
DEFAULTS = dict(base_url="https://integrate.api.nvidia.com/v1", auth_header="Authorization",
                auth_scheme="Bearer", probe_timeout_s=30, max_models_per_run=40,
                exclude_patterns=["embed", "rerank", "guard", "translate", "parse", "clip", "safety"],
                max_tokens=64, interval_minutes=10, latency_target_ms=3000,
                latency_ceiling_ms=20000, half_life_hours=6)

def load(path: str | Path) -> dict:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ValueError("Cannot read models.yml as YAML") from None
    if not isinstance(raw, dict) or set(raw) - {"settings", "models", "aliases"}:
        raise ValueError("Configuration needs settings, models and aliases mappings")
    settings = raw.get("settings", {})
    if not isinstance(settings, dict) or set(settings) - DEFAULTS.keys():
        raise ValueError("Unknown setting or invalid settings mapping")
    settings = DEFAULTS | settings
    for key in ("probe_timeout_s", "max_models_per_run", "max_tokens", "interval_minutes",
                "latency_target_ms", "latency_ceiling_ms", "half_life_hours"):
        value = settings[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"settings.{key} must be positive and finite")
    for key in ("max_models_per_run", "max_tokens", "interval_minutes"):
        if type(settings[key]) is not int:
            raise ValueError(f"settings.{key} must be an integer")
    if settings["latency_ceiling_ms"] <= settings["latency_target_ms"]:
        raise ValueError("latency_ceiling_ms must exceed latency_target_ms")
    if not isinstance(settings["base_url"], str) or not re.fullmatch(r"https://[^/?#@\s]+(?:/[^?#\s]*)?", settings["base_url"]):
        raise ValueError("base_url must be HTTPS without credentials, query or fragment")
    for key in ("auth_header", "auth_scheme"):
        if not isinstance(settings[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", settings[key]):
            raise ValueError(f"Invalid {key}")
    patterns = settings["exclude_patterns"]
    if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
        raise ValueError("exclude_patterns must be nonempty substring strings")
    models, aliases = raw.get("models"), raw.get("aliases")
    if not isinstance(models, dict) or not models or not isinstance(aliases, dict) or not aliases:
        raise ValueError("models and aliases must be nonempty mappings")
    for name, model in models.items():
        if not isinstance(name, str) or not re.fullmatch(r"[\w./:-]+", name):
            raise ValueError("Invalid model identifier")
        if not isinstance(model, dict) or set(model) - {"tier", "capabilities", "preference"}:
            raise ValueError(f"Invalid model configuration: {name}")
        if model.get("tier") not in TIERS:
            raise ValueError(f"Unknown tier for {name}")
        caps = model.get("capabilities")
        if not isinstance(caps, list) or not caps or any(c not in CAPABILITIES for c in caps):
            raise ValueError(f"Invalid capabilities for {name}")
        pref = model.setdefault("preference", 0)
        if type(pref) not in (int, float) or not math.isfinite(pref) or not -1 <= pref <= 1:
            raise ValueError(f"preference for {name} must be between -1 and 1")
    for name, alias in aliases.items():
        if not isinstance(name, str) or not isinstance(alias, dict) or set(alias) - {"min_tier", "require", "prefer_speed"}:
            raise ValueError("Invalid alias configuration")
        if alias.get("min_tier") not in TIERS:
            raise ValueError(f"Unknown tier for alias {name}")
        caps = alias.get("require")
        if not isinstance(caps, list) or not caps or any(c not in CAPABILITIES for c in caps):
            raise ValueError(f"Invalid requirements for alias {name}")
        if type(alias.get("prefer_speed", False)) is not bool:
            raise ValueError(f"prefer_speed must be boolean for {name}")
        if not any(set(caps) <= set(m["capabilities"]) and TIERS[m["tier"]] >= TIERS[alias["min_tier"]] for m in models.values()):
            raise ValueError(f"No configured model satisfies alias {name}")
    return dict(settings=settings, models=models, aliases=aliases)
