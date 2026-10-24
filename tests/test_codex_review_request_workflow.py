"""Contracts for the reusable Codex Review Request workflow."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAME = "reusable-codex-review-request.yml"


def _workflow() -> dict[str, Any]:
    path = ROOT / ".github" / "workflows" / NAME
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job() -> dict[str, Any]:
    return _workflow()["jobs"]["request"]


def test_is_call_only_with_optional_requester_secret_and_defaults() -> None:
    document = _workflow()
    events = document.get("on") or document[True]
    assert set(events) == {"workflow_call"}
    call = events["workflow_call"]
    assert call["inputs"]["threshold"]["default"] == 200
    assert call["inputs"]["runner"]["default"] == "d-sorg-fleet"
    assert call["secrets"]["CODEX_REVIEW_REQUEST_TOKEN"]["required"] is False


def test_github_token_is_read_only_and_runs_serialize_per_pr() -> None:
    document = _workflow()
    assert set(document["permissions"].values()) == {"read"}
    assert document["concurrency"]["cancel-in-progress"] is False
    assert "pull_request.number" in document["concurrency"]["group"]


def test_job_skips_drafts_forks_and_non_pull_request_events() -> None:
    condition = _job()["if"]
    assert "github.event_name == 'pull_request'" in condition
    assert "github.event.pull_request.draft == false" in condition
    assert "head.repo.full_name == github.repository" in condition


def test_job_timeout_is_caller_tunable_with_a_short_default() -> None:
    """Fleet workflow lint requires callers to state timeout-minutes; a
    `uses:` job cannot set it, so the reusable workflow accepts it."""
    events = _workflow().get("on") or _workflow()[True]
    timeout = events["workflow_call"]["inputs"]["timeout-minutes"]
    assert timeout["type"] == "number"
    assert 0 < timeout["default"] <= 5
    assert _job()["timeout-minutes"] == "${{ inputs.timeout-minutes }}"


def test_checks_out_its_own_immutable_revision_without_credentials() -> None:
    checkout = next(
        step for step in _job()["steps"] if "actions/checkout@" in step.get("uses", "")
    )
    assert checkout["with"]["repository"] == "${{ job.workflow_repository }}"
    assert checkout["with"]["ref"] == "${{ job.workflow_sha }}"
    assert checkout["with"]["persist-credentials"] is False


def test_request_step_passes_credentials_and_inputs_through_env_only() -> None:
    step = next(step for step in _job()["steps"] if step.get("id") == "request")
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert (
        step["env"]["CODEX_REVIEW_REQUEST_TOKEN"]
        == "${{ secrets.CODEX_REVIEW_REQUEST_TOKEN }}"
    )
    assert "${{" not in step["run"]
    assert "set -euo pipefail" in step["run"]
    assert "scripts/codex_review_request.py" in step["run"]
