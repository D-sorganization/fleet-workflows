"""Read ``gh api --paginate`` output on any gh release.

``gh api --slurp`` only exists from gh 2.48, and self-hosted runners carry older
releases, so the fleet scripts must not pass it. Without it gh prints the pages
one after another (``[...][...]``, or one merged array, depending on the
release), which ``json.loads`` cannot parse as one document. This module reads
that stream document by document with the JSON decoder itself, so text such as
``][`` inside a string value never counts as a page boundary.

DbC
---
Pre: ``stdout`` is the complete standard output of a successful
``gh api <list endpoint> --paginate`` call with no ``--slurp``, ``--jq`` or
``--template`` flag.

Post: returns every JSON object found directly inside a top-level JSON array, in
output order. Documents that are not arrays and array members that are not
objects are skipped. Empty or whitespace-only output returns ``[]``.

Raises: ``json.JSONDecodeError`` when the output is not a sequence of complete
JSON documents (for example truncated output).
"""

from __future__ import annotations

import json
from typing import Any

PAGINATE_FLAGS: tuple[str, ...] = ("--paginate",)
_WHITESPACE = " \t\r\n"


def decode_paginated_items(stdout: str) -> list[dict[str, Any]]:
    """Flatten the page arrays in ``gh api --paginate`` output into one list."""
    decoder = json.JSONDecoder()
    items: list[dict[str, Any]] = []
    position = _skip_whitespace(stdout, 0)
    while position < len(stdout):
        page, position = decoder.raw_decode(stdout, position)
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
        position = _skip_whitespace(stdout, position)
    return items


def _skip_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position] in _WHITESPACE:
        position += 1
    return position
