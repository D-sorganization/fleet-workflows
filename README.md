# Fleet Workflows

Public reusable GitHub Actions workflows for D-sorganization.

Conflict Disarm's Python implementation and reusable workflow live together here.
Track validation, release, and caller rollout in
[migration issue #1](https://github.com/D-sorganization/fleet-workflows/issues/1).

Implementation changes arrive through reviewed pull requests. Consumers will use
the `v1` release after the implementation merges. Dry-run remains the default;
the migration does not authorize live PR disarm sweeps.

Licensed under the MIT License. Repository owner: @dieterolson.

## Calling Conflict Disarm

After the reviewed implementation merges and `v1` is published:

```yaml
permissions:
  contents: write
  pull-requests: write

jobs:
  disarm:
    uses: D-sorganization/fleet-workflows/.github/workflows/reusable-conflict-disarm.yml@v1
    with:
      dry_run: "true"
      exclude_prs: ""
    secrets:
      JULES_APP_ID: ${{ secrets.JULES_APP_ID }}
      JULES_APP_PRIVATE_KEY: ${{ secrets.JULES_APP_PRIVATE_KEY }}
```

App credentials are optional; omit the `secrets` block to use the caller's
`GITHUB_TOKEN`. No credentials are stored in the checkout or passed as command
arguments. The default runner is `d-sorg-fleet`; public CI can explicitly select
`runner: ubuntu-latest`. Keep dry-run enabled until the owner approves live sweeps.
Keep the caller's concurrency group distinct from `conflict-disarm-sweep-*`.

The reusable workflow checks out its own source at `job.workflow_sha`, using
[GitHub's documented workflow identity context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#example-usage-of-job-context-workflow-identity).
It never checks out the caller's code or a private sibling repository.

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

CI runs these checks and an actual reusable-workflow dry-run on PRs, merge groups,
and main pushes. Fork PRs run hermetic tests on hosted runners and skip the
write-scoped smoke job. `quality-gate` verifies both results.

## Provenance and Release

The implementation and decision tests originated in Repository_Management
(`scripts/conflict_disarm.py`, `tests/test_conflict_disarm.py`), licensed MIT.
The workflow preserves its dry-run contract, credential fallback, exclusion
inputs, and the later concurrency namespace fix. See
[the migration record](docs/migration.md) for source revision and validation.

The first `v1` tag must point to the reviewed, merged implementation. Never tag a
PR branch or move the release to unreviewed code. RD, Tools, and AD switch callers
only after that tag exists; workflow re-enablement follows merged caller PRs.
