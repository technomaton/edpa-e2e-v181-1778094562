"""GraphQL helper for linking GitHub Issues as sub-issues.

GitHub's `addSubIssue` mutation creates a parent → child relationship
that surfaces in the GitHub UI as native sub-issues (under the parent
issue's "Sub-issues" panel) and through the ProjectsV2 API as the
hierarchy field. The mutation is idempotent: calling it for an already
linked pair returns an "already a sub-issue" error which we treat as
success.

Two callers share this:
  * project_setup.py STEP 8 — initial bulk link of every backlog item
    that has a `parent:` field, right after the project + issues are
    created.
  * sync.py push — after each new issue is created, link it to its
    parent (if any) so the customer's hierarchy stays intact across
    incremental adds. Without this, every Story/Feature added after
    the initial setup lands as a top-level issue.
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Iterable

logger = logging.getLogger(__name__)


def _gh_graphql(query: str) -> "dict | None":
    """Execute a GraphQL query via `gh api graphql -f query=...`.
    Returns the parsed JSON response, or None on subprocess failure."""
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("gh graphql subprocess failed: %s", exc)
        return None
    if result.returncode != 0:
        # gh exits non-zero on GraphQL errors but still returns JSON in stdout.
        try:
            return json.loads(result.stdout)
        except (ValueError, json.JSONDecodeError):
            logger.warning("gh graphql exit %d: %s",
                           result.returncode, result.stderr.strip())
            return None
    try:
        return json.loads(result.stdout)
    except (ValueError, json.JSONDecodeError):
        return None


def link_sub_issue(parent_node_id: str, child_node_id: str) -> "tuple[bool, str]":
    """Link `child_node_id` as a sub-issue of `parent_node_id`.

    Returns ``(ok, message)``:
      ok=True   — mutation succeeded OR the pair was already linked
      ok=False  — GraphQL returned a real error (e.g. invalid node IDs,
                  missing permissions, parent issue closed); message
                  carries the GraphQL error text for caller logging.

    The "already linked" case is treated as success so that a re-run
    of the linker on a partially-synced project is safe.
    """
    if not parent_node_id or not child_node_id:
        return False, "missing parent or child node id"
    mutation = (
        f'mutation {{ addSubIssue(input: {{ '
        f'issueId: "{parent_node_id}", subIssueId: "{child_node_id}" }}) '
        f'{{ issue {{ id }} subIssue {{ id }} }} }}'
    )
    result = _gh_graphql(mutation)
    if result is None:
        return False, "gh graphql subprocess failed"
    errors = result.get("errors") or []
    if not errors:
        return True, "linked"
    # GraphQL returned errors. Several phrasings mean "already linked"
    # depending on which validator GitHub picked up the duplicate at.
    # Treat them all as idempotent so a rerun reports `Links: N` instead
    # of `Links: 0` while pretending it failed.
    err_text = "; ".join(str(e.get("message", e)) for e in errors)
    err_lower = err_text.lower()
    idempotent_phrases = (
        "already",
        "duplicate sub-issue",
        "may only have one parent",
    )
    if any(phrase in err_lower for phrase in idempotent_phrases):
        return True, "already linked"
    return False, err_text


def link_items(items: Iterable[dict],
               issue_map: dict,
               *,
               on_skip=None,
               on_link=None,
               on_error=None) -> "dict[str, int]":
    """Walk ``items`` and call addSubIssue for any item with a parent.

    ``issue_map`` maps internal id → tuple ``(issue_number, url, node_id)``
    (matching project_setup.py's existing structure). Items whose own
    or parent's mapping is missing get skipped.

    Optional ``on_skip``, ``on_link``, ``on_error`` callbacks receive
    ``(child_id, parent_id, message)`` so callers can drive their own
    logging (project_setup uses colored banners, sync.py prints plain
    lines). All three default to no-op.

    Returns ``{"linked": int, "errors": int, "skipped": int}``.
    """
    on_skip = on_skip or (lambda *_: None)
    on_link = on_link or (lambda *_: None)
    on_error = on_error or (lambda *_: None)

    counts = {"linked": 0, "errors": 0, "skipped": 0}

    for item in items:
        parent_id = item.get("parent")
        if not parent_id:
            continue

        child_id = item.get("id")
        child_mapping = issue_map.get(child_id)
        parent_mapping = issue_map.get(parent_id)

        if not child_mapping or not parent_mapping:
            counts["skipped"] += 1
            on_skip(child_id, parent_id, "parent not in issue_map")
            continue

        _, _, child_node_id = child_mapping
        _, _, parent_node_id = parent_mapping
        if not child_node_id or not parent_node_id:
            counts["skipped"] += 1
            on_skip(child_id, parent_id, "missing node id")
            continue

        ok, msg = link_sub_issue(parent_node_id, child_node_id)
        if ok:
            counts["linked"] += 1
            on_link(child_id, parent_id, msg)
        else:
            counts["errors"] += 1
            on_error(child_id, parent_id, msg)

    return counts
