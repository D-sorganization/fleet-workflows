"""Paginated gh reads must work on any gh release: no ``--slurp`` (gh >= 2.48)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

import gh_pagination
import pytest
from test_codex_review_request import crr
from test_conflict_disarm import conflict_disarm

ENDPOINT = "repos/o/r/pulls?state=open&per_page=100"


def _old_gh(stdout: str) -> Any:
    """Return a runner shaped like a gh release that predates ``--slurp``."""

    def run(cmd: Sequence[str], token: str = "") -> Any:
        assert "--paginate" in cmd
        assert "--jq" not in cmd and "--template" not in cmd
        if "--slurp" in cmd:
            return subprocess.CompletedProcess(
                list(cmd), 1, stdout="", stderr="unknown flag: --slurp"
            )
        return subprocess.CompletedProcess(list(cmd), 0, stdout=stdout, stderr="")

    return run


def _fetch_both(stdout: str) -> list[list[dict[str, Any]]]:
    run = _old_gh(stdout)
    return [
        crr._gh_items(run, "read-token", ENDPOINT),
        conflict_disarm._gh_paginated(run, ENDPOINT),
    ]


@pytest.mark.parametrize(
    "stdout",
    ['[{"a":1}][{"a":2}]', '[{"a":1}]\n[{"a":2}]\n', ' [{"a":1}] \r\n\t[{"a":2}]'],
)
def test_both_scripts_read_concatenated_pages_without_slurp(stdout: str) -> None:
    assert _fetch_both(stdout) == [[{"a": 1}, {"a": 2}]] * 2


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('[{"a":1},{"a":2}]', [{"a": 1}, {"a": 2}]),
        ("", []),
        ("\n", []),
        ("[]", []),
        ("[][]", []),
        ('[][{"a":1}]', [{"a": 1}]),
    ],
)
def test_single_empty_and_empty_array_pages(
    stdout: str, expected: list[dict[str, Any]]
) -> None:
    assert _fetch_both(stdout) == [expected, expected]


def test_page_boundary_text_inside_a_string_value_is_not_split() -> None:
    stdout = '[{"body":"a][b \\" ]\\n[ c"}][{"body":"}]["}]'
    expected = [{"body": 'a][b " ]\n[ c'}, {"body": "}]["}]
    assert _fetch_both(stdout) == [expected, expected]


def test_non_array_documents_and_non_object_items_are_skipped() -> None:
    stdout = '{"message":"x"}[{"a":1},2,"s",null,[{"nested":1}]]null'
    assert gh_pagination.decode_paginated_items(stdout) == [{"a": 1}]


def test_truncated_output_raises_a_json_error() -> None:
    with pytest.raises(ValueError):
        gh_pagination.decode_paginated_items('[{"a":1}][{"a":')


def test_gh_failure_keeps_its_stderr_message() -> None:
    def run(cmd: Sequence[str], token: str = "") -> Any:
        return subprocess.CompletedProcess(list(cmd), 1, stdout="", stderr="HTTP 502\n")

    with pytest.raises(RuntimeError, match="^HTTP 502$"):
        crr._gh_items(run, "read-token", ENDPOINT)
    with pytest.raises(RuntimeError, match="^HTTP 502$"):
        conflict_disarm._gh_paginated(run, ENDPOINT)
