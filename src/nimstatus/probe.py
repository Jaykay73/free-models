"""Tiny probes. No response bodies, headers or exception messages are logged."""
import asyncio
import json
import random
import re
import time
import httpx
from .discover import AuthError
from .history import stamp

ERRORS = {401: "auth", 403: "auth", 404: "not_found", 410: "not_found", 429: "throttled"}

def error_class(status):
    return ERRORS.get(status, "server_error" if status >= 500 else "client_error" if status >= 400 else "malformed")

def clean(text):
    return re.sub(r"<think\b[^>]*>.*?(?:</think>|$)", "", text, flags=re.S | re.I).strip()

class Budget:
    def __init__(self, limit, jitter=True):
        self.limit, self.count, self.jitter = limit, 0, jitter
        self.semaphore = asyncio.Semaphore(5)
        self.launch_lock = asyncio.Lock()

    async def acquire(self):
        async with self.launch_lock:
            if self.count >= self.limit:
                return False
            if self.jitter:
                await asyncio.sleep(random.uniform(0, 2))
            self.count += 1
            return True

class Prober:
    def __init__(self, client, settings, budget):
        self.client, self.settings, self.budget = client, settings, budget

    async def request(self, payload, stream=False):
        async with self.budget.semaphore:
            if not await self.budget.acquire():
                return None
            start = time.monotonic()
            status, ttft = None, None
            try:
                async with asyncio.timeout(self.settings["probe_timeout_s"]), self.client.stream("POST", "chat/completions", json=payload | {"stream": stream}) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise AuthError()
                    if status != 200:
                        # Inspect only a bounded error string to detect unsupported options.
                        body = (await response.aread())[:8192].decode(errors="replace").lower()
                        unsupported = status in (400, 422) and any(x in body for x in ("not support", "unsupported", "not permitted", "extra inputs"))
                        return dict(error=error_class(status), status=status, unsupported=unsupported, option=body if unsupported else "",
                                    total_ms=(time.monotonic()-start)*1000)
                    if stream and "text/event-stream" in response.headers.get("content-type", ""):
                        content, finish = "", None
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            chunk = json.loads(data)
                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            choice = choices[0]
                            delta = choice.get("delta", {}).get("content") or ""
                            if delta and ttft is None:
                                ttft = (time.monotonic() - start) * 1000
                            content += delta
                            finish = choice.get("finish_reason") or finish
                        message = {"content": content}
                    else:
                        data = json.loads(await response.aread())
                        choice = data["choices"][0]
                        message, finish = choice["message"], choice.get("finish_reason")
                    if not isinstance(message, dict) or (message.get("content") is not None and not isinstance(message["content"], str)):
                        raise ValueError("Malformed completion message")
                    return dict(error="ok", status=status, message=message, finish=finish,
                                ttft_ms=ttft, total_ms=(time.monotonic() - start) * 1000)
            except AuthError:
                raise
            except (httpx.TimeoutException, TimeoutError):
                kind = "timeout"
            except httpx.RequestError:
                kind = "network"
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                kind = "malformed"
            return dict(error=kind, status=status, total_ms=(time.monotonic() - start) * 1000)

    def payload(self, model, prompt):
        return dict(model=model, messages=[{"role": "user", "content": prompt}],
                    max_tokens=self.settings["max_tokens"], temperature=0)

    async def liveness(self, model, now):
        payload = self.payload(model, "Reply with exactly the word: PONG")
        result = await self.request(payload, stream=True)
        if result and result.get("unsupported") and "stream" in result.get("option", ""):
            result = await self.request(payload, stream=False)
        if result is None:
            return None
        content = clean(result.get("message", {}).get("content") or "")
        canary = "pong" in content.lower()
        ok = result["error"] == "ok" and (canary or (bool(content) and result.get("finish") in ("stop", "length")))
        return dict(ts=stamp(now), ok=ok, http_status=result["status"],
                    error_class=result["error"] if result["error"] != "ok" or ok else "malformed",
                    ttft_ms=result.get("ttft_ms"), total_ms=result.get("total_ms"), canary_ok=canary)

    async def capability(self, model, cap, now):
        if cap == "tools":
            payload = self.payload(model, "Call get_weather for city Paris. Use the tool, do not answer directly.")
            payload["tools"] = [{"type": "function", "function": {"name": "get_weather", "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
            payload["tool_choice"] = "auto"
        else:
            payload = self.payload(model, 'Return only a JSON object with key "ok" and boolean value true.')
            payload["response_format"] = {"type": "json_object"}
        result = await self.request(payload)
        if result and cap == "json" and result.get("unsupported") and "response_format" in result.get("option", ""):
            payload.pop("response_format")
            result = await self.request(payload)
        if result is None:
            return None
        valid = False
        if result["error"] == "ok":
            try:
                message = result["message"]
                if cap == "json":
                    valid = isinstance(json.loads(clean(message["content"])), dict)
                else:
                    for call in message.get("tool_calls", []):
                        fn = call["function"]
                        args = json.loads(fn["arguments"])
                        valid |= bool(call.get("id") and call.get("type") == "function" and fn["name"] == "get_weather" and
                                      isinstance(args, dict) and isinstance(args.get("city"), str) and args["city"].strip())
            except (ValueError, KeyError, TypeError, AttributeError):
                valid = False
        return {"ok": valid, "ts": stamp(now), "error_class": result["error"]}
