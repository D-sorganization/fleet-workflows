"""Disarm auto-merge on conflicting pull requests (fleet Rule 3, #2195).

Pure :func:`decide` encodes when to disarm, clear the ``conflict:disarmed``
label, wait for GitHub mergeability, or do nothing. :func:`apply_action` performs
GraphQL/REST mutations through an injected ``CommandRunner`` (same seam as
``automerge_guard.py``).

DbC
---
Pre (apply): PR is open; caller fetched mergeability in the same run; token has
``pull-requests: write`` and ``contents: write`` when not in dry-run.

Post (DISARM): auto-merge is off **or** the PR is draft; ``conflict:disarmed``
is present; at most one marker comment per head SHA.

Invariants: never merges, never arms, never pushes, never closes; never acts on
UNKNOWN mergeability (``RETRY_LATER``). Disarm is fail-closed (draft); read
errors are fail-open (skip + warning).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

CONFLICT_DISARMED_LABEL = "conflict:disarmed"
COMMENT_MARKER = "<!-- conflict-disarm -->"
LABEL_COLOR = "B60205"
LABEL_DESCRIPTION = (
    "Auto-merge disabled because the PR conflicts with its base; "
    "rebase, then re-arm via automerge_guard"
)

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class Action(str, Enum):  # noqa: UP042
    DISARM = "DISARM"
    CLEAR_LABEL = "CLEAR_LABEL"
    NOOP = "NOOP"
    RETRY_LATER = "RETRY_LATER"


@dataclass(frozen=True)
class PullRequestView:
    """Minimal PR slice for :func:`decide` — no I/O."""

    number: int
    draft: bool
    state: str
    mergeable: str
    mergeable_state: str
    auto_merge_armed: bool
    labels: frozenset[str]
    head_sha: str
    last_marker_comment_sha: str = ""


@dataclass(frozen=True)
class ApplyResult:
    action: Action
    dry_run: bool
    revoked: bool = False
    revoke_attempts: int = 0
    labeled: bool = False
    cleared: bool = False
    commented: bool = False
    drafted: bool = False
    skipped: bool = False


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    exe = shutil.which(cmd[0]) or cmd[0]
    return subprocess.run(
        [exe, *cmd[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=os.environ.copy(),
    )


def _label_present(labels: frozenset[str]) -> bool:
    lowered = {name.lower() for name in labels}
    return CONFLICT_DISARMED_LABEL.lower() in lowered


def _is_unknown(view: PullRequestView) -> bool:
    if view.mergeable == "UNKNOWN":
        return True
    if view.mergeable_state == "unknown":
        return True
    return False


def _is_conflicting(view: PullRequestView) -> bool:
    if view.mergeable == "CONFLICTING":
        return True
    return view.mergeable_state.lower() == "dirty"


def decide(view: PullRequestView) -> Action:
    """Return the next action for ``view`` without side effects.

    Pre: ``view`` was built from a single consistent API snapshot.
    Post: result is one of :class:`Action` values above.
    """
    if view.state != "open" or view.draft:
        return Action.NOOP
    if _is_unknown(view):
        return Action.RETRY_LATER
    if _label_present(view.labels) and view.mergeable == "MERGEABLE":
        if view.head_sha and view.head_sha != view.last_marker_comment_sha:
            return Action.CLEAR_LABEL
        return Action.NOOP
    if _is_conflicting(view):
        return Action.DISARM
    return Action.NOOP


def _gh(
    runner: CommandRunner, token: str, args: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    """Run ``gh`` with ``token`` bound for this invocation only."""
    prior = os.environ.get("GH_TOKEN")
    os.environ["GH_TOKEN"] = token
    try:
        return runner(["gh", *args])
    finally:
        if prior is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = prior


def _disable_auto_merge(runner: CommandRunner, repo: str, pr: int, token: str) -> bool:
    if not token:
        return False
    proc = _gh(
        runner,
        token,
        ["pr", "merge", str(pr), "--repo", repo, "--disable-auto"],
    )
    return proc.returncode == 0


def _convert_to_draft(runner: CommandRunner, repo: str, pr: int, token: str) -> bool:
    if not token:
        return False
    proc = _gh(runner, token, ["pr", "ready", str(pr), "--repo", repo, "--undo"])
    return proc.returncode == 0


def _add_label(runner: CommandRunner, repo: str, pr: int, token: str) -> bool:
    proc = _gh(
        runner,
        token,
        ["pr", "edit", str(pr), "--repo", repo, "--add-label", CONFLICT_DISARMED_LABEL],
    )
    return proc.returncode == 0


def _remove_label(runner: CommandRunner, repo: str, pr: int, token: str) -> bool:
    proc = _gh(
        runner,
        token,
        [
            "pr",
            "edit",
            str(pr),
            "--repo",
            repo,
            "--remove-label",
            CONFLICT_DISARMED_LABEL,
        ],
    )
    return proc.returncode == 0


def _comment_body(head_sha: str) -> str:
    return (
        f"{COMMENT_MARKER}\n"
        f"head_sha: {head_sha}\n\n"
        "Auto-merge was disabled because this pull request **conflicts** with "
        f"its base branch (head `{head_sha}`). Rebase or merge `main`, then re-arm "
        "with `python scripts/automerge_guard.py <owner/repo> <pr> --arm`."
    )


def _post_comment(
    runner: CommandRunner, repo: str, pr: int, token: str, head_sha: str
) -> bool:
    proc = _gh(
        runner,
        token,
        ["pr", "comment", str(pr), "--repo", repo, "--body", _comment_body(head_sha)],
    )
    return proc.returncode == 0


def apply_action(
    repo: str,
    view: PullRequestView,
    action: Action,
    *,
    runner: CommandRunner | None = None,
    app_token: str = "",
    github_token: str = "",
    dry_run: bool = False,
) -> ApplyResult:
    """Execute ``action`` for ``repo#view.number``."""
    run = runner or _default_runner
    if action == Action.NOOP:
        return ApplyResult(action, dry_run, skipped=True)
    if action == Action.RETRY_LATER:
        return ApplyResult(action, dry_run, skipped=True)
    if dry_run:
        return ApplyResult(action, dry_run=True)

    write_token = app_token or github_token
    revoked = False
    attempts = 0
    drafted = False

    if action == Action.DISARM:
        if view.auto_merge_armed:
            tokens = [t for t in (app_token, github_token) if t]
            for token in tokens:
                attempts += 1
                if _disable_auto_merge(run, repo, view.number, token):
                    revoked = True
                    break
            if not revoked and tokens:
                drafted = _convert_to_draft(run, repo, view.number, tokens[-1])
        labeled = _add_label(run, repo, view.number, write_token)
        commented = False
        if view.head_sha != view.last_marker_comment_sha:
            commented = _post_comment(
                run, repo, view.number, write_token, view.head_sha
            )
        return ApplyResult(
            action,
            dry_run=False,
            revoked=revoked,
            revoke_attempts=attempts,
            labeled=labeled,
            commented=commented,
            drafted=drafted,
        )

    if action == Action.CLEAR_LABEL:
        cleared = _remove_label(run, repo, view.number, write_token)
        return ApplyResult(action, dry_run=False, cleared=cleared)

    return ApplyResult(action, dry_run=False, skipped=True)  # type: ignore[unreachable]


