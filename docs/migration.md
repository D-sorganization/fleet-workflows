# Conflict Disarm Migration

Owner decision and acceptance: [issue #1](https://github.com/D-sorganization/fleet-workflows/issues/1).

## Source

- Repository_Management revision: `bb9ffbe17c4ccc09671aaf9f4e5192c399691538`.
- `scripts/conflict_disarm.py` blob: `bd3659bd5e7fe9cf7201279be695aa0163851d09`, verified against GitHub default-branch contents.
- Decision and injected-runner tests ported from `tests/test_conflict_disarm.py`.
- Workflow lineage: RM #2205 / #2195, with the #2244 caller/callee concurrency namespace correction.
- Current callers: RD #2026 and Tools #5693; proposed AD caller: #5114.

The implementation's only runtime dependencies are Python's standard library
and the installed GitHub CLI. Independent review reproduced four inherited
defects, fixed here before release: multiline marker lookup, pagination beyond
one page, credential fallback for labeling/commenting, and false success when
the required mutations fail. Missing marker evidence now retains the hold, and
an incomplete disarm returns a nonzero status instead of reporting success.
The concurrent Cursor marker fix `d77a3cd` was preserved during integration.
The new
workflow removes the private RM checkout, pins its implementation checkout to
the defining workflow revision, and passes credentials through environment
variables instead of process arguments. Inputs enter shell scripts through
quoted environment variables. Dry-run rejects invalid values before the sweep.

## TDD and Validation

Before adding either workflow, the five new migration-contract tests failed
with missing workflow files. After adding the implementation and workflows,
`python -m pytest -q` passed all 20 initial tests. Independent review then added
seven failing runtime regressions before the corrective implementation. The
final suite passes all 28 tests, including parameterized failure cases. Ruff
lint and format checks passed.

Local actionlint 1.7.12 validates the workflows with only its two unsupported
`job.workflow_repository` / `job.workflow_sha` property diagnostics excluded.
GitHub documents both properties for reusable workflow source checkouts; the
CI dry-run smoke must qualify their actual execution before release. No other
actionlint diagnostics are excluded. No live disarm sweep was run locally.

## Rollout Gates

1. The permitted bootstrap commit `a0ec942` contains only README.md, LICENSE,
   and CODEOWNERS. The owner-approved temporary organization targeting exception
   was restored immediately after that push; `quality-gate` is required again.
2. This implementation must merge through the normal reviewed PR path after CI
   succeeds. No automerge guard is present in this repository; leave the PR for
   Dieter rather than merging manually.
3. Create `v1` only on the reviewed merged commit. Then update RD, Tools, and AD
   callers to `fleet-workflows/...@v1` in separate PRs, preserving dry-run.
4. Retire the old RM canonical copies and migrate its other consumers in an RM
   follow-up without breaking their references. No old copy is deleted by this
   provider PR.
5. Re-enable disabled workflow state only after the caller lands and Dieter
   approves the exact settings change. The migration does not authorize a live
   disarm sweep or changes to unrelated PR state.
