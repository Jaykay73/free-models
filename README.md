# NVIDIA model status and automatic switching

This project checks which configured models are responding on NVIDIA's hosted API. It publishes a first choice and a list of backups for each task. In your app, use a short name like `fast` instead of a specific model ID. The client looks up which model to try and can switch to a backup if the first request fails.

## See which models are available

Open the [public status directory](https://jaykay73.github.io/free-models/status.json) in your browser. You can view it without an account or API key.

Find `aliases` and look under `fast`. Each alias is a short name for a task and has these fields:

| Field | What it means |
| --- | --- |
| `primary` | The model to try first. `null` means there is currently no selection. |
| `fallbacks` | Backup models, in the order the client tries them. An empty list means the alias has no backups. |
| `stale` | When `true`, the monitor is keeping an older choice for this alias. |

At the top of the file, `generated_at` shows when the directory was published and `valid_until` shows when it expires. The `models` section shows the health and last check time for individual models. The checks use the maintainer's NVIDIA account, so access and usage limits can differ on your account.

| Alias | When to use it |
| --- | --- |
| `fast` | Quick chat and a good first test. |
| `general` | Everyday chat and writing. |
| `code` | Coding tasks. Maintainers choose which models qualify. |
| `tools` | Requests that use tool calling. |
| `json` | Requests that need JSON output. |

## Use automatic switching in Python

You need **Python 3.11 or newer**. You can use the public directory without cloning this repository or setting up GitHub Actions. For JavaScript and Node.js, see the [usage guide](docs/USAGE.md#javascript).

### 1. Install the client and try a model lookup

Open PowerShell on Windows or Terminal on macOS or Linux. Run these two commands:

```sh
python -m pip install nimstatus openai
python -c "from nimstatus import ModelRouter; print(ModelRouter().resolve('fast'))"
```

If your Python command is `python3` or `py`, use it in place of `python` throughout this guide. If you're adding this to an existing app, run the installation in that app's Python environment.

The second command prints a list of model IDs. The first is the current choice and the rest are backups. If you get an empty list, open the [status directory](https://jaykay73.github.io/free-models/status.json) to see whether `fast` has a primary model.

### 2. Get your NVIDIA API key

Sign in to [NVIDIA Build](https://build.nvidia.com/), open a hosted model's page, then select **Get API Key** and **Generate Key**. When the example below prompts you, paste the key and press Enter. The terminal hides the key as you paste. If your app already sets `NVIDIA_API_KEY` in its environment, the example uses that value. [NVIDIA's API key instructions](https://docs.nvidia.com/nim/large-language-models/1.7.0/getting-started.html#generate-an-api-key).

### 3. Send a request

Save this as `ask_nvidia.py` in the folder where you opened your terminal.

```python
from getpass import getpass
import os

from nimstatus import ModelRouter
from openai import OpenAI

api_key = os.getenv("NVIDIA_API_KEY") or getpass("Paste your NVIDIA API key: ")
api = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=api_key,
    max_retries=0,
    timeout=30,
)
router = ModelRouter(max_attempts=3)


def ask(model):
    print(f"Trying {model}")
    return api.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "What is a Python dictionary?"}],
        max_tokens=256,
    )


answer = router.call("fast", ask)
print(answer.choices[0].message.content)
```

Run it from the same folder:

```sh
python ask_nvidia.py
```

You'll see `Trying …` followed by the model's answer. If the first request hits a rate limit, times out, loses its connection or gets a server error, the router tries the next available backup. This example tries up to three models total. Your app sends its prompts and key directly to NVIDIA. The public directory only provides model information.

### 4. Use it in your app

Keep one `ModelRouter` and call `router.call("fast", ask)` for each request. Put your existing NVIDIA request inside `ask(model)` and pass that `model` to the SDK. Change `fast` to another alias when you need a different kind of model. The router checks for updated choices at most once every five minutes and can use its last successful lookup if the directory is temporarily unreachable.

`router.resolve("fast")` lists choices. `router.call("fast", ask)` is what tries the backups. Each new request starts with the current first choice. The example uses a standard, non-streaming response. If your app streams responses, handle errors that occur after streaming begins in your app. [Python client reference](clients/python/README.md).

## Troubleshooting

| What you see | What to check |
| --- | --- |
| `python` is unknown | Try `python3 --version` or, on Windows, `py --version`. Install Python 3.11 or newer if needed. |
| `No module named nimstatus` or `No module named openai` | Run the install command with the same Python command and environment you use to run the script. |
| `NoModelsAvailable`, an empty list or a stale-route warning | Check the alias and the directory's timestamps. If Pages is unreachable, try `ModelRouter(status_url="https://raw.githubusercontent.com/Jaykay73/free-models/data/status.json")`. A missing alias or an expired snapshot can also affect the raw copy. |
| `401` or `403` | Check that your NVIDIA key is valid and that your account can call the selected model. Authentication and permission errors stop the request. |
| `400` | Check your request fields and whether the alias supports what you're asking for. Invalid requests stop immediately. |
| `429`, a timeout or a server error after trying backups | The request ran out of eligible backups or reached the attempt limit. Check your NVIDIA usage limits or try again later. |

## Run your own monitor

Use this section if you want to manage the model list or publish a separate directory. The monitor uses Python **3.12** and Git.

1. Fork this repository on GitHub, or push a copy to a public GitHub repository with `main` as its default branch. Enable Actions on the repository.
2. In your repository, open **Settings → Secrets and variables → Actions → New repository secret**. Name the secret `NVIDIA_API_KEY` and paste the key the monitor will use to check models.
3. Review [`models.yml`](models.yml) on `main`. It contains the configured models and the rules for each alias. To add a model, copy its ID from the [NVIDIA catalog](https://integrate.api.nvidia.com/v1/models), then set its tier and capabilities in `models.yml`. Newly discovered models appear under `unclassified` until you configure them. Tool calling and JSON must pass their respective health checks before a model can be newly selected for those aliases.
4. Create the `data` branch if it doesn't already exist. Run these commands from your local repository in Bash or Git Bash on Windows. They create a separate worktree for the empty branch.

   ```bash
   git worktree add --detach ../nim-status-data
   cd ../nim-status-data
   git switch --orphan data
   python -c 'import json; from datetime import datetime, timedelta, timezone; n = datetime.now(timezone.utc); print(json.dumps(dict(schema_version=1, generated_at=n.isoformat(), valid_until=(n+timedelta(minutes=40)).isoformat(), health="stale", run_id="bootstrap", aliases={}, models={}, unclassified=[], notes=["Awaiting first successful probe"])))' > status.json
   touch .nojekyll
   git add status.json .nojekyll
   git commit -m "Initialize status directory"
   git push -u origin data
   cd -
   ```

5. In **Settings → Pages**, choose **Deploy from a branch**, select `data` and `/ (root)`, then save.
6. Under **Actions**, run **Probe NVIDIA models** on `main`. Wait for both the probe and publish jobs to succeed, then check **Refresh branch-source Pages**. Run **Keep schedules enabled** once as well. See the [maintainer setup guide](docs/SECURITY_SETUP.md#2-apply-branch-protection) for applying branch protections.
7. Open your published site URL from **Settings → Pages**, add `/status.json`, and check that `aliases` has a primary model. Use that URL in your app with `ModelRouter(status_url="YOUR_STATUS_URL")`.

The workflow is scheduled every ten minutes, though GitHub may delay runs. If the directory stops updating, check the latest probe and Pages runs in **Actions**. Failed probes can leave the previous directory in place. [GitHub schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

### Test the monitor locally

From the repository root, run these commands in a Python 3.12 environment. The final command uses saved test responses, so it needs no NVIDIA key and writes no status files.

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q
python -m nimstatus.main --config tests/fixtures/healthy/models.yml --offline tests/fixtures/healthy --dry-run
```

Use separate Python environments for the monitor and the installable client. Both use the `nimstatus` import name.

## More information

- [Python and JavaScript integration guide](docs/USAGE.md)
- [Monitor deployment and branch protection](docs/SECURITY_SETUP.md)
- [Publishing the Python package](docs/PYPI.md)

[MIT license](LICENSE). This is an independent project.