_MARKER_SHA_RE = re.compile(
    rf"{re.escape(COMMENT_MARKER)}.*?head_sha:\s*([0-9a-f]+)",
    re.IGNORECASE | re.DOTALL,
)


def parse_marker_sha(body: str) -> str:
    """Extract the head SHA from the latest conflict-disarm comment body."""
    match = _MARKER_SHA_RE.search(body or "")
    return match.group(1) if match else ""


def normalize_mergeable(raw: Mapping[str, Any]) -> tuple[str, str]:
    """Map REST pull fields to ``(mergeable, mergeable_state)`` vocabulary."""
    mergeable_state = str(raw.get("mergeable_state") or "unknown").lower()
    mergeable_field = raw.get("mergeable")
    if mergeable_field is None:
        return "UNKNOWN", mergeable_state
    if mergeable_field is False or mergeable_state == "dirty":
        return "CONFLICTING", mergeable_state
    if mergeable_field is True:
        return "MERGEABLE", mergeable_state
    return "UNKNOWN", mergeable_state


def view_from_rest(
    raw: Mapping[str, Any], *, commented_sha: str = ""
) -> PullRequestView:
    mergeable, mergeable_state = normalize_mergeable(raw)
    labels_raw = raw.get("labels") or []
    labels = frozenset(
        str(item.get("name", "")) for item in labels_raw if isinstance(item, dict)
    )
    return PullRequestView(
        number=int(raw.get("number") or 0),
        draft=bool(raw.get("draft")),
        state=str(raw.get("state") or "open"),
        mergeable=mergeable,
        mergeable_state=mergeable_state,
        auto_merge_armed=raw.get("auto_merge") is not None,
        labels=labels,
        head_sha=str((raw.get("head") or {}).get("sha") or ""),
        last_marker_comment_sha=commented_sha,
    )


