#!/usr/bin/env python3
"""Validate .edpa/iterations/*.yaml structure and continuity.

Usage:
  python validate_iterations.py [path]    # path defaults to ./.edpa
  python validate_iterations.py --json    # machine-readable output
  python validate_iterations.py --quiet   # exit code only

Designed for two trigger points:
  1. Claude Code PostToolUse hook on Edit/Write of iterations/*.yaml
  2. CI / pre-commit gate

Exit code: 0 if no errors (warnings allowed), 1 if any error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _pi_loader import derive_pis, split_diagnostics  # noqa: E402
from _people_loader import validate_people  # noqa: E402

SEV_GLYPH = {"error": "✗", "warning": "⚠"}


def _find_edpa_root(start: Path) -> Path | None:
    """Walk up looking for .edpa/. If start IS .edpa, return it."""
    p = start.resolve()
    if p.name == ".edpa" and p.is_dir():
        return p
    if (p / ".edpa").is_dir():
        return p / ".edpa"
    while p != p.parent:
        if (p / ".edpa").is_dir():
            return p / ".edpa"
        p = p.parent
    return None


def _format_human(diags: list[dict]) -> str:
    if not diags:
        return "✓ iterations/ schema OK"
    lines = []
    for d in diags:
        glyph = SEV_GLYPH.get(d["severity"], "?")
        loc = d.get("iteration") or d.get("pi") or d.get("file") or "?"
        lines.append(f"{glyph} [{d['code']}] {loc}: {d['message']}")
    errors, warnings = split_diagnostics(diags)
    lines.append(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".",
                    help="Path to project root or .edpa/ dir (default: .)")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON {pis, errors, warnings} instead of human text")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress all output; rely on exit code")
    args = ap.parse_args(argv)

    edpa_root = _find_edpa_root(Path(args.path))
    if edpa_root is None:
        if not args.quiet:
            print(f"ERROR: no .edpa/ found from {args.path}", file=sys.stderr)
        return 1

    pis, iter_diags = derive_pis(edpa_root)
    people_diags = validate_people(edpa_root)
    diags = list(iter_diags) + list(people_diags)
    errors, warnings = split_diagnostics(diags)

    if args.json:
        print(json.dumps({"pis": pis, "errors": errors, "warnings": warnings},
                         indent=2, ensure_ascii=False))
    elif not args.quiet:
        print(_format_human(diags))

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
