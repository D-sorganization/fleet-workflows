"""Hermetic tests for scripts/codex_review_request.py: no network, no real gh."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "codex_review_request.py"
_SPEC = importlib.util.spec_from_file_location("codex_review_request", _PATH)
assert _SPEC and _SPEC.loader
crr = importlib.util.module_from_spec(_SPEC)
sys.modules["codex_review_request"] = crr
_SPEC.loader.exec_module(crr)


def _view(**overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "number": 7,
        "state": "open",
        "draft": False,
        "author_is_bot": False,
        "additions": 10,
        "deletions": 5,
        "labels": frozenset(),
        "paths": ("src/app.py",),
        "comment_bodies": (),
    }
    fields.update(overrides)
    return crr.PullRequestView(**fields)


def _decide(view: Any, threshold: int = 200) -> Any:
    return crr.decide(view, threshold)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/actions/setup/action.yml",
        "scripts/automerge_guard.py",
        "src/auth/session.py",
        "docs/security/policy.md",
        "scripts/merge_hold_check.py",
        "src/secrets_loader.py",
        "CODEOWNERS",
    ],
)
def test_sensitive_path_requests_review(path: str) -> None:
    decision = _decide(_view(paths=(path,)))
    assert decision.request is True
    assert "sensitive path" in decision.reason


@pytest.mark.parametrize("label", ["security", "CI", "type:security", "hold"])
def test_sensitive_label_requests_review(label: str) -> None:
    decision = _decide(_view(labels=frozenset({label})))
    assert decision.request is True
    assert "sensitive label" in decision.reason


def test_large_change_requests_review_and_threshold_is_exclusive() -> None:
    assert _decide(_view(additions=150, deletions=51)).request is True
    assert _decide(_view(additions=150, deletions=50)).request is False


def test_small_ordinary_change_is_skipped() -> None:
    decision = _decide(_view())
    assert decision.request is False
    assert decision.reason == "not risky or large"


def test_drafts_and_closed_prs_are_skipped_even_when_risky() -> None:
    risky = {"paths": (".github/workflows/x.yml",), "additions": 900}
    assert _decide(_view(draft=True, **risky)).reason == "draft"
    assert _decide(_view(state="closed", **risky)).reason == "not open"


def test_bot_deletion_only_pr_is_skipped_but_human_deletion_is_not() -> None:
    gone = {"additions": 0, "deletions": 900, "paths": (".github/workflows/x.yml",)}
    assert _decide(_view(author_is_bot=True, **gone)).reason == (
        "bot-authored deletion-only"
    )
    assert _decide(_view(**gone)).request is True


@pytest.mark.parametrize(
    "body",
    [
        "@codex review",
        "  @Codex review please look at the guard",
        "<!-- codex-review-receipt:v1 -->\nCodex Review: no issues",
        "<!-- codex-pull-request-review-summary -->",
        crr.request_body(),
    ],
)
def test_existing_request_or_review_prevents_duplicate(body: str) -> None:
    view = _view(paths=(".github/workflows/x.yml",), comment_bodies=(body,))
    assert _decide(view).reason == "already requested or reviewed"


def test_security_review_request_alone_does_not_count_as_code_review() -> None:
    view = _view(paths=("CODEOWNERS",), comment_bodies=("@codex security review",))
    assert _decide(view).request is True


def test_threshold_precondition() -> None:
    with pytest.raises(ValueError):
        _decide(_view(), threshold=0)


def test_request_body_starts_with_trigger_and_carries_marker() -> None:
    body = crr.request_body()
    assert body.startswith("@codex review")
    assert crr.REQUEST_MARKER in body


def _completed(code: int, out: Any = "", err: str = "") -> Any:
    text = out if isinstance(out, str) else json.dumps(out)
    return subprocess.CompletedProcess(["gh"], code, stdout=text, stderr=err)


def _fake_gh(pr: dict[str, Any], files: list[Any], comments: list[Any]) -> Any:
    calls: list[tuple[list[str], str]] = []

    def run(cmd: Sequence[str], token: str) -> Any:
        calls.append((list(cmd), token))
        endpoint = cmd[2]
        if cmd[1] == "api" and "--method" in cmd:
            return _completed(0, {"id": 1})
        if endpoint.endswith("/files?per_page=100"):
            return _completed(0, [files])
        if endpoint.endswith("/comments?per_page=100"):
            return _completed(0, [comments])
        return _completed(0, pr)

    return run, calls


_PR = {
    "state": "open",
    "draft": False,
    "user": {"login": "d-sorgcodexagent[bot]", "type": "Bot"},
    "additions": 3,
    "deletions": 1,
    "labels": [{"name": "documentation"}],
}


def test_fetch_view_maps_rest_payloads() -> None:
    run, _ = _fake_gh(_PR, [{"filename": "a.py"}], [{"body": "hi"}])
    view = crr.fetch_view(run, "o/r", 7, "read-token")
    assert view.author_is_bot is True
    assert view.labels == frozenset({"documentation"})
    assert view.paths == ("a.py",)
    assert view.comment_bodies == ("hi",)


def test_run_posts_once_with_requester_token_only_when_requested() -> None:
    pr = {**_PR, "user": {"login": "dieterolson", "type": "User"}}
    run, calls = _fake_gh(pr, [{"filename": ".github/workflows/x.yml"}], [])
    result = crr.run_request(run, "o/r", 7, "read-token", "human-token", 200)
    assert result.posted is True
    posts = [c for c in calls if "--method" in c[0]]
    assert len(posts) == 1
    assert posts[0][1] == "human-token"
    assert "repos/o/r/issues/7/comments" in posts[0][0]


def test_run_without_requester_token_decides_but_never_posts() -> None:
    pr = {**_PR, "user": {"login": "dieterolson", "type": "User"}}
    run, calls = _fake_gh(pr, [{"filename": ".github/workflows/x.yml"}], [])
    result = crr.run_request(run, "o/r", 7, "read-token", "", 200)
    assert result.decision.request is True
    assert result.posted is False
    assert "no requester token" in result.note
    assert not [c for c in calls if "--method" in c[0]]


def test_failed_post_raises() -> None:
    def run(cmd: Sequence[str], token: str) -> Any:
        if "--method" in cmd:
            return _completed(1, err="denied")
        if cmd[2].endswith("/files?per_page=100"):
            return _completed(0, [[{"filename": "CODEOWNERS"}]])
        if cmd[2].endswith("/comments?per_page=100"):
            return _completed(0, [[]])
        return _completed(0, {**_PR, "user": {"login": "u", "type": "User"}})

    with pytest.raises(RuntimeError, match="denied"):
        crr.run_request(run, "o/r", 7, "read-token", "human-token", 200)
