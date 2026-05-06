#!/usr/bin/env python3
"""
EDPA Syntax Validator — validates YAML, JSON, Python, and (for files under
.edpa/backlog/) backlog item schema.

Used by:
  - Git pre-commit hook (file list from hook script)
  - Claude Code PostToolUse hook (single file via wrapper)
  - CLI validation (directory or file list, "-" / /dev/stdin reads from stdin)

Checks:
  - YAML: syntax + .tmpl files
  - JSON: syntax
  - Python: syntax (ast.parse)
  - Binary detection (UnicodeDecodeError)
  - Backlog item schema: required fields, status enum per type,
    contributors[].as / cw shape (.edpa/backlog/{initiatives,epics,features,stories,defects}/*.yaml)
"""

import ast
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

YAML_EXTENSIONS = {".yaml", ".yml", ".tmpl"}
JSON_EXTENSIONS = {".json"}
PYTHON_EXTENSIONS = {".py"}

# ─────────────────────────────────────────────────────────────────────
# Backlog schema (kept in sync with templates/cw_heuristics.yaml.tmpl,
# project_setup.py field options, and engine.EVIDENCE_ROLES)
# ─────────────────────────────────────────────────────────────────────
PORTFOLIO_STATUSES = {
    "Funnel", "Reviewing", "Analyzing", "Ready", "Implementing", "Done",
}
DELIVERY_STATUSES = {
    "Funnel", "Analyzing", "Backlog", "Implementing", "Validating",
    "Deploying", "Releasing", "Done",
}
# Non-blocking statuses that legacy backlogs may carry. We accept them
# silently for the validator (no error) but they won't round-trip
# through GitHub Projects because the typed Status fields don't list
# them. Setup docs flag this.
LEGACY_STATUSES = {"Active", "Closed", "Accepted"}

ITEM_SCHEMA = {
    "Initiative": {
        "dir": "initiatives",
        "required": {"id", "type", "title", "status"},
        "optional": {"parent", "js", "owner", "assignee", "contributors", "iteration"},
        "statuses": PORTFOLIO_STATUSES | LEGACY_STATUSES,
        "parent_required": False,
    },
    "Epic": {
        "dir": "epics",
        "required": {"id", "type", "title", "parent", "status"},
        "optional": {"js", "owner", "assignee", "contributors", "iteration"},
        "statuses": PORTFOLIO_STATUSES | LEGACY_STATUSES,
        "parent_required": True,
    },
    "Feature": {
        "dir": "features",
        "required": {"id", "type", "title", "parent", "status", "js"},
        "optional": {"owner", "assignee", "contributors", "iteration",
                     "bv", "tc", "rr", "wsjf"},
        "statuses": DELIVERY_STATUSES | LEGACY_STATUSES,
        "parent_required": True,
    },
    "Story": {
        "dir": "stories",
        "required": {"id", "type", "title", "parent", "status", "js", "iteration"},
        "optional": {"owner", "assignee", "contributors",
                     "bv", "tc", "rr", "wsjf"},
        "statuses": DELIVERY_STATUSES | LEGACY_STATUSES,
        "parent_required": True,
    },
    "Defect": {
        "dir": "defects",
        "required": {"id", "type", "title", "status", "js"},
        "optional": {"parent", "owner", "assignee", "contributors", "iteration"},
        "statuses": DELIVERY_STATUSES | LEGACY_STATUSES,
        "parent_required": False,
    },
    "Task": {
        "dir": "tasks",
        "required": {"id", "type", "title", "status"},
        "optional": {"parent", "js", "owner", "assignee", "contributors", "iteration"},
        "statuses": DELIVERY_STATUSES | LEGACY_STATUSES,
        "parent_required": False,
    },
}

# Mirror engine.EVIDENCE_ROLES — kept here so the validator stays
# self-contained and doesn't import the full engine module just to
# check a contributor entry.
EVIDENCE_ROLES = {"owner", "key", "reviewer", "consulted"}

# Type → expected `id` prefix (matches naming.item_prefixes default).
TYPE_PREFIXES = {
    "Initiative": "I",
    "Epic": "E",
    "Feature": "F",
    "Story": "S",
    "Defect": "D",
    "Task": "T",
}


def _is_backlog_path(path: Path) -> bool:
    """True if `path` lives under a backlog item dir we know how to validate."""
    parts = path.parts
    if ".edpa" not in parts:
        return False
    try:
        idx = parts.index(".edpa")
    except ValueError:
        return False
    backlog_idx = idx + 1
    if backlog_idx >= len(parts) or parts[backlog_idx] != "backlog":
        return False
    type_idx = backlog_idx + 1
    if type_idx >= len(parts):
        return False
    type_dir = parts[type_idx]
    return any(s["dir"] == type_dir for s in ITEM_SCHEMA.values())


def _schema_for_path(path: Path):
    """Return (item_type, schema) when path is under a known type dir."""
    for item_type, schema in ITEM_SCHEMA.items():
        if f"/backlog/{schema['dir']}/" in str(path).replace("\\", "/"):
            return item_type, schema
    return None, None


