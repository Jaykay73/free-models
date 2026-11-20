# Apply the security update

The branch protection JSON files document the expected settings for this repository. Use the read commands below to verify the live settings before reapplying them.

## 1. Push the update and verify CI

From this repository, using your own authenticated Git client:

```bash
git switch -c security/harden-workflows
git add .github .gitignore requirements.txt requirements.lock pyproject.toml src/nimstatus/main.py scripts tests README.md docs clients
git commit -m "Harden dependency installation and isolate status publishing"
git push -u origin security/harden-workflows
gh pr create --base main --head security/harden-workflows --title "Harden workflows and dependencies" --body "Pin verified dependencies and actions, isolate publishing, and add security boundary tests."
```

Check the **CI → Offline tests** result and merge the pull request. CI has read-only permissions and no NVIDIA secret. The scheduled probe still runs only on `main`, via schedule or manual dispatch. Never run unreviewed code with production secrets.

## 2. Apply branch protection

Run these commands locally after `gh auth login` if needed. They replace classic protection settings for these two branches; review existing settings first if someone has added protections since the audit.

```bash
gh api --method PUT repos/Jaykay73/free-models/branches/main/protection --input .github/protection/main.json
gh api --method PUT repos/Jaykay73/free-models/branches/data/protection --input .github/protection/data.json
gh api repos/Jaykay73/free-models/branches/main/protection
gh api repos/Jaykay73/free-models/branches/data/protection
```

`main` requires pull requests, an up-to-date passing **Offline tests** check from the GitHub Actions app (ID 15368), resolved conversations and linear history. Force pushes and deletion are blocked, including for administrators. No bot bypass is configured.

For a single-maintainer repository the prepared policy requires **zero independent approving reviews**, because an author cannot approve their own PR. It still requires the PR and CI. After adding a trusted second maintainer, change `required_approving_review_count` to `1`, enable `require_code_owner_reviews`, update CODEOWNERS to include that person, and reapply. CODEOWNERS alone does not enforce independent review.

`data` blocks force pushes and deletion, including for administrators, while allowing ordinary fast-forward workflow commits. It deliberately does not require pull requests or CI on machine-generated state. Repository writers can still modify this branch; audit collaborators. Restricting it to a dedicated publisher identity would require an additional GitHub App/deploy identity and is not provided by this single-token setup.

If the API returns 401, authenticate again; if 403, use a repository-admin account with an appropriate token. You can apply the same policies under Settings → Branches. Do not paste GitHub or NVIDIA tokens into chat, source files or commands.

## 3. Validate the deployed update

Manually run **Probe NVIDIA models** on `main`. Confirm both `probe` and `publish` jobs succeed, followed by **Refresh branch-source Pages**. Verify:

```bash
curl -fsS https://jaykay73.github.io/free-models/status.json
```

If publishing reports that the data revision changed, rerun the probe. The publisher rejects stale artifacts rather than overwriting a newer state snapshot. Artifact uploads expire after one day and contain only the two public JSON files and the original data commit ID.

## Security boundaries

- `setuptools` is pinned to 80.9.0 and `pytest` to 9.0.3, beyond the fixes identified in the review. PyYAML is pinned to 6.0.3.
- `requirements.lock` pins direct, transitive and build dependencies with SHA-256 wheel hashes from PyPI release metadata. CI uses `--require-hashes --only-binary=:all:`; no source distribution or editable package build runs in CI.
- Actions use full commit SHAs resolved from their official repositories. Dependabot proposes weekly action and Python dependency updates; review them before merging.
- The probe job has only `contents: read` and checkout credentials are not persisted. Only its probe step receives `NVIDIA_API_KEY`. `NIMSTATUS_ALLOWED_ORIGIN` restricts that key to the NVIDIA origin even if `models.yml` changes.
- The separate publisher has `contents: write`, installs no dependencies, receives no NVIDIA secret, and runs a stdlib-only validator from the workflow's source commit using Python isolated mode. It validates names, schemas, sizes, credential-like values, and the expected data revision. Extra files and symlinks are rejected.
- Only the publisher's `data` checkout retains a write credential for committing. Protection of `main` is necessary to constrain that credential's impact.
- Validation prevents execution of artifact contents. It cannot establish that a compromised probe produced truthful health measurements; the probe and its reviewed dependencies remain trusted for measurement integrity.
- Hosted runner images, Python distribution, GitHub's artifact service and the reviewed action commits remain trusted infrastructure. SHA/hash pins improve reproducibility; they are not an audit of every dependency.

When updating dependencies, regenerate and review the complete hash lock in an isolated Python 3.12 environment, keep the setuptools build pin aligned with `pyproject.toml`, and run the full suite. A version-only Dependabot PR may need a matching lock update before it passes CI. The workflow intentionally does not use a package cache.

Local installation matching the workflow:

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q
```

The Python client has a three-second directory fetch deadline. The public directory expires after 12 hours and refreshes on the next successful probe once its two-hour heartbeat is due. Streamed probe response size limits remain follow-up work.
