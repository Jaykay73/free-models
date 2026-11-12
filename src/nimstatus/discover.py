"""Catalog discovery. A failed catalog fetch never implies removal."""
import httpx

class AuthError(Exception):
    def __init__(self):
        super().__init__("NVIDIA_API_KEY missing, invalid, or expired")

async def discover(client: httpx.AsyncClient) -> list[str]:
    response = await client.get("models")
    if response.status_code in (401, 403):
        raise AuthError()
    if response.status_code != 200:
        raise ValueError("Discovery unavailable")
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Malformed model catalog")
    if any(not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"] for m in data["data"]):
        raise ValueError("Malformed catalog entry")
    ids = sorted({m["id"] for m in data["data"]})
    if not ids:
        raise ValueError("Empty model catalog; retaining previous catalog")
    return ids

def unclassified(ids, config):
    patterns = config["settings"]["exclude_patterns"]
    return sorted(m for m in ids if m not in config["models"] and
                  not any(p.lower() in m.lower() for p in patterns))
