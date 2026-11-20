# Release nimstatus to PyPI

The public package is built from `clients/python`. Use `from nimstatus import ModelRouter`. The package also supports the previous `nimstatus_client` import.

In this repository, `main` requires a pull request and the **Offline tests** check. Only the repository owner can create `v*` tags under the current ruleset. These tags cannot be moved or deleted under the active rules.

1. Merge the release workflow and package changes into `main`.
2. In [GitHub repository environments](https://github.com/Jaykay73/free-models/settings/environments), create the `pypi` environment. Under deployment branches and tags, select `main` as a branch and `v*` as a tag pattern. Add a required reviewer if you have a second maintainer who can approve releases.
3. Sign in to PyPI and open [Publishing](https://pypi.org/manage/account/publishing/). Under GitHub, add a pending publisher with these values. If you already own the package, add the same publisher under the package's Publishing settings. [PyPI setup instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)

   | Field | Value |
   | --- | --- |
   | PyPI project name | `nimstatus` |
   | Owner | `Jaykay73` |
   | Repository name | `free-models` |
   | Workflow name | `release-pypi.yml` |
   | Environment name | `pypi` |

4. From an authenticated GitHub CLI, publish the first GitHub release once the reviewed changes are on `main`.

   ```powershell
   gh release create v0.1.0 --repo Jaykay73/free-models --target main --title "nimstatus 0.1.0" --notes "First public release of the nimstatus Python client."
   ```

   If the GitHub release already exists when you finish PyPI setup, rerun its failed workflow or dispatch a fresh run from `main`.

   ```powershell
   gh workflow run release-pypi.yml --repo Jaykay73/free-models --ref main -f tag=v0.1.0
   ```

5. After the [workflow](https://github.com/Jaykay73/free-models/actions/workflows/release-pypi.yml) succeeds, check the [PyPI project](https://pypi.org/project/nimstatus/) and try this in a clean Python environment.

   ```powershell
   python -m pip install nimstatus
   python -c "from nimstatus import ModelRouter; print(ModelRouter().resolve('fast'))"
   ```

For later releases, bump `version` in `clients/python/pyproject.toml` and `__version__` in `clients/python/nimstatus_client.py`, merge to `main`, and publish a matching `vX.Y.Z` tag. The workflow uses the tagged source, checks that it belongs to `main`, and installs the wheel before passing it to PyPI through Trusted Publishing.
