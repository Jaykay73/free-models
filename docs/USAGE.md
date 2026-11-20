# Use the deployed model directory

Fetch **https://jaykay73.github.io/free-models/status.json**. The GitHub repository URL is source code, not the JSON endpoint. If Pages is temporarily unavailable, the alternative is **https://raw.githubusercontent.com/Jaykay73/free-models/data/status.json**.

```bash
curl -fsS https://jaykay73.github.io/free-models/status.json
```

Choose an alias:

| Alias | Intended use |
| --- | --- |
| `fast` | Latency-sensitive chat |
| `general` | General chat, medium tier or above |
| `code` | Manually classified coding models |
| `tools` | Models that passed the tool-call check |
| `json` | Models that passed the JSON check |

Each alias contains `primary`, up to four `fallbacks`, and a `stale` flag. `primary` can be null. Check the document's `valid_until` too. A healthy probe is not a model-quality or account-quota guarantee. Cache for at least five minutes; do not fetch before every inference request.

## Python

After the first PyPI release, install the standalone client and an inference SDK:

```bash
python -m pip install nimstatus openai
```

Once the client-package changes have been pushed, other projects can install it directly from GitHub:

```bash
python -m pip install "nimstatus @ git+https://github.com/Jaykay73/free-models.git@main#subdirectory=clients/python"
```

Use a reviewed commit SHA instead of `main` in production dependencies. Model selections still refresh from the status URL. The client requires Python 3.11+ and has no runtime dependencies. Install it in your application's environment, separate from any editable install of the monitor, which shares the import name.

**No model IDs or default model environment variable are required.** Your app chooses an alias once, then calls the router for each request:

```python
import os
from openai import OpenAI
from nimstatus import ModelRouter

router = ModelRouter(max_attempts=3)  # defaults to your deployed status URL

api = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.environ["NVIDIA_API_KEY"],
    max_retries=0,
    timeout=30,
)

answer = router.call(
    "fast",
    lambda model: api.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Explain a Python dictionary."}],
        max_tokens=256,
    ),
)
print(answer.choices[0].message.content)
```

The router refreshes model choices through its five-minute cache. Call it for each inference instead of saving one resolved model forever. It recognizes SDK connection/timeouts and HTTP 429/5xx automatically; auth and bad-request errors are rethrown immediately. You can override error classification with `ModelRouter(retryable=...)`. Disable SDK retries as shown to keep application attempts predictable. This example uses non-streaming completions; streaming failures after a stream is returned need handling in your application.

If you only need model names, use `router.primary("fast")` or `router.resolve("code")`. The existing `resolve` and `call_with_fallback` functions remain available. Your application owns its SDK client and key; the router only passes model names to your callback. [Full client reference](../clients/python/README.md).

## JavaScript

Copy `clients/js/nimstatus-client.mjs` into your app. Resolving names works in Node 18+ and browsers:

```js
import {resolve} from './nimstatus-client.mjs';

const models = await resolve('fast', {
  status_url: 'https://jaykay73.github.io/free-models/status.json',
  ttl_s: 300,
});
console.log(models);
```

For inference, use the following only in your trusted Node backend, with the OpenAI SDK installed:

```js
import OpenAI from 'openai';
import {call_with_fallback} from './nimstatus-client.mjs';

const api = new OpenAI({
  baseURL: 'https://integrate.api.nvidia.com/v1',
  apiKey: process.env.NVIDIA_API_KEY,
  maxRetries: 0,
  timeout: 30_000,
});
const result = await call_with_fallback('fast', model =>
  api.chat.completions.create({
    model,
    messages: [{role: 'user', content: 'Explain a JavaScript Map.'}],
    max_tokens: 256,
  }), {
    status_url: 'https://jaykay73.github.io/free-models/status.json',
    max_attempts: 3,
    retryable: error => error instanceof OpenAI.APIConnectionError ||
      error.status === 429 || error.status >= 500,
  });
console.log(result.choices[0].message.content);
```

Never expose your NVIDIA key in browser JavaScript. Only the public status fetch belongs in the browser.

## Failure behavior

Both helpers cache per URL in memory and retain the previous good document if a refresh fails. Optional supplied defaults provide an emergency route; they are not required for normal use. Without defaults or a prior cache, failure to fetch a usable alias means Python `resolve` returns an empty list and `ModelRouter.primary`/`call` raise `NoModelsAvailable`. The cache does not survive restarts. Expired or stale routing produces a warning and remains usable. Defaults are not currently appended after every exhausted nonempty remote list. The Python helper's socket timeout is three seconds but is not yet a strict total deadline; do not make process startup depend on a successful fetch.

Your applications no longer swap model IDs. The monitor maintainer still tags newly discovered models in `models.yml`; every application using its aliases benefits from those central updates without changing application code.

The existing six-hour publication heartbeat can make stable status expire between updates. Use your own fallback handling even when the JSON says `health: ok`. These are documented follow-up limitations, not fixed by the workflow hardening update.
