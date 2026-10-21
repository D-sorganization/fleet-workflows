"""Hermetic tests for scripts/conflict_disarm.py — no network, no real gh."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "conflict_disarm.py"
_SPEC = importlib.util.spec_from_file_location("conflict_disarm", _MODULE_PATH)
assert _SPEC and _SPEC.loader
conflict_disarm = importlib.util.module_from_spec(_SPEC)
sys.modules["conflict_disarm"] = conflict_disarm
_SPEC.loader.exec_module(conflict_disarm)


def _pr(
    *,
    number: int = 1,
    draft: bool = False,
    state: str = "open",
    mergeable: str = "MERGEABLE",
    mergeable_state: str = "clean",
    armed: bool = False,
    labels: tuple[str, ...] = (),
    head_sha: str = "abc123",
    commented_sha: str = "",
) -> conflict_disarm.PullRequestView:
    return conflict_disarm.PullRequestView(
        number=number,
        draft=draft,
        state=state,
        mergeable=mergeable,
        mergeable_state=mergeable_state,
        auto_merge_armed=armed,
        labels=frozenset(labels),
        head_sha=head_sha,
        last_marker_comment_sha=commented_sha,
    )


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (_pr(mergeable="CONFLICTING", armed=True), conflict_disarm.Action.DISARM),
        (_pr(mergeable_state="dirty", armed=True), conflict_disarm.Action.DISARM),
        (_pr(mergeable="UNKNOWN"), conflict_disarm.Action.RETRY_LATER),
        (
            _pr(
                mergeable="MERGEABLE",
                labels=("conflict:disarmed",),
                head_sha="newsha",
                commented_sha="oldsha",
            ),
            conflict_disarm.Action.CLEAR_LABEL,
        ),
        (
            _pr(
                mergeable="MERGEABLE",
                labels=("conflict:disarmed",),
                head_sha="same",
                commented_sha="same",
            ),
            conflict_disarm.Action.NOOP,
        ),
        (
            _pr(draft=True, mergeable="CONFLICTING", armed=True),
            conflict_disarm.Action.NOOP,
        ),
        (_pr(state="closed"), conflict_disarm.Action.NOOP),
        (_pr(mergeable="MERGEABLE"), conflict_disarm.Action.NOOP),
    ],
)
def test_decide_table(
    view: conflict_disarm.PullRequestView, expected: conflict_disarm.Action
) -> None:
    assert conflict_disarm.decide(view) == expected


def test_decide_conflicting_unarmed_still_disarms_for_label() -> None:
    """Conflicting without auto-merge still needs the hold label (no revoke)."""
    view = _pr(mergeable="CONFLICTING", mergeable_state="dirty", armed=False)
    assert conflict_disarm.decide(view) == conflict_disarm.Action.DISARM


class _RecordingRunner:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._responses = responses or {}
        self._disable_failures = 2

    def __call__(self, cmd: list[str]) -> Any:
        self.calls.append(list(cmd))
        argv = cmd[1:] if cmd and cmd[0] == "gh" else cmd
        key = " ".join(cmd)
        if key in self._responses:
            return self._responses[key]
        if argv[0:2] == ["pr", "merge"] and "--disable-auto" in argv:
            if self._disable_failures > 0:
                self._disable_failures -= 1
                return _completed(1, stderr="refused")
            return _completed(0)
        if argv[0:2] == ["pr", "ready"]:
            return _completed(0)
        return _completed(0, stdout="{}")


def _completed(
    code: int, stdout: str = "", stderr: str = ""
) -> conflict_disarm.subprocess.CompletedProcess[str]:
    return conflict_disarm.subprocess.CompletedProcess(
        args=[], returncode=code, stdout=stdout, stderr=stderr
    )


def test_apply_disarm_tries_app_token_then_github_token() -> None:
    runner = _RecordingRunner()
    runner._disable_failures = 1
    view = _pr(mergeable="CONFLICTING", armed=True, head_sha="deadbeef")
    result = conflict_disarm.apply_action(
        "o/r",
        view,
        conflict_disarm.Action.DISARM,
        runner=runner,
        app_token="ghs_app",
        github_token="ghs_actions",
        dry_run=False,
    )
    assert result.revoke_attempts == 2
    disable_cmds = [c for c in runner.calls if len(c) > 2 and c[1:3] == ["pr", "merge"]]
    assert len(disable_cmds) == 2
    assert disable_cmds[0][-1] == "--disable-auto"
    assert result.drafted is False


def test_apply_disarm_converts_to_draft_when_both_revokes_refused() -> None:
    runner = _RecordingRunner()
    view = _pr(mergeable="CONFLICTING", armed=True)
    result = conflict_disarm.apply_action(
        "o/r",
        view,
        conflict_disarm.Action.DISARM,
        runner=runner,
        app_token="ghs_app",
        github_token="ghs_actions",
        dry_run=False,
    )
    assert result.drafted is True
    assert any(
        len(c) > 3 and c[1:3] == ["pr", "ready"] and "--undo" in c for c in runner.calls
    )


def test_apply_disarm_skips_revoke_when_not_armed() -> None:
    runner = _RecordingRunner()
    view = _pr(mergeable="CONFLICTING", armed=False)
    conflict_disarm.apply_action(
        "o/r",
        view,
        conflict_disarm.Action.DISARM,
        runner=runner,
        app_token="ghs_app",
        github_token="ghs_actions",
        dry_run=False,
    )
    assert not any(len(c) > 2 and c[1:3] == ["pr", "merge"] for c in runner.calls)


def test_apply_never_arms_merges_or_pushes() -> None:
    runner = _RecordingRunner()
    view = _pr(mergeable="CONFLICTING", armed=True)
    conflict_disarm.apply_action(
        "o/r",
        view,
        conflict_disarm.Action.DISARM,
        runner=runner,
        app_token="ghs_app",
        github_token="ghs_actions",
        dry_run=False,
    )
    joined = " ".join(" ".join(c) for c in runner.calls)
    assert "--auto" not in joined
    assert "merge" not in joined or "--disable-auto" in joined
    assert "push" not in joined


def test_exclude_prs_skipped_in_sweep() -> None:
    raw_prs = [
        {"number": 12634, "auto_merge": {}, "labels": []},
        {"number": 99, "auto_merge": {}, "labels": []},
    ]
    nums = conflict_disarm.select_pr_numbers(
        raw_prs, exclude={12634}, label=conflict_disarm.CONFLICT_DISARMED_LABEL
    )
    assert nums == [99]


def test_dry_run_does_not_mutate() -> None:
    runner = _RecordingRunner()
    view = _pr(mergeable="CONFLICTING", armed=True)
    result = conflict_disarm.apply_action(
        "o/r",
        view,
        conflict_disarm.Action.DISARM,
        runner=runner,
        app_token="ghs_app",
        github_token="ghs_actions",
        dry_run=True,
    )
    assert result.dry_run is True
    assert runner.calls == []
