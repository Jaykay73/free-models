# nimstatus

A dependency-free Python 3.11+ library that replaces hardcoded NVIDIA model IDs
with aliases: `fast`, `general`, `code`, `tools`, and `json`.

## Install

After the first PyPI release:

```bash
python -m pip install nimstatus
```

To install from a local checkout:

```bash
python -m pip install ./clients/python
```

After these files have been pushed to GitHub:

```bash
python -m pip install "nimstatus @ git+https://github.com/Jaykay73/free-models.git@main#subdirectory=clients/python"
```

For reproducible application builds, replace `main` with a reviewed commit SHA.
This pins the client implementation; model selections still refresh dynamically.
Use the client in your application environment. Use a separate environment if you also install the monitor, which shares the `nimstatus` import name.

## Use without model IDs

```python
from nimstatus import ModelRouter

router = ModelRouter()
print(router.primary("fast"))
print(router.resolve("code"))
```

The default directory is `https://jaykay73.github.io/free-models/status.json`.
No network request occurs when constructing the router. Calls refresh its
process-local cache at most once every five minutes.

For inference, install the OpenAI SDK in your application and provide your
key only to that SDK:

```python
import os
from openai import OpenAI
from nimstatus import ModelRouter

api = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.environ["NVIDIA_API_KEY"],
    max_retries=0,
    timeout=30,
)
router = ModelRouter(max_attempts=3)

answer = router.call("fast", lambda model: api.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Explain what a dictionary is."}],
    max_tokens=256,
))
print(answer.choices[0].message.content)
```

Only the alias stays in application code. Each call resolves current routing
through the cache; 429, 5xx and recognized connection/timeout failures try the
next model. Auth and bad-request errors propagate immediately. Unknown errors
are not retried. Override classification with `ModelRouter(retryable=...)` for
other SDKs. SDK retrying is disabled in the example so attempts remain bounded.

This library sends no inference traffic and accepts no API key. Your callback
calls NVIDIA directly. The monitor uses a separate maintainer key for probes.

## Outages and limits

Expired status and failed refreshes retain the last good in-memory document
with a warning. Optional `defaults={alias: [...]}` can provide emergency model
names, but is **not required**. Without defaults or a prior cache, a failed first
fetch means `resolve` returns an empty list and `primary`/`call` raise
`NoModelsAvailable`. The cache does not survive process restarts. No library can
discover a model during an outage without some available source of model names.

Only synchronous calls are supported. Fallback handles failures raised by the
callback, not errors while consuming a returned streaming iterator. The current
directory fetch has a three-second caller deadline. Defaults apply when no route is available and are not appended after an
otherwise nonempty remote route. Stale routing can currently be retained
indefinitely. Do not depend on a successful directory fetch to start your app.

Consumers need no NVIDIA model IDs; the **monitor maintainer still classifies
models in `models.yml`**. New unclassified models never enter aliases until tagged.
