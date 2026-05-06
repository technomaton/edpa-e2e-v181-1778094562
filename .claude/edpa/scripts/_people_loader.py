"""People-registry loader and hygiene validator.

Owns three concerns that previously lived scattered across consumers:

  1. Loading ``.edpa/config/people.yaml`` and giving callers a clean
     list of person dicts plus a fast ``id -> person`` index.
  2. Producing the canonical avatar / display string for a person
     (``@github_login`` if set; falls back to the internal ``id``).
  3. Validating the registry against backlog + iteration usage —
     anyone who shows up as an assignee but has no ``github`` login
     in people.yaml is flagged so ``sync push --assignee`` does not
     silently skip them.

The validator returns the same diagnostic shape as ``_pi_loader`` so
``edpa_validate`` can merge them into one structured response.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def _default_loader(path: Path) -> "dict | None":
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("_people_loader: failed to read %s: %s", path, exc)
        return None


def load_people(edpa_root: Path, *, loader=None) -> tuple[list[dict], dict]:
    """Return ``(people, by_id)``. Both are empty if people.yaml is missing."""
    load = loader or _default_loader
    cfg = load(edpa_root / "config" / "people.yaml") or {}
    people = cfg.get("people", []) or []
    by_id = {p["id"]: p for p in people if isinstance(p, dict) and p.get("id")}
    return people, by_id


def display_handle(person: dict) -> str:
    """Render `@github_login` if set, else fall back to the internal id."""
    gh = person.get("github")
    if gh:
        return f"@{gh}"
    return person.get("id", "?")


def avatar_url(person: dict, size: int = 40) -> "str | None":
    """Best-effort avatar URL. None if no github login is on file."""
    gh = person.get("github")
    if not gh:
        return None
    return f"https://github.com/{gh}.png?size={size}"


def _diag(severity: str, code: str, message: str, **extra) -> dict:
    return {"severity": severity, "code": code, "message": message, **extra}


def _collect_assignees_from_iterations(edpa_root: Path,
                                       loader=None) -> "set[str]":
    """Walk iterations/*.yaml stories_detail[*].assignee — return the
    set of internal person IDs referenced.
    """
    iter_dir = edpa_root / "iterations"
    if not iter_dir.is_dir():
        return set()
    load = loader or _default_loader
    seen: set[str] = set()
    for f in sorted(iter_dir.glob("*.yaml")):
        doc = load(f) or {}
        for s in doc.get("stories_detail", []) or []:
            assignee = s.get("assignee")
            if isinstance(assignee, str) and assignee:
                seen.add(assignee)
    return seen


def _collect_assignees_from_backlog(edpa_root: Path,
                                    loader=None) -> "set[str]":
    """Walk backlog/{stories,features,epics,initiatives}/*.yaml — return
    the set of internal person IDs referenced as ``assignee``."""
    backlog = edpa_root / "backlog"
    if not backlog.is_dir():
        return set()
    load = loader or _default_loader
    seen: set[str] = set()
    for sub in ("stories", "features", "epics", "initiatives"):
        d = backlog / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            doc = load(f) or {}
            assignee = doc.get("assignee")
            if isinstance(assignee, str) and assignee:
                seen.add(assignee)
    return seen


def validate_people(edpa_root: Path, *, loader=None) -> list[dict]:
    """Cross-reference people.yaml against assignee usage in
    iterations/ and backlog/. Return diagnostics shaped like
    ``_pi_loader``: ``severity``, ``code``, ``message`` plus context.

    Codes emitted:
      - ``person_missing`` (error): assignee references an id that is
        not in people.yaml at all.
      - ``person_no_github`` (warning): person is referenced as an
        assignee but has no ``github`` login on file. Sync push will
        record the assignee in the issue body but cannot set the
        actual GitHub assignee, so the GitHub UI shows nobody.
      - ``person_unused`` (info-only warning): person exists in
        people.yaml but is not referenced anywhere — flagged so the
        registry does not bloat indefinitely.
    """
    people, by_id = load_people(edpa_root, loader=loader)
    used_in_iters = _collect_assignees_from_iterations(edpa_root, loader=loader)
    used_in_backlog = _collect_assignees_from_backlog(edpa_root, loader=loader)
    used = used_in_iters | used_in_backlog

    diags: list[dict] = []

    for assignee in sorted(used):
        person = by_id.get(assignee)
        if person is None:
            diags.append(_diag(
                "error", "person_missing",
                f"assignee {assignee!r} referenced by an iteration story or "
                f"backlog item but not present in people.yaml",
                person=assignee,
            ))
            continue
        if not person.get("github"):
            diags.append(_diag(
                "warning", "person_no_github",
                f"{assignee} is an active assignee but has no github login — "
                f"sync push will not set the GitHub assignee for their issues",
                person=assignee,
            ))

    for pid, person in by_id.items():
        if pid in used:
            continue
        diags.append(_diag(
            "warning", "person_unused",
            f"{pid} is in people.yaml but does not appear as an assignee in "
            f"any iteration story or backlog item",
            person=pid,
        ))

    return diags


def split_diagnostics(diags: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Mirror of _pi_loader.split_diagnostics — kept here so callers can
    import a single helper instead of two."""
    errors = [d for d in diags if d.get("severity") == "error"]
    warnings = [d for d in diags if d.get("severity") == "warning"]
    return errors, warnings