def validate_backlog_schema(path: Path, data, *, strict=False):
    """Validate a parsed backlog-item dict against ITEM_SCHEMA.

    Returns (errors, warnings) — both lists of "<path>: <message>" strings.
    Contributors role checks are warnings by default (real-world backlogs
    often use human-readable labels like 'architect' for documentation);
    pass strict=True to upgrade them to errors.
    """
    errors = []
    warnings = []
    if not isinstance(data, dict):
        return [f"{path}: backlog item must be a YAML mapping (got {type(data).__name__})"], warnings

    expected_type, schema = _schema_for_path(path)
    if not schema:
        return errors  # not a backlog file we recognize

    declared_type = data.get("type")
    if declared_type and declared_type != expected_type:
        errors.append(
            f"{path}: type={declared_type!r} but file is under "
            f"backlog/{schema['dir']}/ (expected type={expected_type!r})"
        )

    # Required fields
    for field in schema["required"]:
        if field == "parent":
            # `parent: null` is acceptable for items where parent is optional
            if "parent" not in data:
                if schema["parent_required"]:
                    errors.append(f"{path}: missing required field 'parent'")
            continue
        if field not in data or data[field] in (None, ""):
            errors.append(f"{path}: missing required field {field!r}")

    # ID prefix sanity
    item_id = data.get("id")
    if isinstance(item_id, str):
        prefix = TYPE_PREFIXES.get(expected_type)
        if prefix and not item_id.startswith(f"{prefix}-"):
            errors.append(
                f"{path}: id={item_id!r} should start with {prefix!r}- "
                f"for type {expected_type}"
            )

    # Status enum
    status = data.get("status")
    if status and status not in schema["statuses"]:
        errors.append(
            f"{path}: status={status!r} is not valid for {expected_type}. "
            f"Allowed: {sorted(schema['statuses'])}"
        )

    # JS sanity
    js = data.get("js")
    if js is not None:
        try:
            js_val = float(js)
            if js_val <= 0:
                errors.append(f"{path}: js must be > 0 (got {js!r})")
        except (TypeError, ValueError):
            errors.append(f"{path}: js must be numeric (got {js!r})")

    # Iteration tag sanity for Stories
    if expected_type == "Story":
        iteration = data.get("iteration", "")
        if iteration and not isinstance(iteration, str):
            errors.append(f"{path}: iteration must be a string (got {type(iteration).__name__})")

    # Contributors schema. The evidence-role enum lives under `as:` since
    # v1.7. Legacy keys `role:` and `weight:` are HARD errors with a
    # migration pointer — there are no backwards-compatibility aliases,
    # users have to run migrate_contributors.py once. Values outside the
    # evidence enum are warnings by default (rich-doc backlogs use
    # human-readable labels for documentation); --strict upgrades them.
    contribs = data.get("contributors")
    if contribs is not None:
        bucket = errors if strict else warnings
        if not isinstance(contribs, list):
            errors.append(f"{path}: contributors must be a list (got {type(contribs).__name__})")
        else:
            for idx, entry in enumerate(contribs):
                if not isinstance(entry, dict):
                    errors.append(
                        f"{path}: contributors[{idx}] must be a mapping "
                        f"(got {type(entry).__name__})"
                    )
                    continue
                if not entry.get("person"):
                    bucket.append(f"{path}: contributors[{idx}] missing 'person'")
                # Reject legacy keys outright with a migration breadcrumb.
                if "role" in entry:
                    errors.append(
                        f"{path}: contributors[{idx}] uses legacy 'role' — "
                        f"renamed to 'as' in v1.7 to disambiguate from "
                        f"people[].role. Run "
                        f"`python3 .claude/edpa/scripts/migrate_contributors.py` "
                        f"to rewrite the whole backlog at once."
                    )
                if "weight" in entry:
                    errors.append(
                        f"{path}: contributors[{idx}] uses legacy 'weight' — "
                        f"renamed to 'cw' in v1.7. Run "
                        f"`python3 .claude/edpa/scripts/migrate_contributors.py`."
                    )
                evidence_as = (entry.get("as") or "").lower()
                if not evidence_as and "role" not in entry:
                    bucket.append(
                        f"{path}: contributors[{idx}] missing 'as' "
                        f"(one of {sorted(EVIDENCE_ROLES)})"
                    )
                elif evidence_as and evidence_as not in EVIDENCE_ROLES:
                    bucket.append(
                        f"{path}: contributors[{idx}] as={evidence_as!r} is not an evidence role "
                        f"({sorted(EVIDENCE_ROLES)}). Job roles (Dev/Arch/QA/PM) "
                        f"belong in people.yaml — engine will not credit this contributor."
                    )
                # cw — out-of-range is always an error (real correctness)
                cw_value = entry.get("cw")
                if cw_value is not None:
                    try:
                        cw_num = float(cw_value)
                    except (TypeError, ValueError):
                        errors.append(
                            f"{path}: contributors[{idx}] cw must be numeric "
                            f"(got {cw_value!r})"
                        )
                        continue
                    if not 0 <= cw_num <= 1:
                        errors.append(
                            f"{path}: contributors[{idx}] cw must be in [0,1] "
                            f"(got {cw_num})"
                        )

    return errors, warnings


