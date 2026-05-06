#!/usr/bin/env python3
"""
EDPA — one-shot backlog migration: contributors[].role → as, weight → cw.

Reason: people.yaml uses `role:` for the human job role (Dev/Arch/QA/PM),
backlog YAMLs used `role:` for the evidence role (owner/key/reviewer/
consulted). Two domains, one key — confusing on every read. v1.7
renames the backlog key to `as:` so the two never collide. There is
no backwards-compat alias on the engine side; this script is the only
fixup path.

What it does, per `.edpa/backlog/**/*.yaml`:

  - rewrite `contributors[*].role` → `contributors[*].as`
  - rewrite `contributors[*].weight` → `contributors[*].cw`
  - if the value of `as:` is a known person-role label
    (Dev / Arch / architect / developer / QA / PM / product_owner /
    DevSecOps), translate it to its closest evidence role using the
    table below.
  - leave anything that's already valid (`as: owner` etc.) alone.

Person-role → evidence-role mapping (deliberately conservative — pick
the most common GH evidence signal for each role):

    Dev / dev / developer        → owner       (typical assignee)
    Arch / arch / architect      → key         (PR author / lead)
    QA / qa                      → reviewer
    PM / pm / product_owner / BO → consulted

Anything outside the table is left as-is (so a manual reviewer can
spot it via `validate_syntax.py`).

Usage:
    python3 plugin/edpa/scripts/migrate_contributors.py            # default: .edpa/backlog/
    python3 plugin/edpa/scripts/migrate_contributors.py --dry-run  # preview
    python3 plugin/edpa/scripts/migrate_contributors.py --root other/.edpa/backlog
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required.", file=sys.stderr)
    sys.exit(1)


PERSON_TO_EVIDENCE = {
    "dev": "owner",
    "developer": "owner",
    "arch": "key",
    "architect": "key",
    "qa": "reviewer",
    "tester": "reviewer",
    "pm": "consulted",
    "product_owner": "consulted",
    "po": "consulted",
    "bo": "consulted",
    "business_owner": "consulted",
    "devsecops": "owner",
    "secops": "owner",
}

EVIDENCE_ROLES = {"owner", "key", "reviewer", "consulted"}


def migrate_contributor(entry: dict) -> dict | None:
    """Return a new contributor dict with v1.7 keys.

    Returns None when the entry is malformed (caller skips it).
    """
    if not isinstance(entry, dict):
        return None

    new_entry = dict(entry)
    notes = []

    # role → as
    if "role" in new_entry and "as" not in new_entry:
        new_entry["as"] = new_entry.pop("role")
        notes.append("role→as")
    elif "role" in new_entry and "as" in new_entry:
        # Both present — drop role (as wins).
        new_entry.pop("role")
        notes.append("dropped duplicate role")

    # weight → cw
    if "weight" in new_entry and "cw" not in new_entry:
        new_entry["cw"] = new_entry.pop("weight")
        notes.append("weight→cw")
    elif "weight" in new_entry and "cw" in new_entry:
        new_entry.pop("weight")
        notes.append("dropped duplicate weight")

    # Translate person-role values to evidence-role values
    raw_as = new_entry.get("as")
    if isinstance(raw_as, str):
        lower = raw_as.strip().lower()
        if lower in PERSON_TO_EVIDENCE:
            new_entry["as"] = PERSON_TO_EVIDENCE[lower]
            notes.append(f"as: {raw_as}→{new_entry['as']}")
        elif lower in EVIDENCE_ROLES and lower != raw_as:
            new_entry["as"] = lower  # normalize case

    return new_entry if notes else entry


def migrate_file(path: Path, *, dry_run: bool) -> tuple[bool, list[str]]:
    """Rewrite one backlog YAML in place. Returns (changed, notes)."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        return False, []
    contribs = data.get("contributors")
    if not isinstance(contribs, list) or not contribs:
        return False, []

    changed = False
    notes: list[str] = []
    new_list = []
    for idx, c in enumerate(contribs):
        before = c
        after = migrate_contributor(c)
        if after is None:
            new_list.append(before)
            continue
        if after is not before:
            changed = True
            notes.append(
                f"  {path.relative_to(path.parents[3]) if len(path.parents) >= 4 else path}"
                f" contributors[{idx}]: "
                f"{ {k: c.get(k) for k in ('role','weight') if k in c} }"
                f" → {{'as': {after.get('as')!r}"
                + (f", 'cw': {after.get('cw')}" if 'cw' in after else "")
                + "}"
            )
        new_list.append(after)

    if not changed:
        return False, []

    data["contributors"] = new_list
    if not dry_run:
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return True, notes


def main():
    parser = argparse.ArgumentParser(
        description="Migrate .edpa/backlog YAMLs to v1.7 contributors schema "
                    "(role→as, weight→cw, person-role→evidence-role)"
    )
    parser.add_argument(
        "--root", default=".edpa/backlog",
        help="Backlog root directory (default: .edpa/backlog)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show planned rewrites without modifying any file",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: {root} not found", file=sys.stderr)
        sys.exit(2)

    files = sorted(root.glob("**/*.yaml"))
    if not files:
        print(f"No YAML files under {root}")
        return

    total_files = 0
    changed_files = 0
    for f in files:
        changed, notes = migrate_file(f, dry_run=args.dry_run)
        total_files += 1
        if changed:
            changed_files += 1
            print(f"{'[dry-run] ' if args.dry_run else ''}rewrote {f}")
            for n in notes:
                print(n)

    print()
    print(f"Scanned {total_files} file(s); "
          f"{'would rewrite' if args.dry_run else 'rewrote'} "
          f"{changed_files}.")


if __name__ == "__main__":
    main()