def select_pr_numbers(
    rows: Sequence[Mapping[str, Any]],
    *,
    exclude: set[int],
    label: str,
) -> list[int]:
    """Return open PR numbers that are armed or carry ``label``, minus exclusions."""
    numbers: list[int] = []
    label_lower = label.lower()
    for row in rows:
        number = int(row.get("number") or 0)
        if not number or number in exclude:
            continue
        labels = row.get("labels") or []
        has_label = any(
            isinstance(item, dict) and str(item.get("name", "")).lower() == label_lower
            for item in labels
        )
        armed = row.get("auto_merge") is not None
        if armed or has_label:
            numbers.append(number)
    return sorted(numbers)


def _gh_json(runner: CommandRunner, args: Sequence[str]) -> Any:
    proc = runner(["gh", *args])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh failed")
    return json.loads(proc.stdout or "null")


def _fetch_open_pulls(runner: CommandRunner, repo: str) -> list[dict[str, Any]]:
    raw = _gh_json(
        runner,
        [
            "api",
            f"repos/{repo}/pulls?state=open&per_page=100",
            "--paginate",
        ],
    )
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _latest_marker_comment_sha(runner: CommandRunner, repo: str, pr: int) -> str:
    try:
        proc = runner(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr}/comments?per_page=100",
                "--paginate",
                "--jq",
                f'.[] | select(.body | contains("{COMMENT_MARKER}")) | .body',
            ]
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    matches = _MARKER_SHA_RE.findall(proc.stdout)
    return matches[-1] if matches else ""


@dataclass
class SweepSummary:
    disarmed: int = 0
    cleared: int = 0
    retried: int = 0
    noop: int = 0
    errors: int = 0


def sweep(
    repo: str,
    *,
    runner: CommandRunner | None = None,
    app_token: str = "",
    github_token: str = "",
    dry_run: bool = False,
    exclude: set[int] | None = None,
) -> SweepSummary:
    """Evaluate every candidate PR in ``repo`` and apply decisions."""
    run = runner or _default_runner
    excluded = exclude or set()
    summary = SweepSummary()
    try:
        rows = _fetch_open_pulls(run, repo)
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        logger.warning("conflict_disarm: could not list PRs for %s: %s", repo, exc)
        summary.errors += 1
        return summary

    for number in select_pr_numbers(
        rows, exclude=excluded, label=CONFLICT_DISARMED_LABEL
    ):
        try:
            raw = _gh_json(run, ["api", f"repos/{repo}/pulls/{number}"])
            if not isinstance(raw, dict):
                summary.errors += 1
                continue
            commented_sha = _latest_marker_comment_sha(run, repo, number)
            view = view_from_rest(raw, commented_sha=commented_sha)
            action = decide(view)
            if action == Action.RETRY_LATER:
                summary.retried += 1
                continue
            if action == Action.NOOP:
                summary.noop += 1
                continue
            result = apply_action(
                repo,
                view,
                action,
                runner=run,
                app_token=app_token,
                github_token=github_token,
                dry_run=dry_run,
            )
            if action == Action.DISARM and (
                result.revoked or result.labeled or dry_run
            ):
                summary.disarmed += 1
            elif action == Action.CLEAR_LABEL and (result.cleared or dry_run):
                summary.cleared += 1
        except (RuntimeError, json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("conflict_disarm: PR %s#%s skipped: %s", repo, number, exc)
            summary.errors += 1
    return summary


def _parse_exclude(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute actions only; do not mutate GitHub.",
    )
    parser.add_argument(
        "--exclude-prs",
        default="",
        help="Comma/space-separated PR numbers to skip (e.g. 12634).",
    )
    parser.add_argument(
        "--app-token",
        default=os.environ.get("CONFLICT_DISARM_APP_TOKEN", ""),
        help="Fleet GitHub App token (preferred).",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", "")),
        help="Fallback Actions token.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parse_args(argv)
    summary = sweep(
        args.repo,
        app_token=args.app_token,
        github_token=args.github_token,
        dry_run=args.dry_run,
        exclude=_parse_exclude(args.exclude_prs),
    )
    payload = {
        "disarmed": summary.disarmed,
        "cleared": summary.cleared,
        "retried": summary.retried,
        "noop": summary.noop,
        "errors": summary.errors,
        "dry_run": args.dry_run,
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0 if summary.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
