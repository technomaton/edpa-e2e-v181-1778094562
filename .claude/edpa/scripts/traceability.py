#!/usr/bin/env python3
"""
EDPA Traceability Checker — verifies parent chain in backlog hierarchy.

Hierarchy: Initiative (I-*) -> Epic (E-*) -> Feature (F-*) -> Story (S-*)

Checks:
  - Every item declares a parent (except Initiative)
  - Parent exists in backlog
  - Parent type matches hierarchy rule
  - No cycles (parent chain terminates at Initiative)

Usage:
    python3 .claude/edpa/scripts/traceability.py
    python3 .claude/edpa/scripts/traceability.py --edpa-root .edpa
    python3 .claude/edpa/scripts/traceability.py --format json
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


PARENT_RULES = {
    "Initiative": None,
    "Epic": "Initiative",
    "Feature": "Epic",
    "Story": "Feature",
}

DIRECTORIES = {
    "Initiative": "initiatives",
    "Epic": "epics",
    "Feature": "features",
    "Story": "stories",
}


def load_backlog(edpa_root: Path):
    items = {}
    load_errors = []
    for item_type, dirname in DIRECTORIES.items():
        dir_path = edpa_root / "backlog" / dirname
        if not dir_path.is_dir():
            continue
        for f in sorted(dir_path.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                load_errors.append(f"{f}: YAML parse error: {e}")
                continue
            item_id = data.get("id") or f.stem
            declared_type = data.get("type")
            if declared_type and declared_type != item_type:
                load_errors.append(
                    f"{item_id}: file in {dirname}/ but type={declared_type}"
                )
            items[item_id] = {
                "id": item_id,
                "type": item_type,
                "parent": data.get("parent"),
                "path": str(f),
            }
    return items, load_errors


def check_chain(item_id: str, items: dict):
    """Walk parent chain from item_id to Initiative. Returns list of errors."""
    errors = []
    seen = set()
    current = item_id
    while True:
        if current in seen:
            errors.append(f"{item_id}: cycle detected through {current}")
            return errors
        seen.add(current)

        if current not in items:
            errors.append(f"{item_id}: ancestor {current} not found in backlog")
            return errors

        item = items[current]
        expected_parent_type = PARENT_RULES[item["type"]]

        if expected_parent_type is None:
            return errors

        parent_id = item["parent"]
        if not parent_id:
            errors.append(
                f"{current}: missing parent (expected {expected_parent_type})"
            )
            return errors

        if parent_id not in items:
            errors.append(
                f"{current}: parent {parent_id} not found in backlog"
            )
            return errors

        parent = items[parent_id]
        if parent["type"] != expected_parent_type:
            errors.append(
                f"{current}: parent {parent_id} is {parent['type']}, "
                f"expected {expected_parent_type}"
            )
            return errors

        current = parent_id


def check_all(items: dict):
    all_errors = []
    for item_id in sorted(items.keys()):
        all_errors.extend(check_chain(item_id, items))
    return all_errors


def main():
    parser = argparse.ArgumentParser(description="EDPA Traceability Checker")
    parser.add_argument("--edpa-root", default=".edpa", type=Path,
                        help="Path to .edpa directory (default: .edpa)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    if not args.edpa_root.is_dir():
        print(f"ERROR: {args.edpa_root} not found", file=sys.stderr)
        return 2

    items, load_errors = load_backlog(args.edpa_root)
    chain_errors = check_all(items)
    all_errors = load_errors + chain_errors

    if args.format == "json":
        print(json.dumps({
            "items_scanned": len(items),
            "errors": all_errors,
            "passed": len(all_errors) == 0,
        }, indent=2))
    else:
        counts = {}
        for it in items.values():
            counts[it["type"]] = counts.get(it["type"], 0) + 1
        print(f"Scanned {len(items)} items: " +
              ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
        if all_errors:
            print(f"\n✗ {len(all_errors)} traceability error(s):")
            for e in all_errors:
                print(f"  {e}")
        else:
            print("\n✓ All parent chains valid")

    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main())
