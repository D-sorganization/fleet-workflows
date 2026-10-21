"""Migration contracts: one implementation, immutable checkout, safe defaults."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    )


def triggers(document: dict[str, Any]) -> dict[str, Any]:
    return document.get("on") or document[True]


def test_reusable_checks_out_its_own_immutable_revision_without_credentials() -> None:
    document = workflow("reusable-conflict-disarm.yml")
    steps = document["jobs"]["conflict-disarm"]["steps"]
    checkout = next(
        step for step in steps if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["repository"] == "${{ job.workflow_repository }}"
    assert checkout["with"]["ref"] == "${{ job.workflow_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert "token" not in checkout["with"]
    assert "Repository_Management" not in str(document)


def test_reusable_preserves_inputs_permissions_and_safe_defaults() -> None:
    document = workflow("reusable-conflict-disarm.yml")
    call = triggers(document)["workflow_call"]
    assert call["inputs"]["dry_run"]["default"] == "true"
    assert call["inputs"]["exclude_prs"]["default"] == ""
    assert call["inputs"]["repo_name"]["default"] == ""
    assert document["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert all(not secret["required"] for secret in call["secrets"].values())
    assert document["concurrency"]["cancel-in-progress"] is False
    assert document["concurrency"]["group"].startswith("conflict-disarm-sweep-")
    job = document["jobs"]["conflict-disarm"]
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository"
        in job["if"]
    )
    assert job["timeout-minutes"] == 15


def test_sweep_uses_environment_credentials_and_quoted_inputs() -> None:
    steps = workflow("reusable-conflict-disarm.yml")["jobs"]["conflict-disarm"]["steps"]
    sweep = next(step for step in steps if step.get("id") == "sweep")
    assert sweep["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert (
        sweep["env"]["CONFLICT_DISARM_APP_TOKEN"]
        == "${{ steps.app-token.outputs.token }}"
    )
    assert "--app-token" not in sweep["run"]
    assert "--github-token" not in sweep["run"]
    assert "${{" not in sweep["run"]
    assert '--repo "$REPO_SLUG"' in sweep["run"]
    assert '--exclude-prs "$EXCLUDE_PRS"' in sweep["run"]
    assert "set -euo pipefail" in sweep["run"]


def test_ci_checks_prs_and_merge_groups_and_exercises_reusable_in_dry_run() -> None:
    document = workflow("ci.yml")
    events = triggers(document)
    assert "pull_request" in events
    assert "merge_group" in events
    assert events["push"]["branches"] == ["main"]
    smoke = document["jobs"]["dry-run-smoke"]
    assert smoke["uses"] == "./.github/workflows/reusable-conflict-disarm.yml"
    assert smoke["with"]["dry_run"] == "${{ 'true' }}"
    assert smoke["with"]["repo_name"] == "${{ github.repository }}"
    assert smoke["with"]["runner"] == "ubuntu-latest"
    assert document["jobs"]["quality-gate"]["if"] == "always()"
    assert set(document["jobs"]["quality-gate"]["needs"]) == {"test", "dry-run-smoke"}


def test_external_actions_are_sha_pinned() -> None:
    import re

    for name in ("ci.yml", "reusable-conflict-disarm.yml"):
        for job in workflow(name)["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
