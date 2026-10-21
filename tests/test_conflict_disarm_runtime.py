"""Runtime regressions for paginated reads and fail-closed credential fallback."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any

import pytest
from test_conflict_disarm import _completed, _pr, conflict_disarm


def test_multiline_marker_lookup_preserves_latest_body_and_same_head_hold() -> None:
    old_sha, current_sha = "a" * 40, "b" * 40
    bodies = [
        conflict_disarm._comment_body(old_sha),
        conflict_disarm._comment_body(current_sha),
        "<!-- conflict-disarm -->\nMalformed marker without a head SHA",
    ]

    def run(cmd: Sequence[str]) -> Any:
        if "--slurp" in cmd:
            return _completed(0, json.dumps([{"body": body} for body in bodies]))
        return _completed(0, "\n".join(bodies))

    marker = conflict_disarm._latest_marker_comment_sha(run, "o/r", 1)
    assert marker == current_sha
    view = _pr(
        labels=("conflict:disarmed",), head_sha=current_sha, commented_sha=marker
    )
    assert conflict_disarm.decide(view) == conflict_disarm.Action.NOOP


def test_missing_marker_does_not_prove_a_new_head_for_clearing_the_hold() -> None:
    view = _pr(labels=("conflict:disarmed",), head_sha="b" * 40)
    assert conflict_disarm.decide(view) == conflict_disarm.Action.NOOP


def test_open_pull_fetch_flattens_all_cli_pages() -> None:
    pages = [[{"number": 1}], [{"number": 101}]]

    def run(cmd: Sequence[str]) -> Any:
        assert "--paginate" in cmd
        if "--slurp" in cmd and "--jq" in cmd:
            return _completed(0, json.dumps([item for page in pages for item in page]))
        return _completed(0, "\n".join(json.dumps(page) for page in pages))

    assert conflict_disarm._fetch_open_pulls(run, "o/r") == [
        {"number": 1},
        {"number": 101},
    ]


class MutationRunner:
    """A real CLI-shaped seam with configurable credential permissions."""

    def __init__(
        self,
        *,
        deny_revoke: bool = False,
        deny_label: bool = False,
        deny_comment: bool = False,
    ) -> None:
        self.deny_revoke = deny_revoke
        self.deny_label = deny_label
        self.deny_comment = deny_comment
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, cmd: Sequence[str]) -> Any:
        self.calls.append((list(cmd), os.environ.get("GH_TOKEN", "")))
        if cmd[1] == "api":
            if "comments?" in cmd[2]:
                return _completed(0, "[]" if "--slurp" in cmd else "")
            raw = {
                "number": 1,
                "state": "open",
                "mergeable": False,
                "mergeable_state": "dirty",
                "auto_merge": {},
                "head": {"sha": "b" * 40},
            }
            return _completed(0, json.dumps([raw] if "pulls?" in cmd[2] else raw))
        if self.deny_revoke and ("--disable-auto" in cmd or "--undo" in cmd):
            return _completed(1, stderr="refused")
        if self.deny_label and "--add-label" in cmd:
            return _completed(1, stderr="refused")
        if self.deny_comment and cmd[1:3] == ["pr", "comment"]:
            return _completed(1, stderr="refused")
        if os.environ.get("GH_TOKEN") == "app-placeholder":
            return _completed(1, stderr="refused")
        return _completed(0)


def test_successful_fallback_credential_is_used_for_the_required_writes() -> None:
    runner = MutationRunner()
    result = conflict_disarm.apply_action(
        "o/r",
        _pr(mergeable="CONFLICTING", armed=True),
        conflict_disarm.Action.DISARM,
        runner=runner,
        app_token="app-placeholder",
        github_token="fallback-placeholder",
    )
    assert result.revoked and result.labeled and result.commented
    writes = [
        (cmd, token) for cmd, token in runner.calls if "--disable-auto" not in cmd
    ]
    assert all(token == "fallback-placeholder" for _, token in writes)


def test_label_clear_uses_the_fallback_when_app_credentials_are_refused() -> None:
    result = conflict_disarm.apply_action(
        "o/r",
        _pr(labels=("conflict:disarmed",)),
        conflict_disarm.Action.CLEAR_LABEL,
        runner=MutationRunner(),
        app_token="app-placeholder",
        github_token="fallback-placeholder",
    )
    assert result.cleared


@pytest.mark.parametrize("failure", ["revoke", "label", "comment"])
def test_cli_fails_for_an_incomplete_disarm(
    failure: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = MutationRunner(
        deny_revoke=failure == "revoke",
        deny_label=failure == "label",
        deny_comment=failure == "comment",
    )
    monkeypatch.setattr(conflict_disarm, "_default_runner", runner)
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-placeholder")
    monkeypatch.delenv("CONFLICT_DISARM_APP_TOKEN", raising=False)

    assert conflict_disarm.main(["--repo", "o/r"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["disarmed"] == 0
    assert payload["errors"] == 1
    if failure == "revoke":
        assert not any(cmd[1:3] == ["pr", "comment"] for cmd, _ in runner.calls)
