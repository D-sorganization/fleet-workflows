#!/usr/bin/env python3
"""Request one Codex review on a risky or large pull request.

Codex review runs request-only across the fleet, so this posts ``@codex review``
once per pull request, and only when the PR is open, not a draft, not a
bot-authored deletion-only PR, has no earlier request or Codex review, and
either touches a security-sensitive path or label or changes more than
``--threshold`` lines.

Codex ignores ``@codex review`` from bot accounts. It replies "create a Codex
account" (Repository_Management#2329 and Runner_Dashboard#2196 on 2026-10-22).
So the request is posted with ``CODEX_REVIEW_REQUEST_TOKEN``, which must belong
to a Codex-linked human account. Without it the script only reports its
decision. Reads use ``GH_TOKEN``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

REQUEST_MARKER = "<!-- fleet-codex-review-request:v1 -->"
CODEX_MARKERS = (
    "<!-- codex-review-receipt:v1 -->",
    "<!-- codex-pull-request-review-summary -->",
    REQUEST_MARKER,
)
_TRIGGER = re.compile(r"^\s*@codex\s+review\b", re.IGNORECASE)
_SENSITIVE_PATH = re.compile(
    r"(^|/)\.github/(workflows|actions)/|(^|/)CODEOWNERS$"
    r"|(^|[/_.-])(auth|security|secrets?|credentials?|tokens?|guards?|holds?"
    r"|permissions?)(?=[/_.-]|$)",
    re.IGNORECASE,
)
_SENSITIVE_LABELS = frozenset(
    {"ci", "auth", "hold", "merge-hold", "do-not-merge", "guard", "type:ci"}
)

# (cmd, token) -> process; the token is passed explicitly so reads and the
# single write never share a credential by accident.
CommandRunner = Callable[[Sequence[str], str], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class PullRequestView:
    number: int
    state: str
    draft: bool
    author_is_bot: bool
    additions: int
    deletions: int
    labels: frozenset[str]
    paths: tuple[str, ...]
    comment_bodies: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    request: bool
    reason: str


@dataclass(frozen=True)
class RunResult:
    decision: Decision
    posted: bool
    note: str


def request_body() -> str:
    return (
        "@codex review\n\n"
        f"{REQUEST_MARKER}\n"
        "Requested once by the fleet Codex Review Request workflow because this "
        "pull request is security-sensitive or large."
    )


def _already_requested(bodies: Sequence[str]) -> bool:
    return any(
        _TRIGGER.match(body) or any(marker in body for marker in CODEX_MARKERS)
        for body in bodies
    )


def _sensitive_label(labels: frozenset[str]) -> str:
    for label in sorted(labels):
        lowered = label.lower()
        if lowered in _SENSITIVE_LABELS or "security" in lowered:
            return label
    return ""


def _sensitive_path(paths: Sequence[str]) -> str:
    return next((path for path in paths if _SENSITIVE_PATH.search(path)), "")


def decide(view: PullRequestView, threshold: int) -> Decision:
    """Return whether to request a review.

    Precondition: ``threshold`` is positive.
    Postcondition: ``request`` is true only for an open, non-draft PR with no
    earlier request or review that is sensitive or exceeds ``threshold``.
    """
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if view.state != "open":
        return Decision(False, "not open")
    if view.draft:
        return Decision(False, "draft")
    if view.author_is_bot and view.additions == 0:
        return Decision(False, "bot-authored deletion-only")
    if _already_requested(view.comment_bodies):
        return Decision(False, "already requested or reviewed")
    if label := _sensitive_label(view.labels):
        return Decision(True, f"sensitive label: {label}")
    if path := _sensitive_path(view.paths):
        return Decision(True, f"sensitive path: {path}")
    changed = view.additions + view.deletions
    if changed > threshold:
        return Decision(True, f"large change: {changed} lines > {threshold}")
    return Decision(False, "not risky or large")


def _default_runner(cmd: Sequence[str], token: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GH_TOKEN": token}
    exe = shutil.which(cmd[0]) or cmd[0]
    return subprocess.run(
        [exe, *cmd[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )


def _gh_json(run: CommandRunner, token: str, args: Sequence[str]) -> Any:
    proc = run(["gh", *args], token)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh failed")
    return json.loads(proc.stdout or "null")


def _gh_items(run: CommandRunner, token: str, endpoint: str) -> list[dict[str, Any]]:
    pages = _gh_json(run, token, ["api", endpoint, "--paginate", "--slurp"])
    return [
        item
        for page in pages or []
        if isinstance(page, list)
        for item in page
        if isinstance(item, dict)
    ]


def fetch_view(run: CommandRunner, repo: str, pr: int, token: str) -> PullRequestView:
    base = f"repos/{repo}"
    raw = _gh_json(run, token, ["api", f"{base}/pulls/{pr}"])
    files = _gh_items(run, token, f"{base}/pulls/{pr}/files?per_page=100")
    comments = _gh_items(run, token, f"{base}/issues/{pr}/comments?per_page=100")
    user = raw.get("user") or {}
    return PullRequestView(
        number=pr,
        state=str(raw.get("state", "")),
        draft=bool(raw.get("draft")),
        author_is_bot=user.get("type") == "Bot"
        or str(user.get("login", "")).endswith("[bot]"),
        additions=int(raw.get("additions") or 0),
        deletions=int(raw.get("deletions") or 0),
        labels=frozenset(str(label.get("name", "")) for label in raw.get("labels", [])),
        paths=tuple(str(item.get("filename", "")) for item in files),
        comment_bodies=tuple(str(item.get("body") or "") for item in comments),
    )


def run_request(
    run: CommandRunner,
    repo: str,
    pr: int,
    read_token: str,
    requester_token: str,
    threshold: int,
) -> RunResult:
    """Decide and, when warranted and possible, post exactly one request."""
    decision = decide(fetch_view(run, repo, pr, read_token), threshold)
    if not decision.request:
        return RunResult(decision, False, "nothing to do")
    if not requester_token:
        return RunResult(
            decision, False, "no requester token; decision recorded, nothing posted"
        )
    _gh_json(
        run,
        requester_token,
        [
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues/{pr}/comments",
            "-f",
            f"body={request_body()}",
        ],
    )
    return RunResult(decision, True, "posted @codex review")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--threshold", type=int, default=200)
    args = parser.parse_args(argv)
    result = run_request(
        _default_runner,
        args.repo,
        args.pr,
        os.environ.get("GH_TOKEN", ""),
        os.environ.get("CODEX_REVIEW_REQUEST_TOKEN", ""),
        args.threshold,
    )
    print(
        json.dumps(
            {
                "pr": args.pr,
                "request": result.decision.request,
                "reason": result.decision.reason,
                "posted": result.posted,
                "note": result.note,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