def validate_yaml(path, *, content=None, strict=False):
    """Validate a single YAML file. Returns (errors, warnings).

    `content` may be passed in for stdin mode; otherwise file is read.
    `strict` upgrades soft schema warnings (unknown contributor role,
    missing person/role) to errors.
    """
    errors = []
    warnings = []
    path = Path(path)

    if content is None:
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            errors.append(f"{path}: file not found")
            return errors, warnings
        except UnicodeDecodeError:
            errors.append(f"{path}: binary file, not valid YAML")
            return errors, warnings

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        errors.append(f"{path}: {e}")
        return errors, warnings

    # Backlog schema check applies only to files we recognize as backlog items.
    if data is not None and _is_backlog_path(path):
        item_errors, item_warnings = validate_backlog_schema(
            path, data, strict=strict)
        errors.extend(item_errors)
        warnings.extend(item_warnings)

    return errors, warnings


def validate_json(path, *, content=None):
    """Validate a single JSON file. Returns (errors, warnings)."""
    path = Path(path)
    if content is None:
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [f"{path}: file not found"], []
        except UnicodeDecodeError:
            return [f"{path}: binary file, not valid JSON"], []

    try:
        json.loads(content)
    except json.JSONDecodeError as e:
        return [f"{path}: {e}"], []
    return [], []


def validate_python(path, *, content=None):
    """Validate Python syntax. Returns (errors, warnings)."""
    path = Path(path)
    if content is None:
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [f"{path}: file not found"], []
        except UnicodeDecodeError:
            return [f"{path}: binary file, not valid Python"], []

    try:
        ast.parse(content, filename=str(path))
    except SyntaxError as e:
        return [f"{path}: {e.msg} (line {e.lineno})"], []
    return [], []


def validate_file(path, *, content=None, kind=None, strict=False):
    """Validate a single file based on its extension or explicit kind.

    Returns (errors, warnings).
    """
    path = Path(path)
    if kind is None:
        ext = path.suffix.lower()
        if ext in YAML_EXTENSIONS:
            kind = "yaml"
        elif ext in JSON_EXTENSIONS:
            kind = "json"
        elif ext in PYTHON_EXTENSIONS:
            kind = "python"
        else:
            return [], []

    if kind == "yaml":
        return validate_yaml(path, content=content, strict=strict)
    if kind == "json":
        return validate_json(path, content=content)
    if kind == "python":
        return validate_python(path, content=content)
    return [], []


def validate_directory(directory, *, strict=False):
    """Validate all supported files in a directory tree.

    Returns (errors, warnings).
    """
    directory = Path(directory)
    all_errors = []
    all_warnings = []
    seen = set()

    for ext_set in [YAML_EXTENSIONS, JSON_EXTENSIONS, PYTHON_EXTENSIONS]:
        for ext in ext_set:
            for path in directory.glob(f"**/*{ext}"):
                if path in seen:
                    continue
                seen.add(path)
                e, w = validate_file(path, strict=strict)
                all_errors.extend(e)
                all_warnings.extend(w)

    return all_errors, all_warnings


def _read_stdin():
    return sys.stdin.read()


def main():
    if len(sys.argv) < 2:
        print("Usage: validate_syntax.py <path> [<path> ...]", file=sys.stderr)
        print("       validate_syntax.py - --kind yaml         (read from stdin)",
              file=sys.stderr)
        print("       validate_syntax.py --strict <path>       (upgrade soft warnings to errors)",
              file=sys.stderr)
        sys.exit(1)

    # Parse --kind (stdin mode) and --strict flags out of argv
    kind_override = None
    strict = False
    cleaned = []
    it = iter(sys.argv[1:])
    for arg in it:
        if arg == "--kind":
            try:
                kind_override = next(it).lower()
            except StopIteration:
                print("ERROR: --kind requires a value", file=sys.stderr)
                sys.exit(1)
            continue
        if arg == "--strict":
            strict = True
            continue
        cleaned.append(arg)

    all_errors = []
    all_warnings = []
    for arg in cleaned:
        if arg in ("-", "/dev/stdin"):
            content = _read_stdin()
            kind = kind_override or "yaml"  # default for backlog hooks
            label = Path("<stdin>")
            e, w = validate_file(label, content=content, kind=kind, strict=strict)
            all_errors.extend(e)
            all_warnings.extend(w)
            continue
        p = Path(arg)
        if p.is_dir():
            e, w = validate_directory(p, strict=strict)
            all_errors.extend(e)
            all_warnings.extend(w)
        elif p.is_file():
            e, w = validate_file(p, strict=strict)
            all_errors.extend(e)
            all_warnings.extend(w)
        else:
            all_errors.append(f"{p}: not found")

    for w in all_warnings:
        print(f"WARN:  {w}", file=sys.stderr)

    if all_errors:
        for err in all_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    elif all_warnings:
        print(f"All files valid (with {len(all_warnings)} warning"
              f"{'s' if len(all_warnings) != 1 else ''}).")
    else:
        print("All files valid.")


if __name__ == "__main__":
    main()
