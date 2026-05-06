#!/usr/bin/env python3
"""
EDPA Sync CLI -- Bidirectional sync between GitHub Projects and .edpa/ item files.

Usage:
    python .claude/edpa/scripts/sync.py pull          # GitHub Projects -> .edpa/backlog/ item files
    python .claude/edpa/scripts/sync.py push          # .edpa/backlog/ item files -> GitHub Projects
    python .claude/edpa/scripts/sync.py diff           # Show what would change (dry-run)
    python .claude/edpa/scripts/sync.py log            # Show changelog
    python .claude/edpa/scripts/sync.py conflicts      # Show unresolved conflicts
    python .claude/edpa/scripts/sync.py status         # Show sync status

Flags:
    --mock       Simulate GitHub Project data from existing backlog (for testing)
    --commit     Auto-commit changes after pull (used by CI)
    --verbose    Show detailed output
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml")
    sys.exit(1)


# -- ANSI Colors (EDPA palette) -----------------------------------------------

class C:
    """ANSI color codes matching EDPA design palette."""
    RESET    = "\033[0m"
    BOLD     = "\033[1m"
    DIM      = "\033[2m"
    INIT     = "\033[35m"
    EPIC     = "\033[38;5;93m"
    FEAT     = "\033[36m"
    STORY    = "\033[32m"
    DONE     = "\033[32m"
    ACTIVE   = "\033[33m"
    PROGRESS = "\033[34m"
    PLANNED  = "\033[37m"
    WARN     = "\033[33m"
    ERR      = "\033[31m"
    OK       = "\033[32m"
    HEADER   = "\033[38;5;147m"
    MUTED    = "\033[38;5;245m"
    SYNC     = "\033[38;5;81m"   # Cyan-blue for sync operations
    # SAFe status colors
    FUNNEL   = "\033[38;5;245m"  # Gray -- not yet started
    REVIEW   = "\033[38;5;81m"   # Cyan-blue -- reviewing
    ANALYZE  = "\033[34m"        # Blue -- analyzing
    READY    = "\033[36m"        # Cyan -- ready to pull
    IMPL     = "\033[33m"        # Yellow -- implementing
    VALIDATE = "\033[35m"        # Magenta -- validating
    DEPLOY   = "\033[38;5;208m"  # Orange -- deploying
    RELEASE  = "\033[38;5;147m"  # Light purple -- releasing
    BACKLOG  = "\033[37m"        # Light gray -- in backlog
    DIFF_ADD = "\033[32m"
    DIFF_DEL = "\033[31m"
    DIFF_MOD = "\033[33m"


def color(text, code):
    return f"{code}{text}{C.RESET}"


def bold(text):
    return f"{C.BOLD}{text}{C.RESET}"


# -- Box-drawing characters ---------------------------------------------------

PIPE  = "\u2502"
TEE   = "\u251c"
ELBOW = "\u2514"
DASH  = "\u2500"
DOT   = "\u2022"
ARROW = "\u2192"
CHECK = "\u2713"
CROSS = "\u2717"
SYNC_ICON = "\u21c4"  # bidirectional arrow


# -- Validation ---------------------------------------------------------------

FIBONACCI = {1, 2, 3, 5, 8, 13, 20}
FIBONACCI_FIELDS = {"js", "bv", "tc", "rr"}

# -- SAFe Status Workflows -----------------------------------------------------

PORTFOLIO_STATUSES = ["Funnel", "Reviewing", "Analyzing", "Ready", "Implementing", "Done"]
DELIVERY_STATUSES = ["Funnel", "Analyzing", "Backlog", "Implementing", "Validating", "Deploying", "Releasing", "Done"]


def validate_fibonacci(items):
    """Validate that JS, BV, TC, RR use Fibonacci values. Returns list of warnings."""
    warnings = []
    for item_id, item in items.items():
        for field in FIBONACCI_FIELDS:
            val = item.get(field)
            if val is not None and val != "" and val != 0:
                try:
                    num = float(val)
                    if num > 0 and int(num) == num and int(num) not in FIBONACCI:
                        warnings.append(
                            f"{item_id}: {field}={int(num)} is not Fibonacci "
                            f"(valid: {', '.join(str(f) for f in sorted(FIBONACCI))})"
                        )
                except (ValueError, TypeError):
                    pass
    return warnings


# -- Path Resolution ----------------------------------------------------------

def find_repo_root():
    """Walk up from CWD to find the repo root (contains .edpa/)."""
    p = Path.cwd()
    while p != p.parent:
        if (p / ".edpa").is_dir():
            return p
        p = p.parent
    return None


# -- Data Loading / Writing ----------------------------------------------------

def load_yaml(path):
    """Load a YAML file. Returns parsed content, or None if missing/unparseable.

    Errors print to stderr so the normal stdout output (which downstream
    tooling may consume) stays clean. Specific exceptions only —
    KeyboardInterrupt / SystemExit propagate.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        return None
    except (yaml.YAMLError, OSError) as exc:
        print(f"WARNING: load_yaml({path}) failed: {exc}", file=sys.stderr)
        return None


def save_yaml(path, data):
    """Write a YAML file preserving readability."""
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False, width=120)


def load_json(path):
    """Load a JSON file. Returns parsed content, or None if missing/unparseable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: load_json({path}) failed: {exc}", file=sys.stderr)
        return None


def save_json(path, data):
    """Write a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def append_jsonl(path, entry):
    """Append a single JSONL entry."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_jsonl(path):
    """Read all JSONL entries."""
    entries = []
    if not path.exists():
        return entries
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


# -- Config Loading ------------------------------------------------------------

DEFAULT_SYNC_CONFIG = {
    "github_org": "YOUR_ORG",
    "github_project_number": 1,
    "sync_interval": "15m",
    "auto_commit": True,
    "fields_mapping": {
        "js": "Job Size",
        "bv": "Business Value",
        "tc": "Time Criticality",
        "rr": "Risk Reduction",
        "wsjf": "WSJF Score",
        "iteration": "Iteration",
        "team": "Team",
    },
}


def load_sync_config(root):
    """Load sync configuration from .edpa/config/edpa.yaml."""
    config_path = root / ".edpa" / "config" / "edpa.yaml"
    if not config_path.exists():
        return DEFAULT_SYNC_CONFIG
    config = load_yaml(config_path)
    return config.get("sync", DEFAULT_SYNC_CONFIG)


# -- Backlog Helpers -----------------------------------------------------------

TYPE_DIRS = ["initiatives", "epics", "features", "stories"]


def collect_items_flat(root):
    """Collect all items from per-file .edpa/backlog/ directories into a flat dict keyed by ID.

    Reads individual YAML files from .edpa/backlog/initiatives/, .edpa/backlog/epics/,
    .edpa/backlog/features/, and .edpa/backlog/stories/.
    """
    items = {}
    backlog = root / ".edpa" / "backlog"
    for type_dir in TYPE_DIRS:
        dir_path = backlog / type_dir
        if not dir_path.exists():
            continue
        for f in sorted(dir_path.glob("*.yaml")):
            item = load_yaml(f)
            if not item:
                continue
            item_id = item.get("id")
            if not item_id:
                continue
            entry = {
                "level": item.get("type", ""),
                "title": item.get("title", ""),
                "status": item.get("status", ""),
                "parent": item.get("parent") or "",
                "owner": item.get("owner", ""),
                "assignee": item.get("assignee", ""),
                "iteration": item.get("iteration", ""),
                "js": item.get("js", 0),
                "bv": item.get("bv", 0),
                "tc": item.get("tc", 0),
                "rr": item.get("rr", 0),
                "wsjf": item.get("wsjf", 0),
                "type": item.get("epic_type", ""),
            }
            items[item_id] = entry
    return items


def compute_backlog_checksum(root):
    """Compute a deterministic checksum for the backlog content."""
    items = collect_items_flat(root)
    serialized = json.dumps(items, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()[:12]


# -- GitHub CLI Interface ------------------------------------------------------

SYNC_FIELDS = ["js", "bv", "tc", "rr", "wsjf", "iteration", "status"]


def gh_fetch_project_items(sync_config):
    """Fetch project items via `gh project item-list`."""
    org = sync_config.get("github_org", "YOUR_ORG")
    project_num = sync_config.get("github_project_number", 1)

    cmd = [
        "gh", "project", "item-list", str(project_num),
        "--owner", org,
        "--format", "json",
        "--limit", "500",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(color(f"  Error: gh CLI failed: {result.stderr.strip()}", C.ERR))
            return None
        return json.loads(result.stdout)
    except FileNotFoundError:
        print(color("  Error: `gh` CLI not found. Install from https://cli.github.com/", C.ERR))
        return None
    except subprocess.TimeoutExpired:
        print(color("  Error: gh CLI timed out after 30s", C.ERR))
        return None
    except json.JSONDecodeError:
        print(color("  Error: Could not parse gh output as JSON", C.ERR))
        return None


def gh_update_project_item(sync_config, item_id, project_id, field_id, value):
    """Update a single TEXT field on a project item.

    Legacy helper kept for callers that already have project_id+field_id.
    For typed updates (NUMBER, SINGLE_SELECT) prefer gh_set_field_value.
    """
    cmd = [
        "gh", "project", "item-edit",
        "--id", item_id,
        "--project-id", project_id,
        "--field-id", field_id,
        "--text", str(value),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# -- Real GitHub Helpers (push to live GH) ------------------------------------

LEVEL_TO_STATUS_FIELD = {
    "Initiative": "Initiative Status",
    "Epic": "Epic Status",
    "Feature": "Feature Status",
    "Story": "Story Status",
}

NUMBER_FIELDS = {
    "js": "Job Size",
    "bv": "Business Value",
    "tc": "Time Criticality",
    "rr": "Risk Reduction",
    "wsjf": "WSJF Score",
}


def load_people_handles(root):
    """Return a dict mapping internal person IDs → GitHub handles, from people.yaml.

    Internal IDs without a `github:` field are silently omitted; sync push then
    falls back to recording the assignee in the issue body only.
    """
    people_path = root / ".edpa" / "config" / "people.yaml"
    if not people_path.exists():
        return {}
    doc = load_yaml(people_path) or {}
    out = {}
    for p in doc.get("people", []) or []:
        pid = p.get("id")
        gh = p.get("github")
        if pid and gh:
            out[pid] = gh
    return out


def load_setup_state(root):
    """Load persisted GitHub state populated by project_setup.py.

    Returns dict with keys: field_ids, option_ids, issue_map, project_id, project_number, repo, org.
    Returns None if setup state is missing (push to real GH then refuses).
    """
    config_path = root / ".edpa" / "config" / "edpa.yaml"
    if not config_path.exists():
        return None
    config = load_yaml(config_path) or {}
    sync = config.get("sync", {}) or {}
    field_ids = sync.get("field_ids") or {}
    option_ids = sync.get("option_ids") or {}

    issue_map = {}
    issue_map_path = root / ".edpa" / "config" / "issue_map.yaml"
    if issue_map_path.exists():
        data = load_yaml(issue_map_path) or {}
        issue_map = data.get("items") or {}

    return {
        "org": sync.get("github_org", ""),
        "repo": sync.get("github_repo", ""),
        "project_id": sync.get("github_project_id", ""),
        "project_number": sync.get("github_project_number", 0),
        "field_ids": field_ids,
        "option_ids": option_ids,
        "issue_map": issue_map,
    }


def save_issue_map(root, state):
    """Persist issue_map back to .edpa/config/issue_map.yaml."""
    issue_map_path = root / ".edpa" / "config" / "issue_map.yaml"
    issue_map_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "github_repo": f"{state['org']}/{state['repo']}" if state.get("org") and state.get("repo") else "",
        "github_project_number": state.get("project_number", 0),
        "items": state.get("issue_map", {}),
    }
    save_yaml(issue_map_path, payload)


def gh_set_field_value(state, project_item_id, edpa_field, value, item_level):
    """Set a single field value on a project item using correct GH typing.

    Routes:
      - js/bv/tc/rr/wsjf -> --number
      - status           -> --single-select-option-id (per-level field)
      - title            -> not handled here (use gh issue edit)
      - other            -> --text fallback
    Returns (ok: bool, message: str).
    """
    project_id = state.get("project_id", "")
    field_ids = state.get("field_ids") or {}
    option_ids = state.get("option_ids") or {}

    if not project_id:
        return False, "no project_id in setup state"

    # Number fields
    if edpa_field in NUMBER_FIELDS:
        gh_name = NUMBER_FIELDS[edpa_field]
        fid = field_ids.get(gh_name)
        if not fid:
            return False, f"no field_id for '{gh_name}'"
        try:
            num = float(value)
        except (TypeError, ValueError):
            return False, f"value {value!r} not numeric"
        cmd = [
            "gh", "project", "item-edit",
            "--id", project_item_id,
            "--project-id", project_id,
            "--field-id", fid,
            "--number", str(num),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            return True, "ok"
        return False, result.stderr.strip()[:120] or "gh failed"

    # Status (per-level single-select)
    if edpa_field == "status":
        status_field = LEVEL_TO_STATUS_FIELD.get(item_level)
        if not status_field:
            return False, f"unknown level {item_level!r} for status"
        fid = field_ids.get(status_field)
        if not fid:
            return False, f"no field_id for '{status_field}'"
        opt_id = option_ids.get(f"{status_field}:{value}")
        if not opt_id:
            return False, f"no option_id for '{status_field}:{value}'"
        cmd = [
            "gh", "project", "item-edit",
            "--id", project_item_id,
            "--project-id", project_id,
            "--field-id", fid,
            "--single-select-option-id", opt_id,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            return True, "ok"
        return False, result.stderr.strip()[:120] or "gh failed"

    # Team or Iteration (single-select)
    if edpa_field in ("team", "iteration"):
        gh_name = "Team" if edpa_field == "team" else "Iteration"
        fid = field_ids.get(gh_name)
        if not fid:
            return False, f"no field_id for {gh_name!r}"
        opt_id = option_ids.get(f"{gh_name}:{value}")
        if not opt_id:
            return False, f"no option_id for {gh_name!r}:{value!r}"
        cmd = [
            "gh", "project", "item-edit",
            "--id", project_item_id,
            "--project-id", project_id,
            "--field-id", fid,
            "--single-select-option-id", opt_id,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            return True, "ok"
        return False, result.stderr.strip()[:120] or "gh failed"

    # title is handled at the issue level, not here
    if edpa_field == "title":
        return False, "use gh_edit_issue_title for titles"

    return False, f"unsupported field {edpa_field!r}"


def gh_edit_issue_title(state, issue_number, new_title):
    """Edit issue title via gh issue edit."""
    if not (state.get("org") and state.get("repo") and issue_number):
        return False, "missing org/repo/issue_number"
    cmd = [
        "gh", "issue", "edit", str(issue_number),
        "--repo", f"{state['org']}/{state['repo']}",
        "--title", str(new_title),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return (result.returncode == 0, result.stderr.strip()[:120] or "ok")


def gh_set_issue_assignee(state, issue_number, gh_login, prev_login=None):
    """Replace the issue's assignee with `gh_login` (or clear it if None).

    Uses `gh issue edit --add-assignee/--remove-assignee`. Returns (ok, msg).
    """
    if not (state.get("org") and state.get("repo") and issue_number):
        return False, "missing org/repo/issue_number"
    repo = f"{state['org']}/{state['repo']}"
    cmd = ["gh", "issue", "edit", str(issue_number), "--repo", repo]
    if prev_login:
        cmd += ["--remove-assignee", prev_login]
    if gh_login:
        cmd += ["--add-assignee", gh_login]
    if len(cmd) == 5:  # nothing to add or remove
        return True, "no-op"
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if result.returncode == 0:
        return True, "ok"
    return False, result.stderr.strip()[:120] or "gh failed"


def gh_close_issue(state, issue_number):
    """Close an issue."""
    cmd = [
        "gh", "issue", "close", str(issue_number),
        "--repo", f"{state['org']}/{state['repo']}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return result.returncode == 0


def gh_reopen_issue(state, issue_number):
    """Reopen a closed issue."""
    cmd = [
        "gh", "issue", "reopen", str(issue_number),
        "--repo", f"{state['org']}/{state['repo']}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return result.returncode == 0


def gh_get_issue_type_ids(org):
    """Query org-level issue type IDs (Initiative/Epic/Feature/Story)."""
    query = f'{{ organization(login: "{org}") {{ issueTypes(first: 20) {{ nodes {{ id name }} }} }} }}'
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    nodes = (((data.get("data") or {}).get("organization") or {}).get("issueTypes") or {}).get("nodes") or []
    return {n["name"]: n["id"] for n in nodes if n.get("name") and n.get("id")}


def gh_create_issue(state, item, item_level, people_handles=None):
    """Create a GH issue for a local-only EDPA item, add to project, set issue type.

    If `people_handles` maps the item's assignee to a GitHub login, the issue is
    created with `--assignee <login>`. Otherwise the assignee is recorded in the
    body only. Returns dict with issue_number, project_item_id, node_id on success.
    """
    repo = f"{state['org']}/{state['repo']}"
    item_id = item.get("id") or item.get("_id") or ""
    title = item.get("title", "")
    full_title = f"{item_id}: {title}" if item_id else title

    body_parts = [item_level]
    for k in ("js", "bv", "tc", "rr", "wsjf"):
        v = item.get(k)
        if v:
            body_parts.append(f"{k.upper()}={v}")
    if item.get("assignee"):
        body_parts.append(f"owner={item['assignee']}")
    if item.get("iteration"):
        body_parts.append(f"iteration={item['iteration']}")
    body = ", ".join(body_parts)

    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", full_title,
        "--body", body,
    ]
    if item.get("epic_type") == "Enabler":
        cmd += ["--label", "Enabler"]

    # Resolve internal assignee ID -> GitHub login if a mapping was supplied
    handles = people_handles or {}
    assignee_internal = item.get("assignee") or item.get("owner")
    gh_login = handles.get(assignee_internal) if assignee_internal else None
    if gh_login:
        cmd += ["--assignee", gh_login]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return None

    issue_url = result.stdout.strip()
    try:
        issue_num = int(issue_url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError):
        return None

    # Resolve issue node_id
    node_q = (
        f'{{ repository(owner: "{state["org"]}", name: "{state["repo"]}") '
        f'{{ issue(number: {issue_num}) {{ id }} }} }}'
    )
    node_result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={node_q}"],
        capture_output=True, text=True, timeout=20,
    )
    node_id = ""
    if node_result.returncode == 0:
        try:
            d = json.loads(node_result.stdout)
            node_id = (((d.get("data") or {}).get("repository") or {}).get("issue") or {}).get("id", "") or ""
        except json.JSONDecodeError:
            pass

    # Assign issue type via GraphQL
    type_ids = gh_get_issue_type_ids(state["org"])
    type_id = type_ids.get(item_level)
    if type_id and node_id:
        mutation = (
            f'mutation {{ updateIssueIssueType(input: '
            f'{{ issueId: "{node_id}", issueTypeId: "{type_id}" }}) '
            f'{{ issue {{ id }} }} }}'
        )
        subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={mutation}"],
            capture_output=True, text=True, timeout=20,
        )

    # Add to project
    add_cmd = [
        "gh", "project", "item-add", str(state["project_number"]),
        "--owner", state["org"],
        "--url", issue_url,
        "--format", "json",
    ]
    add_result = subprocess.run(add_cmd, capture_output=True, text=True, timeout=30)
    project_item_id = ""
    if add_result.returncode == 0:
        try:
            project_item_id = (json.loads(add_result.stdout) or {}).get("id", "") or ""
        except json.JSONDecodeError:
            pass

    return {
        "issue_number": issue_num,
        "project_item_id": project_item_id,
        "node_id": node_id,
    }


def gh_link_subissue(state, parent_node_id, child_node_id):
    """Link a child issue to its parent via GraphQL addSubIssue.

    Thin shim over the shared `_sub_issue_linker` helper so that
    project_setup.py STEP 8 (initial bulk link) and sync.py push
    (incremental link) share one implementation. ``state`` is
    accepted but unused for backward compatibility with existing
    callers.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _sub_issue_linker import link_sub_issue  # noqa: E402

    if not (parent_node_id and child_node_id):
        return False
    ok, _msg = link_sub_issue(parent_node_id, child_node_id)
    return ok


def parse_gh_item_type(item):
    """Determine EDPA item type from GitHub Issue Type, labels, or title prefix."""
    # 1. Check native Issue Type (preferred)
    issue_type = item.get("issueType", {})
    if isinstance(issue_type, dict) and issue_type.get("name"):
        return issue_type["name"]

    # 2. Fallback to labels (backward compat)
    labels = []
    if isinstance(item.get("labels"), list):
        labels = [l.lower() if isinstance(l, str) else l.get("name", "").lower()
                  for l in item["labels"]]
    elif isinstance(item.get("labels"), str):
        labels = [item["labels"].lower()]

    for label in labels:
        if "initiative" in label:
            return "Initiative"
        if "epic" in label:
            return "Epic"
        if "feature" in label:
            return "Feature"
        if "story" in label:
            return "Story"

    # 3. Fallback to title prefix (I-, E-, F-, S-)
    title = item.get("title", "")
    if title.startswith("I-") or "initiative" in title.lower():
        return "Initiative"
    if title.startswith("E-") or "epic" in title.lower():
        return "Epic"
    if title.startswith("F-") or "feature" in title.lower():
        return "Feature"
    return "Story"


def map_gh_items_to_edpa(gh_data, fields_mapping):
    """Map GitHub Project items to EDPA flat item dict."""
    items = {}
    if not gh_data or "items" not in gh_data:
        return items

    # Build reverse lookup: "job size" -> "js", "business value" -> "bv", ...
    reverse_fields = {v.lower(): k for k, v in fields_mapping.items()}
    numeric_fields = {"js", "bv", "tc", "rr", "wsjf"}

    for gh_item in gh_data["items"]:
        title = gh_item.get("title", "")

        # Extract EDPA ID from title: "S-200: OMOP parser impl." or "S-200 title"
        edpa_id = None
        content = gh_item.get("content", {}) or {}

        for prefix in ("I-", "E-", "F-", "S-"):
            if title.startswith(prefix):
                parts = title.split(" ", 1)
                candidate = parts[0].rstrip(":")
                if len(candidate) > 2 and candidate[2:].isdigit():
                    edpa_id = candidate
                    title = parts[1].lstrip(": ") if len(parts) > 1 else title
                    break

        if not edpa_id:
            continue

        item_type = parse_gh_item_type(gh_item)

        # Per-level typed status field name (e.g., "Initiative Status", "Story Status")
        typed_status_key_lower = f"{item_type.lower()} status"

        entry = {
            "level": item_type,
            "title": title,
            # Default to GH's built-in Status; will be overridden below by typed status if present
            "status": gh_item.get("status", ""),
            "_gh_item_id": gh_item.get("id", ""),
        }

        # Map fields by checking both mapped names and direct EDPA key names
        for gh_field_name, value in gh_item.items():
            if gh_field_name in ("id", "title", "status", "labels", "content", "fieldValues"):
                continue

            key_lower = gh_field_name.lower()

            # Per-level typed status field overrides default Status
            if key_lower == typed_status_key_lower and value:
                entry["status"] = value
                continue

            edpa_key = None

            # Check reverse mapping first (e.g., "job size" -> "js")
            if key_lower in reverse_fields:
                edpa_key = reverse_fields[key_lower]
            # Check if it's already an EDPA key name (e.g., "js", "bv", ...)
            elif key_lower in ("js", "bv", "tc", "rr", "wsjf", "iteration",
                               "assignee", "owner", "team"):
                edpa_key = key_lower

            if edpa_key and value is not None and value != "":
                if edpa_key in numeric_fields:
                    try:
                        entry[edpa_key] = float(value)
                    except (ValueError, TypeError):
                        entry[edpa_key] = value
                else:
                    entry[edpa_key] = value

        # Also check nested fieldValues (GraphQL format)
        field_values = gh_item.get("fieldValues", {})
        if isinstance(field_values, dict):
            for field_obj in field_values.get("nodes", []):
                field_name = (field_obj.get("field", {}).get("name", "") or "").lower()
                val = field_obj.get("text") or field_obj.get("name") or field_obj.get("number")
                # Per-level typed status field overrides default Status
                if field_name == typed_status_key_lower and val:
                    entry["status"] = val
                    continue
                edpa_key = reverse_fields.get(field_name)
                if not edpa_key:
                    continue
                if val is not None:
                    if edpa_key in numeric_fields:
                        try:
                            entry[edpa_key] = float(val)
                        except (ValueError, TypeError):
                            entry[edpa_key] = val
                    else:
                        entry[edpa_key] = val

        items[edpa_id] = entry

    return items


# -- Mock Data Generator -------------------------------------------------------

def generate_mock_gh_data(root, fields_mapping=None):
    """Generate fake GitHub Project data from existing .edpa/ item files for testing.

    Produces data in the same shape that `gh project item-list --format json`
    returns, using mapped field names so `map_gh_items_to_edpa` can round-trip.
    """
    if fields_mapping is None:
        fields_mapping = DEFAULT_SYNC_CONFIG["fields_mapping"]

    items = collect_items_flat(root)
    gh_items = []

    for item_id, item in items.items():
        gh_item = {
            "id": f"PVTI_mock_{item_id}",
            "title": f"{item_id}: {item['title']}",
            "status": item.get("status", ""),
            "issueType": {"name": item["level"].capitalize()},
            "labels": [item["level"].lower()],
        }

        # Add custom fields using the mapped GitHub field names
        for edpa_key, gh_name in fields_mapping.items():
            val = item.get(edpa_key)
            if val is not None and val != "" and val != 0:
                gh_item[gh_name] = val

        # Also include assignee and iteration as direct fields
        if item.get("iteration"):
            gh_item["Iteration"] = item["iteration"]
        if item.get("assignee"):
            gh_item["assignee"] = item["assignee"]
        if item.get("owner"):
            gh_item["owner"] = item["owner"]

        gh_items.append(gh_item)

    # Simulate some "remote" changes for diff demonstration
    for gh_item in gh_items:
        if "S-221" in gh_item.get("title", ""):
            gh_item["status"] = "Done"
            break

    return {"items": gh_items}


# -- Diff Engine ---------------------------------------------------------------

def compute_diff(local_items, remote_items):
    """
    Compare local (.edpa/backlog/) and remote (GitHub Project) items.
    Returns a list of change dicts.
    """
    changes = []
    all_ids = set(local_items.keys()) | set(remote_items.keys())

    for item_id in sorted(all_ids):
        local = local_items.get(item_id)
        remote = remote_items.get(item_id)

        if local and not remote:
            changes.append({
                "id": item_id,
                "action": "local_only",
                "detail": f"Exists in .edpa/backlog/ but not in GitHub Project",
                "local": local,
            })
            continue

        if remote and not local:
            changes.append({
                "id": item_id,
                "action": "remote_only",
                "detail": f"Exists in GitHub Project but not in .edpa/backlog/",
                "remote": remote,
            })
            continue

        # Both exist -- compare fields
        compare_fields = ["status", "title", "js", "bv", "tc", "rr", "wsjf",
                          "iteration", "assignee", "owner"]
        for field in compare_fields:
            local_val = local.get(field, "")
            remote_val = remote.get(field, "")
            # Normalize: treat None and "" as equivalent
            if not local_val:
                local_val = ""
            if not remote_val:
                remote_val = ""
            # Normalize numeric comparisons
            if isinstance(local_val, (int, float)) and isinstance(remote_val, (int, float)):
                if abs(float(local_val) - float(remote_val)) < 0.01:
                    continue
            elif str(local_val) == str(remote_val):
                continue

            # Optional fields not yet present on GH should not wipe local
            # values. Iteration is created lazily during setup; without this
            # guard, every pull would clear local iteration tags whenever the
            # GH field is missing.
            if field == "iteration" and not remote_val and local_val:
                continue

            changes.append({
                "id": item_id,
                "action": "field_changed",
                "field": field,
                "local_val": local_val,
                "remote_val": remote_val,
                "level": local.get("level", remote.get("level", "?")),
            })

    return changes


LEVEL_TO_DIR = {
    "Initiative": "initiatives",
    "Epic": "epics",
    "Feature": "features",
    "Story": "stories",
}

ID_PREFIX_TO_DIR = {
    "I": "initiatives",
    "E": "epics",
    "F": "features",
    "S": "stories",
}


def _item_file_path(root, item_id):
    """Resolve the .edpa/backlog/ file path for a given item ID (e.g., S-200 -> .edpa/backlog/stories/S-200.yaml)."""
    prefix = item_id.split("-")[0] if "-" in item_id else ""
    type_dir = ID_PREFIX_TO_DIR.get(prefix)
    if type_dir:
        return root / ".edpa" / "backlog" / type_dir / f"{item_id}.yaml"
    return None


def apply_remote_changes(root, changes):
    """
    Apply remote (GitHub) changes into individual .edpa/ item files.
    Returns applied_count.

    Finds the per-item YAML file by ID, loads it, updates the field, and writes back.
    """
    applied = 0
    updatable_fields = {"status", "js", "bv", "tc", "rr", "wsjf", "owner",
                        "assignee", "iteration", "title"}

    for change in changes:
        if change["action"] != "field_changed":
            continue

        item_id = change["id"]
        field = change["field"]
        new_value = change["remote_val"]

        item_path = _item_file_path(root, item_id)
        if not item_path or not item_path.exists():
            continue

        item = load_yaml(item_path)
        if not item:
            continue

        if field in item or field in updatable_fields:
            item[field] = new_value
            save_yaml(item_path, item)
            applied += 1

    return applied


# -- Changelog Helpers ---------------------------------------------------------

def log_change(root, source, action, item_id, field="", old="", new="", actor="sync-bot"):
    """Append a change entry to the changelog."""
    changelog_path = root / ".edpa" / "changelog.jsonl"
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "action": action,
        "item": item_id,
    }
    if field:
        entry["field"] = field
    if old:
        entry["old"] = str(old)
    if new:
        entry["new"] = str(new)
    entry["actor"] = actor

    append_jsonl(changelog_path, entry)


def _files_changed_since(root, since_ts):
    """Return relative paths under .edpa/backlog/ that have a commit
    touching them since `since_ts` (ISO timestamp). Used by sync
    conflicts to know which items have local edits that haven't been
    pushed yet, so we can flag a same-field local-vs-remote drift even
    when the remote change skipped the changelog (direct GH UI / API).
    """
    if not since_ts:
        return set()
    try:
        result = subprocess.run(
            ["git", "log", f"--since={since_ts}", "--name-only",
             "--pretty=format:", "--", ".edpa/backlog/"],
            cwd=str(root), capture_output=True, text=True, check=False,
        )
    except (FileNotFoundError, OSError):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def update_sync_state(root, direction, items_count, checksum):
    """Update the sync state file."""
    state_path = root / ".edpa" / "sync_state.json"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    state = {}
    if state_path.exists():
        try:
            state = load_json(state_path)
        except (json.JSONDecodeError, FileNotFoundError):
            state = {}

    if direction == "pull":
        state["last_pull"] = now
    elif direction == "push":
        state["last_push"] = now

    state["items_synced"] = items_count
    state["checksum"] = checksum

    save_json(state_path, state)


# -- Commands ------------------------------------------------------------------

def cmd_pull(root, sync_config, args):
    """Pull changes from GitHub Projects into .edpa/ item files."""
    print()
    print(bold(color(f"  {SYNC_ICON} EDPA Sync: Pull (GitHub Projects {ARROW} .edpa/backlog/ items)", C.HEADER)))
    print()

    # Fetch remote data
    fields_mapping = sync_config.get("fields_mapping", DEFAULT_SYNC_CONFIG["fields_mapping"])
    if args.mock:
        print(color("  [mock] Generating simulated GitHub Project data...", C.MUTED))
        gh_data = generate_mock_gh_data(root, fields_mapping)
    else:
        org = sync_config.get("github_org", "YOUR_ORG")
        project_num = sync_config.get("github_project_number", 1)
        print(color(f"  Fetching project items from {org}/project#{project_num}...", C.SYNC))
        gh_data = gh_fetch_project_items(sync_config)
        if gh_data is None:
            print(color("  Pull aborted: could not fetch GitHub Project data.", C.ERR))
            sys.exit(1)

    # Map to EDPA format
    remote_items = map_gh_items_to_edpa(gh_data, fields_mapping)
    local_items = collect_items_flat(root)

    print(color(f"  Remote items: {len(remote_items)}", C.MUTED))
    print(color(f"  Local items:  {len(local_items)}", C.MUTED))

    # Validate Fibonacci values
    fib_warnings = validate_fibonacci(remote_items)
    if fib_warnings:
        print()
        print(color(f"  {CROSS} Fibonacci validation warnings ({len(fib_warnings)}):", C.WARN))
        for w in fib_warnings:
            print(color(f"    {DOT} {w}", C.WARN))
    print()

    # Compute diff
    changes = compute_diff(local_items, remote_items)
    field_changes = [c for c in changes if c["action"] == "field_changed"]

    if not field_changes:
        print(color(f"  {CHECK} No changes to apply. Backlog is up to date.", C.OK))
        update_sync_state(root, "pull", len(local_items), compute_backlog_checksum(root))
        print()
        return

    # Display changes
    print(color(f"  Changes detected: {len(field_changes)}", C.DIFF_MOD))
    print()

    for ch in field_changes:
        item_id = ch["id"]
        field = ch["field"]
        local_val = ch["local_val"]
        remote_val = ch["remote_val"]
        print(f"    {color(item_id, C.SYNC):18s}  "
              f"{field:12s}  "
              f"{color(str(local_val), C.DIFF_DEL)} {ARROW} {color(str(remote_val), C.DIFF_ADD)}")

    print()

    # Apply changes to individual item files
    applied = apply_remote_changes(root, field_changes)

    # Log changes
    for ch in field_changes:
        log_change(root, "github", "field_change", ch["id"],
                   field=ch["field"], old=str(ch["local_val"]), new=str(ch["remote_val"]))

    # Update sync state
    checksum = compute_backlog_checksum(root)
    update_sync_state(root, "pull", len(local_items), checksum)

    print(color(f"  {CHECK} Applied {applied} changes to .edpa/backlog/ item files", C.OK))

    # Auto-commit if requested
    if args.commit:
        _git_commit(root, f"sync: pull {applied} changes from GitHub Projects")

    print()


def cmd_push(root, sync_config, args):
    """Push changes from .edpa/ item files to GitHub Projects (creates missing issues)."""
    print()
    print(bold(color(f"  {SYNC_ICON} EDPA Sync: Push (.edpa/backlog/ items {ARROW} GitHub Projects)", C.HEADER)))
    print()

    fields_mapping = sync_config.get("fields_mapping", DEFAULT_SYNC_CONFIG["fields_mapping"])
    setup_state = None
    people_handles: dict[str, str] = {}

    if args.mock:
        print(color("  [mock] Generating simulated GitHub Project data...", C.MUTED))
        gh_data = generate_mock_gh_data(root, fields_mapping)
    else:
        setup_state = load_setup_state(root)
        if setup_state is None or not setup_state.get("project_id") or not setup_state.get("field_ids"):
            print(color("  Push aborted: GitHub setup state missing or incomplete.", C.ERR))
            print(color("  Run `project_setup.py` first, or `sync setup-refresh` to rebuild IDs.", C.MUTED))
            sys.exit(1)
        people_handles = load_people_handles(root)
        org = setup_state["org"]
        project_num = setup_state["project_number"]
        print(color(f"  Fetching current state from {org}/project#{project_num}...", C.SYNC))
        gh_data = gh_fetch_project_items({"github_org": org, "github_project_number": project_num})
        if gh_data is None:
            print(color("  Push aborted: could not fetch GitHub Project data.", C.ERR))
            sys.exit(1)

    remote_items = map_gh_items_to_edpa(gh_data, fields_mapping)
    local_items = collect_items_flat(root)
    # Inject id back into each local item dict for create flow
    for iid, it in local_items.items():
        it["id"] = iid

    print(color(f"  Local items:  {len(local_items)}", C.MUTED))
    print(color(f"  Remote items: {len(remote_items)}", C.MUTED))
    print()

    # Compute diff (local is source of truth for push)
    changes = compute_diff(remote_items, local_items)
    field_changes = [c for c in changes if c["action"] == "field_changed"]
    create_changes = [c for c in changes if c["action"] == "remote_only"]
    # Note: "remote_only" from compute_diff(remote, local) means item exists in `local` but not in `remote`

    pushed = 0
    created = 0
    failed = 0

    # ── Create missing issues first (so subsequent field updates can target them)
    if create_changes:
        print(bold(color(f"  Creating {len(create_changes)} new issues on GitHub:", C.DIFF_ADD)))
        for ch in create_changes:
            item_id = ch["id"]
            local = ch["remote"]   # local-only payload (lives in `remote` slot of swapped diff)
            level = local.get("level", "Story")
            print(f"    {color(item_id, C.SYNC):18s}  create {level:10s}  ", end="")
            if args.mock:
                print(f"  {color('[mock: ok]', C.MUTED)}")
                created += 1
                continue
            payload = dict(local)
            payload["id"] = item_id
            result = gh_create_issue(setup_state, payload, level, people_handles)
            if not result:
                print(f"  {color('[failed]', C.ERR)}")
                failed += 1
                continue
            setup_state["issue_map"][item_id] = result
            issue_label = "#" + str(result["issue_number"])
            print(f"  {color(issue_label, C.OK)}")
            created += 1
            log_change(root, "git", "issue_created", item_id,
                       new=f"#{result['issue_number']}", actor="sync-push")

            # Set initial fields on newly created item (number + status + iteration)
            proj_item_id = result.get("project_item_id", "")
            if proj_item_id:
                for fkey in ("js", "bv", "tc", "rr", "wsjf"):
                    val = local.get(fkey)
                    if val:
                        gh_set_field_value(setup_state, proj_item_id, fkey, val, level)
                if local.get("iteration"):
                    gh_set_field_value(setup_state, proj_item_id, "iteration",
                                        local["iteration"], level)
                if local.get("status"):
                    gh_set_field_value(setup_state, proj_item_id, "status", local["status"], level)
                    if local["status"] == "Done":
                        gh_close_issue(setup_state, result["issue_number"])

        # Persist updated issue_map after creates
        if setup_state and not args.mock:
            save_issue_map(root, setup_state)

        # Link parent-child after all issues exist
        if not args.mock:
            link_count = 0
            for ch in create_changes:
                item_id = ch["id"]
                local_item_path = _item_file_path(root, item_id)
                if not local_item_path or not local_item_path.exists():
                    continue
                local_full = load_yaml(local_item_path) or {}
                parent_id = local_full.get("parent")
                if not parent_id:
                    continue
                child = setup_state["issue_map"].get(item_id, {})
                parent = setup_state["issue_map"].get(parent_id, {})
                if gh_link_subissue(setup_state, parent.get("node_id"), child.get("node_id")):
                    link_count += 1
            if link_count:
                print(f"    {color(CHECK, C.OK)} {link_count} sub-issue links created")
        print()

    # ── Apply field changes
    if field_changes:
        print(bold(color(f"  Pushing {len(field_changes)} field changes:", C.DIFF_MOD)))
        for ch in field_changes:
            item_id = ch["id"]
            field = ch["field"]
            old_val = ch["local_val"]   # remote's current value (swapped in compute_diff arg order)
            new_val = ch["remote_val"]  # local's value
            level = local_items.get(item_id, {}).get("level") or ch.get("level") or "Story"

            print(f"    {color(item_id, C.SYNC):18s}  "
                  f"{field:12s}  "
                  f"{color(str(old_val), C.DIFF_DEL)} {ARROW} {color(str(new_val), C.DIFF_ADD)}",
                  end="")

            if args.mock:
                print(f"  {color('[mock: ok]', C.MUTED)}")
                pushed += 1
                continue

            mapping = setup_state["issue_map"].get(item_id) or {}
            proj_item_id = mapping.get("project_item_id", "")
            issue_num = mapping.get("issue_number")

            if not proj_item_id and not issue_num:
                print(f"  {color('[skipped: not in issue_map]', C.WARN)}")
                failed += 1
                continue

            if field == "title" and issue_num:
                ok_, msg = gh_edit_issue_title(setup_state, issue_num, new_val)
                if ok_:
                    print(f"  {color('[ok]', C.OK)}")
                    pushed += 1
                else:
                    print(f"  {color(f'[failed: {msg}]', C.ERR)}")
                    failed += 1
                continue

            if field in ("assignee", "owner") and issue_num:
                new_login = people_handles.get(str(new_val)) if new_val else None
                old_login = people_handles.get(str(old_val)) if old_val else None
                if not new_login and new_val:
                    print(f"  {color(f'[skipped: no GH handle for {new_val!r} in people.yaml]', C.WARN)}")
                    failed += 1
                    continue
                ok_, msg = gh_set_issue_assignee(setup_state, issue_num, new_login, old_login)
                if ok_:
                    print(f"  {color('[ok]', C.OK)}")
                    pushed += 1
                else:
                    print(f"  {color(f'[failed: {msg}]', C.ERR)}")
                    failed += 1
                continue

            ok_, msg = gh_set_field_value(setup_state, proj_item_id, field, new_val, level)
            if ok_:
                print(f"  {color('[ok]', C.OK)}")
                pushed += 1
                # Mirror Done state to issue close/reopen
                if field == "status" and issue_num:
                    if str(new_val).lower() == "done":
                        gh_close_issue(setup_state, issue_num)
                    elif str(old_val).lower() == "done":
                        gh_reopen_issue(setup_state, issue_num)
            else:
                print(f"  {color(f'[failed: {msg}]', C.ERR)}")
                failed += 1
        print()

    if not field_changes and not create_changes:
        print(color(f"  {CHECK} No changes to push. GitHub Project is up to date.", C.OK))
        update_sync_state(root, "push", len(local_items), compute_backlog_checksum(root))
        print()
        return

    # Log changes
    for ch in field_changes:
        log_change(root, "git", "field_change", ch["id"],
                   field=ch["field"], old=str(ch["local_val"]), new=str(ch["remote_val"]))

    update_sync_state(root, "push", len(local_items), compute_backlog_checksum(root))
    print(color(f"  {CHECK} Pushed {pushed} field changes, {created} issues created, {failed} failed", C.OK))

    # Auto-commit issue_map.yaml (and edpa.yaml when push extended it,
    # e.g., new project_item_ids when an issue was created). Same
    # rationale as project_setup STEP 9b — uncommitted state can be
    # silently reverted by a later git checkout / squash merge.
    if not getattr(args, "no_commit", False):
        _commit_sync_state(root,
                           message=f"EDPA sync push: {created} created, {pushed} updated")
    print()


def _commit_sync_state(root, *, message: str):
    """Auto-commit EDPA-managed config / state files. Imported lazily so
    sync.py stays runnable when _auto_commit isn't on the path (e.g.,
    a stripped-down install)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from _auto_commit import maybe_commit
    except ImportError:
        return
    finally:
        sys.path.pop(0)
    result = maybe_commit(
        paths=[
            ".edpa/config/edpa.yaml",
            ".edpa/config/issue_map.yaml",
            ".edpa/iterations",
            ".edpa/sync_state.json",
            ".edpa/changelog.jsonl",
        ],
        message=message,
        root=root,
    )
    if result == "committed":
        print(color(f"  {CHECK} Auto-committed sync state ({message})", C.OK))
    elif result == "skipped":
        print(color("  (auto-commit skipped — not a git repo or "
                    "git user.name / user.email unset)", C.MUTED))


def cmd_setup_refresh(root, _sync_config, _args):
    """Re-discover field_ids, option_ids, and issue_map from existing GH project.

    Use when: setup ran on different machine, IDs were lost, or project was modified manually.
    Requires github_org and github_project_number already in edpa.yaml.
    """
    print()
    print(bold(color(f"  {SYNC_ICON} EDPA Sync: Setup Refresh (rebuild IDs from GitHub)", C.HEADER)))
    print()

    config_path = root / ".edpa" / "config" / "edpa.yaml"
    config = load_yaml(config_path) if config_path.exists() else {}
    sync = (config.get("sync") or {}).copy()
    org = sync.get("github_org")
    project_num = sync.get("github_project_number")
    repo = sync.get("github_repo", "")
    if not org or not project_num:
        print(color("  Error: github_org / github_project_number missing in edpa.yaml.", C.ERR))
        sys.exit(1)

    print(color(f"  Querying {org}/project#{project_num}...", C.SYNC))

    # 1. Field IDs + option IDs
    field_json = subprocess.run(
        ["gh", "project", "field-list", str(project_num), "--owner", org, "--format", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if field_json.returncode != 0:
        print(color(f"  Error: gh project field-list failed: {field_json.stderr.strip()}", C.ERR))
        sys.exit(1)
    fields = (json.loads(field_json.stdout) or {}).get("fields", [])
    field_ids = {f["name"]: f["id"] for f in fields if f.get("name") and f.get("id")}
    option_ids = {}
    for f in fields:
        for opt in f.get("options", []):
            option_ids[f"{f['name']}:{opt['name']}"] = opt["id"]
    print(color(f"  Fields: {len(field_ids)}, Options: {len(option_ids)}", C.MUTED))

    # 2. Project ID
    proj_q = f'{{ organization(login: "{org}") {{ projectV2(number: {project_num}) {{ id }} }} }}'
    proj_result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={proj_q}"],
        capture_output=True, text=True, timeout=20,
    )
    project_id = ""
    if proj_result.returncode == 0:
        try:
            d = json.loads(proj_result.stdout)
            project_id = (((d.get("data") or {}).get("organization") or {}).get("projectV2") or {}).get("id", "")
        except json.JSONDecodeError:
            pass

    # 3. Issue map (from project items)
    item_json = subprocess.run(
        ["gh", "project", "item-list", str(project_num), "--owner", org, "--format", "json", "--limit", "500"],
        capture_output=True, text=True, timeout=60,
    )
    issue_map = {}
    if item_json.returncode == 0:
        try:
            data = json.loads(item_json.stdout) or {}
        except json.JSONDecodeError:
            data = {}
        for item in data.get("items", []):
            title = item.get("title", "")
            edpa_id = None
            for prefix in ("I-", "E-", "F-", "S-"):
                if title.startswith(prefix):
                    parts = title.split(" ", 1)
                    candidate = parts[0].rstrip(":")
                    if len(candidate) > 2 and candidate[2:].isdigit():
                        edpa_id = candidate
                        break
            if not edpa_id:
                continue
            content = item.get("content", {}) or {}
            issue_number = content.get("number")
            if not issue_number:
                continue
            # Resolve issue node_id
            node_id = ""
            if repo:
                node_q = (
                    f'{{ repository(owner: "{org}", name: "{repo.split("/")[-1]}") '
                    f'{{ issue(number: {issue_number}) {{ id }} }} }}'
                )
                node_r = subprocess.run(
                    ["gh", "api", "graphql", "-f", f"query={node_q}"],
                    capture_output=True, text=True, timeout=20,
                )
                if node_r.returncode == 0:
                    try:
                        nd = json.loads(node_r.stdout)
                        node_id = (((nd.get("data") or {}).get("repository") or {}).get("issue") or {}).get("id", "") or ""
                    except json.JSONDecodeError:
                        pass
            issue_map[edpa_id] = {
                "issue_number": int(issue_number),
                "project_item_id": item.get("id", ""),
                "node_id": node_id,
            }
    print(color(f"  Items mapped: {len(issue_map)}", C.MUTED))

    # 4. Persist
    sync["github_project_id"] = project_id
    sync["field_ids"] = field_ids
    sync["option_ids"] = option_ids
    config["sync"] = sync
    save_yaml(config_path, config)

    state = {
        "org": org,
        "repo": repo.split("/")[-1] if "/" in repo else repo,
        "project_number": project_num,
        "issue_map": issue_map,
    }
    save_issue_map(root, state)

    print(color(f"  {CHECK} Setup state refreshed: {len(field_ids)} fields, {len(issue_map)} items mapped", C.OK))

    # Auto-commit recovered state — the entire point of setup-refresh
    # is to rebuild the IDs that someone or something lost. Leaving the
    # rebuilt state uncommitted invites the same regression on the next
    # git checkout. --no-commit on the command line opts out.
    if not getattr(_args, "no_commit", False):
        _commit_sync_state(
            root,
            message=f"EDPA sync setup-refresh: {len(field_ids)} fields, "
                    f"{len(issue_map)} items recovered",
        )
    print()


def cmd_add_iteration(root, _sync_config, args):
    """Append a new iteration option to the GitHub Project Iteration field.

    project_setup.py creates the Iteration SINGLE_SELECT field on first
    run. As new iterations land in `.edpa/iterations/*.yaml` after
    setup, the GitHub field needs the matching options added or
    `sync push` of items with that iteration value will fail with
    "option not found". This subcommand reads the iteration YAML, calls
    GraphQL `updateProjectV2Field` with the merged option list, and
    persists the new option_id back to `edpa.yaml`.

    Usage:
        sync add-iteration PI-2026-1.5
        sync add-iteration PI-2026-1.5 --color BLUE
        sync add-iteration PI-2026-1.5 --dry-run

    Idempotent: running on an iteration whose option already exists is
    a no-op (with a notice).
    """
    iter_id = getattr(args, "iteration_id", None)
    color_name = (getattr(args, "color", None) or "GRAY").upper()
    dry_run = bool(getattr(args, "dry_run", False))

    print()
    print(bold(color(f"  {SYNC_ICON} EDPA Sync: Add Iteration", C.HEADER)))
    print()

    # 1. Validate iteration ID shape — defensive, prevents user typing
    #    "PI 2026 1.5" with spaces or similar from poisoning options.
    if not iter_id or not re.match(r"^[A-Za-z0-9._-]+$", iter_id):
        print(color(f"  Error: invalid iteration id {iter_id!r}", C.ERR))
        print(color("  Expected: PI-YYYY-N.M (alphanumerics, dots, dashes only)", C.MUTED))
        sys.exit(1)

    # 2. The iteration YAML must exist on disk first — this avoids
    #    creating GH options for iterations the engine doesn't know about.
    iter_yaml = root / ".edpa" / "iterations" / f"{iter_id}.yaml"
    if not iter_yaml.is_file():
        print(color(f"  Error: {iter_yaml.relative_to(root)} not found", C.ERR))
        print(color(f"  Create the iteration YAML first, then re-run.", C.MUTED))
        sys.exit(1)

    # 3. Load setup state from edpa.yaml.
    config_path = root / ".edpa" / "config" / "edpa.yaml"
    config = load_yaml(config_path) or {}
    sync = config.get("sync") or {}
    org = sync.get("github_org")
    project_num = sync.get("github_project_number")
    project_id = sync.get("github_project_id")
    iteration_field_id = (sync.get("field_ids") or {}).get("Iteration")
    if not all((org, project_num, project_id, iteration_field_id)):
        print(color("  Error: setup state missing in edpa.yaml.", C.ERR))
        print(color("  Run `project_setup.py` once or `sync setup-refresh` "
                    "to populate field_ids.", C.MUTED))
        sys.exit(1)

    # 4. Already-known option? Idempotent fast path.
    option_ids = sync.get("option_ids") or {}
    key = f"Iteration:{iter_id}"
    if key in option_ids:
        print(color(f"  Option already exists: {key} -> {option_ids[key]}", C.MUTED))
        print(color("  Nothing to do.", C.OK))
        return

    print(color(f"  Iteration:        {iter_id}", C.MUTED))
    print(color(f"  Project:          {org}/project#{project_num}", C.MUTED))
    print(color(f"  Field:            Iteration ({iteration_field_id[:12]}...)", C.MUTED))
    print(color(f"  Color:            {color_name}", C.MUTED))
    print()

    # 5. Fetch the field's current option list. updateProjectV2Field
    #    REPLACES the option list, so we must read-merge-write to avoid
    #    deleting existing options.
    fetch_query = (
        f'query {{ node(id: "{project_id}") {{ ... on ProjectV2 {{ '
        f'field(name: "Iteration") {{ ... on ProjectV2SingleSelectField '
        f'{{ id options {{ id name color description }} }} }} }} }} }}'
    )
    res = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={fetch_query}"],
        capture_output=True, text=True, timeout=20,
    )
    if res.returncode != 0:
        print(color(f"  Error: gh api graphql failed: {res.stderr.strip()}", C.ERR))
        sys.exit(1)
    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        print(color(f"  Error: malformed graphql response ({exc})", C.ERR))
        sys.exit(1)
    field = (((payload.get("data") or {}).get("node") or {}).get("field") or {})
    existing_options = field.get("options") or []

    # 6. Build the new option list. Strip "TBD" placeholder when first
    #    real iteration lands — it has no semantic value once real
    #    iterations exist.
    merged = []
    for opt in existing_options:
        if opt.get("name") == "TBD" and existing_options:  # placeholder, drop it
            continue
        merged.append({
            "name": opt.get("name"),
            "color": (opt.get("color") or "GRAY").upper(),
            "description": opt.get("description") or "",
        })
    merged.append({
        "name": iter_id,
        "color": color_name,
        "description": f"Iteration {iter_id}",
    })

    if dry_run:
        print(color("  [dry-run] Would append:", C.MUTED))
        print(color(f"    {iter_id} ({color_name})", C.OK))
        if any(o.get("name") == "TBD" for o in existing_options):
            print(color("  [dry-run] Would drop placeholder option 'TBD'", C.MUTED))
        print()
        return

    # 7. Apply via updateProjectV2Field. The API expects
    #    `singleSelectOptions: [{ name, color, description }]` and replaces
    #    the entire list. `gh api -f / --raw-field` can only send strings;
    #    for the array-of-objects variable we feed the full JSON-RPC request
    #    on stdin via `--input -`.
    mutation = (
        "mutation($fid:ID!,$opts:[ProjectV2SingleSelectFieldOptionInput!]!){"
        " updateProjectV2Field(input:{fieldId:$fid,singleSelectOptions:$opts}){"
        " projectV2Field { ... on ProjectV2SingleSelectField"
        " { id options { id name } } } } }"
    )
    body = json.dumps({
        "query": mutation,
        "variables": {"fid": iteration_field_id, "opts": merged},
    })
    res = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=body,
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        print(color(f"  Error: updateProjectV2Field failed: {res.stderr.strip()}", C.ERR))
        sys.exit(1)
    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        print(color(f"  Error: malformed mutation response ({exc})", C.ERR))
        sys.exit(1)
    err = payload.get("errors")
    if err:
        print(color(f"  GraphQL error: {err}", C.ERR))
        sys.exit(1)

    new_field = ((payload.get("data") or {}).get("updateProjectV2Field") or {}).get("projectV2Field") or {}
    new_options = {opt["name"]: opt["id"] for opt in (new_field.get("options") or [])}
    new_id = new_options.get(iter_id)
    if not new_id:
        print(color(f"  Warning: option {iter_id} not echoed back. Re-run "
                    f"`sync setup-refresh` to verify.", C.WARN))
        sys.exit(1)

    # 8. Persist option_id back so subsequent `sync push` knows it.
    sync["option_ids"] = sync.get("option_ids") or {}
    # Refresh ALL Iteration:* options at once — TBD may have been dropped,
    # IDs may have rotated upstream.
    for name, oid in new_options.items():
        sync["option_ids"][f"Iteration:{name}"] = oid
    # Drop TBD entry if it lingers in option_ids from a previous setup.
    sync["option_ids"].pop("Iteration:TBD", None)
    config["sync"] = sync
    save_yaml(config_path, config)

    print(color(f"  {CHECK} Added option {iter_id} -> {new_id}", C.OK))
    print(color(f"  Persisted option_ids[\"{key}\"] in edpa.yaml.", C.MUTED))
    print()


def cmd_diff(root, sync_config, args):
    """Show what would change without applying (dry-run)."""
    print()
    print(bold(color(f"  {SYNC_ICON} EDPA Sync: Diff (dry-run)", C.HEADER)))
    print()

    # Fetch remote data
    fields_mapping = sync_config.get("fields_mapping", DEFAULT_SYNC_CONFIG["fields_mapping"])
    if args.mock:
        print(color("  [mock] Generating simulated GitHub Project data...", C.MUTED))
        gh_data = generate_mock_gh_data(root, fields_mapping)
    else:
        org = sync_config.get("github_org", "YOUR_ORG")
        project_num = sync_config.get("github_project_number", 1)
        print(color(f"  Fetching project items from {org}/project#{project_num}...", C.SYNC))
        gh_data = gh_fetch_project_items(sync_config)
        if gh_data is None:
            print(color("  Diff aborted: could not fetch GitHub Project data.", C.ERR))
            sys.exit(1)

    remote_items = map_gh_items_to_edpa(gh_data, fields_mapping)
    local_items = collect_items_flat(root)

    print(color(f"  Remote items: {len(remote_items)}", C.MUTED))
    print(color(f"  Local items:  {len(local_items)}", C.MUTED))
    print()

    changes = compute_diff(local_items, remote_items)

    if not changes:
        print(color(f"  {CHECK} No differences. Everything is in sync.", C.OK))
        print()
        return

    # Group by action type
    field_changes = [c for c in changes if c["action"] == "field_changed"]
    local_only = [c for c in changes if c["action"] == "local_only"]
    remote_only = [c for c in changes if c["action"] == "remote_only"]

    if field_changes:
        print(bold(color("  Field differences:", C.DIFF_MOD)))
        print()
        # Table header
        print(color(f"    {'Item':10s}  {'Field':12s}  {'Local':20s}     {'Remote':20s}", C.MUTED))
        print(color(f"    {DASH * 75}", C.MUTED))
        for ch in field_changes:
            level = ch.get("level", "")
            lc = C.STORY
            if level == "Epic":
                lc = C.EPIC
            elif level == "Feature":
                lc = C.FEAT
            elif level == "Initiative":
                lc = C.INIT

            print(f"    {color(ch['id'], lc):20s}  "
                  f"{ch['field']:12s}  "
                  f"{color(str(ch['local_val']), C.DIFF_DEL):30s} {ARROW}  "
                  f"{color(str(ch['remote_val']), C.DIFF_ADD)}")
        print()

    if local_only:
        print(bold(color("  Local only (not in GitHub Project):", C.DIFF_ADD)))
        for ch in local_only:
            print(f"    {color('+', C.DIFF_ADD)} {ch['id']}: {ch['local'].get('title', '')}")
        print()

    if remote_only:
        print(bold(color("  Remote only (not in .edpa/backlog/):", C.DIFF_DEL)))
        for ch in remote_only:
            print(f"    {color('-', C.DIFF_DEL)} {ch['id']}: {ch['remote'].get('title', '')}")
        print()

    # Summary
    print(color(f"  Summary: {len(field_changes)} field changes, "
                f"{len(local_only)} local-only, {len(remote_only)} remote-only", C.MUTED))
    print()


def cmd_log(root, _sync_config, args):
    """Show the sync changelog."""
    print()
    print(bold(color("  EDPA Sync Changelog", C.HEADER)))
    print()

    changelog_path = root / ".edpa" / "changelog.jsonl"
    entries = load_jsonl(changelog_path)

    if not entries:
        print(color("  No changelog entries yet.", C.MUTED))
        print()
        return

    # Show last N entries (default 20)
    limit = getattr(args, "limit", 20) or 20
    entries = entries[-limit:]

    # Table header
    print(color(f"    {'Timestamp':22s}  {'Source':8s}  {'Action':15s}  {'Item':8s}  "
                f"{'Field':12s}  {'Change':30s}  {'Actor':10s}", C.MUTED))
    print(color(f"    {DASH * 115}", C.MUTED))

    for entry in reversed(entries):
        ts = entry.get("ts", "")[:19]
        source = entry.get("source", "?")
        action = entry.get("action", "?")
        item = entry.get("item", "?")
        field = entry.get("field", "")
        old = entry.get("old", "")
        new = entry.get("new", "")
        actor = entry.get("actor", "?")

        source_color = C.SYNC if source == "github" else C.OK
        change_str = ""
        if old and new:
            change_str = f"{old} {ARROW} {new}"
        elif new:
            change_str = f"{ARROW} {new}"

        print(f"    {color(ts, C.MUTED):32s}  "
              f"{color(source, source_color):18s}  "
              f"{action:15s}  "
              f"{color(item, C.STORY):18s}  "
              f"{field:12s}  "
              f"{change_str:30s}  "
              f"{color(actor, C.DIM)}")

    print()
    print(color(f"  Showing last {len(entries)} of {len(load_jsonl(changelog_path))} entries", C.MUTED))
    print()


def resolve_conflicts(github_changes, git_changes, strategy):
    """Pure function: choose a winner per (item, field) under the given strategy.

    Inputs are dicts of item_id -> list[changelog entry], one per source.
    Returns a list of resolution dicts:
        {item_id, field, winner: 'local'|'remote', value, ts, reason}
    `winner == 'local'` means the local (git/YAML) value should be pushed to
    GitHub; `winner == 'remote'` means GitHub's value should be pulled into the
    local YAML.

    Strategies:
        local-wins       -> always 'local' (newest local value wins)
        remote-wins      -> always 'remote'
        last-write-wins  -> winner is whichever side has the most recent ts
        report           -> no winner picked; reason='manual'
    """
    if strategy not in ("local-wins", "remote-wins", "last-write-wins", "report"):
        raise ValueError(f"unknown strategy: {strategy}")

    plan = []
    conflict_ids = set(github_changes) & set(git_changes)
    for item_id in sorted(conflict_ids):
        # Group entries per field within each source
        gh_by_field: dict[str, list] = {}
        for e in github_changes[item_id]:
            gh_by_field.setdefault(e.get("field", ""), []).append(e)
        git_by_field: dict[str, list] = {}
        for e in git_changes[item_id]:
            git_by_field.setdefault(e.get("field", ""), []).append(e)

        all_fields = set(gh_by_field) | set(git_by_field)
        for field in sorted(all_fields):
            gh_es = gh_by_field.get(field, [])
            git_es = git_by_field.get(field, [])
            if not (gh_es and git_es):
                # Only one side touched this field — no conflict on this field
                continue
            # Most-recent entry per side
            gh_last = max(gh_es, key=lambda e: e.get("ts", ""))
            git_last = max(git_es, key=lambda e: e.get("ts", ""))

            if strategy == "report":
                plan.append({"item_id": item_id, "field": field,
                              "winner": None, "value": None,
                              "ts": None, "reason": "manual"})
                continue
            if strategy == "local-wins":
                winner, value, ts = "local", git_last.get("new", ""), git_last.get("ts", "")
                reason = "strategy: local-wins"
            elif strategy == "remote-wins":
                winner, value, ts = "remote", gh_last.get("new", ""), gh_last.get("ts", "")
                reason = "strategy: remote-wins"
            else:  # last-write-wins
                if git_last.get("ts", "") >= gh_last.get("ts", ""):
                    winner, value, ts = "local", git_last.get("new", ""), git_last.get("ts", "")
                else:
                    winner, value, ts = "remote", gh_last.get("new", ""), gh_last.get("ts", "")
                reason = "strategy: last-write-wins"
            plan.append({"item_id": item_id, "field": field,
                          "winner": winner, "value": value,
                          "ts": ts, "reason": reason})
    return plan


def cmd_conflicts(root, sync_config, args):
    """Show items changed in both sources, optionally auto-resolve.

    --strategy report           (default)  list conflicts, do nothing
    --strategy local-wins                  always pick local YAML value
    --strategy remote-wins                 always pick GitHub value
    --strategy last-write-wins             pick the most recent timestamp
    --apply                                actually apply the chosen winners
                                            (without --apply, shows the plan)
    """
    print()
    print(bold(color(f"  {SYNC_ICON} EDPA Sync: Conflicts", C.HEADER)))
    print()

    strategy = getattr(args, "strategy", "report") or "report"
    apply_changes = bool(getattr(args, "apply", False))

    state_path = root / ".edpa" / "sync_state.json"
    if not state_path.exists():
        print(color("  No sync state found. Run `pull` or `push` first.", C.WARN))
        print()
        return

    state = load_json(state_path)
    last_pull = state.get("last_pull", "")
    last_push = state.get("last_push", "")

    changelog_path = root / ".edpa" / "changelog.jsonl"
    entries = load_jsonl(changelog_path)

    if not (last_pull or last_push):
        print(color("  No sync history. Cannot detect conflicts.", C.WARN))
        print()
        return

    # A real conflict is: local changed since the last *push* AND remote
    # changed since the last *pull*. Using max(last_pull, last_push) as the
    # cutoff dropped any local change recorded in the window between push
    # and the next pull, so cross-side conflicts were never detected.
    git_cutoff = last_push or last_pull
    github_cutoff = last_pull or last_push

    github_changes: dict[str, list] = {}
    git_changes: dict[str, list] = {}
    for entry in entries:
        ts = entry.get("ts", "")
        item_id = entry.get("item", "")
        source = entry.get("source", "")
        if source == "github" and ts >= github_cutoff:
            github_changes.setdefault(item_id, []).append(entry)
        elif source == "git" and ts >= git_cutoff:
            git_changes.setdefault(item_id, []).append(entry)

    conflict_ids = set(github_changes) & set(git_changes)

    # Augment changelog-based detection with a fresh remote-vs-local diff:
    # changes made directly in the GH UI (or via API) never hit the local
    # changelog, so a same-field conflict like local F-1=Reviewing vs.
    # remote F-1=Implementing would not show up. We flag any field that
    # currently differs *and* has been touched locally since last_pull.
    augmented_ids: set[str] = set()
    augmented_changes: dict[str, list] = {}
    try:
        gh_data = gh_fetch_project_items(sync_config)
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as e:
        print(color(f"  (skipping live diff: {e})", C.MUTED))
        gh_data = None
    if gh_data:
        local_items = collect_items_flat(root)
        fields_mapping = sync_config.get(
            "fields_mapping",
            DEFAULT_SYNC_CONFIG["fields_mapping"],
        )
        remote_items = map_gh_items_to_edpa(gh_data, fields_mapping)
        diff = compute_diff(local_items, remote_items)
        local_recent_files = _files_changed_since(root, last_pull)
        for change in diff:
            if change.get("action") != "field_changed":
                continue
            iid = change["id"]
            if iid in conflict_ids:
                continue
            backlog_path = _item_file_path(root, iid)
            if not backlog_path:
                continue
            rel = str(backlog_path.relative_to(root)) if backlog_path.is_absolute() \
                else str(backlog_path)
            if rel not in local_recent_files:
                continue
            augmented_ids.add(iid)
            augmented_changes.setdefault(iid, []).append({
                "field": change.get("field", "?"),
                "local": change.get("local_val", ""),
                "remote": change.get("remote_val", ""),
            })
    conflict_ids = conflict_ids | augmented_ids

    if not conflict_ids:
        print(color(f"  {CHECK} No conflicts detected.", C.OK))
        print(color(f"  Last pull: {last_pull or '—'}", C.MUTED))
        print(color(f"  Last push: {last_push or '—'}", C.MUTED))
        print()
        return

    print(color(f"  {CROSS} {len(conflict_ids)} items have changes from both sources:", C.ERR))
    print()

    # Always print the raw conflicts first
    for item_id in sorted(conflict_ids):
        print(f"    {bold(color(item_id, C.WARN))}")
        for label, color_code, source_changes in (("GitHub changes:", C.SYNC, github_changes),
                                                    ("Git changes:",    C.OK,   git_changes)):
            entries = source_changes.get(item_id, [])
            if not entries:
                continue
            print(color(f"      {label}", color_code))
            for e in entries:
                f = e.get("field", "?")
                print(f"        {f}: {e.get('old', '')} {ARROW} {e.get('new', '')}"
                      f"  [{e.get('ts', '')[:19]}]")
        if item_id in augmented_changes:
            print(color("      Live diff (no changelog entry from GH UI):", C.SYNC))
            for change in augmented_changes[item_id]:
                print(f"        {change['field']}: {change['local']} (local) "
                      f"{ARROW} {change['remote']} (remote)")
        print()

    if strategy == "report":
        print(color("  Strategy: report (default). Use `--strategy "
                    "local-wins|remote-wins|last-write-wins` to auto-resolve.",
                    C.MUTED))
        print()
        return

    plan = resolve_conflicts(github_changes, git_changes, strategy)
    if not plan:
        print(color("  No resolvable conflicts (only single-source changes).", C.MUTED))
        print()
        return

    print(bold(color(f"  Resolution plan ({strategy}):", C.HEADER)))
    for p in plan:
        arrow_dir = "local→GH" if p["winner"] == "local" else "GH→local"
        print(f"    {p['item_id']:10s}  {p['field']:12s}  "
              f"winner: {p['winner']} ({arrow_dir})  value={p['value']!r}")
    print()

    if not apply_changes:
        print(color("  Dry-run. Re-run with --apply to execute the plan.", C.MUTED))
        print()
        return

    # Apply: 'remote' winners write YAML; 'local' winners push to GH.
    setup_state = load_setup_state(root)
    applied = 0
    failed = 0
    items_by_id = collect_items_flat(root)
    for p in plan:
        item_id = p["item_id"]
        field = p["field"]
        value = p["value"]
        if p["winner"] == "remote":
            item_path = _item_file_path(root, item_id)
            if not item_path or not item_path.exists():
                print(f"    {color(item_id, C.WARN)}: YAML missing")
                failed += 1
                continue
            doc = load_yaml(item_path) or {}
            doc[field] = _coerce_typed(field, value)
            save_yaml(item_path, doc)
            log_change(root, "auto-resolve", "field_change", item_id,
                       field=field, new=str(value), actor=p["reason"])
            applied += 1
        else:  # local winner pushed to GH
            if setup_state is None or not setup_state.get("project_id"):
                print(f"    {color(item_id, C.WARN)}: setup state missing — cannot push to GH")
                failed += 1
                continue
            mapping = setup_state["issue_map"].get(item_id) or {}
            proj_item_id = mapping.get("project_item_id", "")
            level = items_by_id.get(item_id, {}).get("level") or "Story"
            ok_, msg = gh_set_field_value(setup_state, proj_item_id, field, value, level)
            if ok_:
                log_change(root, "auto-resolve", "field_change", item_id,
                           field=field, new=str(value), actor=p["reason"])
                applied += 1
            else:
                print(f"    {color(item_id, C.ERR)}: push failed ({msg})")
                failed += 1
    print()
    print(color(f"  {CHECK} Applied {applied} resolutions, {failed} failed", C.OK))
    print()


def _coerce_typed(field, value):
    """Coerce a stringified changelog value back to its expected type."""
    if field in ("js", "bv", "tc", "rr", "wsjf"):
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    return value


def cmd_status(root, sync_config, args):
    """Show sync status overview."""
    print()
    print(bold(color(f"  {SYNC_ICON} EDPA Sync Status", C.HEADER)))
    print()

    org = sync_config.get("github_org", "YOUR_ORG")
    project_num = sync_config.get("github_project_number", 1)

    print(f"  {bold('Organization:')}     {org}")
    print(f"  {bold('Project:')}          #{project_num}")
    print()

    # Sync state
    state_path = root / ".edpa" / "sync_state.json"
    if state_path.exists():
        state = load_json(state_path)
        last_pull = state.get("last_pull", "never")
        last_push = state.get("last_push", "never")
        items_synced = state.get("items_synced", 0)
        checksum = state.get("checksum", "n/a")

        print(f"  {bold('Last pull:')}        {color(last_pull, C.SYNC)}")
        print(f"  {bold('Last push:')}        {color(last_push, C.OK)}")
        print(f"  {bold('Items synced:')}     {items_synced}")
        print(f"  {bold('Checksum:')}         {color(checksum, C.MUTED)}")
    else:
        print(color("  No sync state found. Run `pull` or `push` to initialize.", C.WARN))

    print()

    # Current backlog stats
    items = collect_items_flat(root)
    levels = {}
    statuses = {}
    for item in items.values():
        level = item.get("level", "?")
        status = item.get("status", "?")
        levels[level] = levels.get(level, 0) + 1
        statuses[status] = statuses.get(status, 0) + 1

    print(f"  {bold('Backlog items:')}")
    for level in ("Initiative", "Epic", "Feature", "Story"):
        count = levels.get(level, 0)
        lc = {"Initiative": C.INIT, "Epic": C.EPIC, "Feature": C.FEAT, "Story": C.STORY}.get(level, C.RESET)
        print(f"    {color(f'{level}:', lc):22s} {count}")

    print()
    print(f"  {bold('By status:')}")
    status_colors = {
        "Done": C.DONE, "Implementing": C.IMPL, "Validating": C.VALIDATE,
        "Deploying": C.DEPLOY, "Releasing": C.RELEASE, "Analyzing": C.ANALYZE,
        "Backlog": C.BACKLOG, "Ready": C.READY, "Reviewing": C.REVIEW,
        "Funnel": C.FUNNEL,
    }
    for status in ("Done", "Implementing", "Validating", "Deploying", "Releasing",
                    "Analyzing", "Backlog", "Ready", "Reviewing", "Funnel"):
        count = statuses.get(status, 0)
        if count == 0:
            continue
        sc = status_colors.get(status, C.RESET)
        print(f"    {color(f'{status}:', sc):22s} {count}")

    print()

    # Changelog stats
    changelog_path = root / ".edpa" / "changelog.jsonl"
    entries = load_jsonl(changelog_path)
    print(f"  {bold('Changelog:')}        {len(entries)} entries")

    # Current checksum vs stored
    current_checksum = compute_backlog_checksum(root)
    if state_path.exists():
        state = load_json(state_path)
        stored = state.get("checksum", "")
        if stored and stored != current_checksum:
            print(color(f"  {CROSS} Backlog has changed since last sync (checksum mismatch)", C.WARN))
        elif stored:
            print(color(f"  {CHECK} Backlog matches last sync state", C.OK))

    print()


# -- Git Helpers ---------------------------------------------------------------

def _git_commit(root, message):
    """Stage .edpa/ changes and commit."""
    try:
        subprocess.run(["git", "add", ".edpa/"], cwd=root, capture_output=True, check=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=root, capture_output=True
        )
        if result.returncode == 0:
            print(color("  No staged changes to commit.", C.MUTED))
            return
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=root, capture_output=True, check=True
        )
        print(color(f"  {CHECK} Committed: {message}", C.OK))
    except subprocess.CalledProcessError as e:
        print(color(f"  Warning: git commit failed: {e}", C.WARN))


# -- Main CLI ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="sync",
        description="EDPA bidirectional sync: GitHub Projects <-> .edpa/backlog/ item files",
    )

    parser.add_argument("--mock", action="store_true",
                        help="Simulate GitHub Project data from existing backlog (for testing)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detailed output")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # pull
    p_pull = sub.add_parser("pull", help="GitHub Projects -> .edpa/backlog/ item files")
    p_pull.add_argument("--commit", action="store_true",
                        help="Auto-commit changes after pull")
    p_pull.add_argument("--mock", action="store_true",
                        help="Use mock data instead of real GitHub API")

    # push
    p_push = sub.add_parser("push", help=".edpa/backlog/ item files -> GitHub Projects")
    p_push.add_argument("--mock", action="store_true",
                        help="Use mock data instead of real GitHub API")
    p_push.add_argument("--no-commit", action="store_true",
                        help="Skip the auto-commit of issue_map.yaml + edpa.yaml "
                             "after push. Default: auto-commit.")

    # diff
    p_diff = sub.add_parser("diff", help="Show what would change (dry-run)")
    p_diff.add_argument("--mock", action="store_true",
                        help="Use mock data instead of real GitHub API")

    # log
    p_log = sub.add_parser("log", help="Show sync changelog")
    p_log.add_argument("--limit", type=int, default=20,
                       help="Number of entries to show (default: 20)")

    # conflicts
    p_conflicts = sub.add_parser("conflicts",
                                  help="Show unresolved conflicts (optionally auto-resolve)")
    p_conflicts.add_argument("--strategy",
                              choices=["report", "local-wins", "remote-wins", "last-write-wins"],
                              default="report",
                              help="report (default) lists conflicts; the others auto-pick a winner.")
    p_conflicts.add_argument("--apply", action="store_true",
                              help="Apply the resolution plan. Without it, the plan is shown dry-run.")

    # status
    sub.add_parser("status", help="Show sync status")

    # setup-refresh: re-discover field_ids/option_ids/issue_map from existing GH project, with optional --no-commit
    # (parser injection happens just below)

    p_refresh = sub.add_parser("setup-refresh",
                               help="Re-query GitHub to rebuild field_ids/option_ids/issue_map (recovery)")
    p_refresh.add_argument("--no-commit", action="store_true",
                           help="Skip the auto-commit of recovered state. Default: auto-commit.")

    # add-iteration: append a new iteration option to the GH Project Iteration field
    p_add_iter = sub.add_parser("add-iteration",
                                help="Add a new iteration option to the GitHub Iteration field")
    p_add_iter.add_argument("iteration_id",
                            help="Iteration ID (e.g., PI-2026-1.5). The corresponding "
                                 ".edpa/iterations/<ID>.yaml must exist first.")
    p_add_iter.add_argument("--color", default="GRAY",
                            help="Option color (GRAY, BLUE, GREEN, YELLOW, ORANGE, RED, "
                                 "PINK, PURPLE). Default: GRAY.")
    p_add_iter.add_argument("--dry-run", action="store_true",
                            help="Print plan without calling the GitHub API.")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Resolve mock flag from either global or subcommand level
    if not hasattr(args, "mock"):
        args.mock = False
    if not hasattr(args, "commit"):
        args.commit = False

    root = find_repo_root()
    if root is None:
        print(color("Error: Cannot find .edpa/ directory. Run from the EDPA project directory.", C.ERR))
        sys.exit(1)

    sync_config = load_sync_config(root)

    # Ensure changelog and sync_state files exist
    changelog_path = root / ".edpa" / "changelog.jsonl"
    if not changelog_path.exists():
        changelog_path.touch()

    sync_state_path = root / ".edpa" / "sync_state.json"
    if not sync_state_path.exists():
        save_json(sync_state_path, {
            "last_pull": None,
            "last_push": None,
            "items_synced": 0,
            "checksum": "",
        })

    commands = {
        "pull": cmd_pull,
        "push": cmd_push,
        "diff": cmd_diff,
        "log": cmd_log,
        "conflicts": cmd_conflicts,
        "status": cmd_status,
        "setup-refresh": cmd_setup_refresh,
        "add-iteration": cmd_add_iteration,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(root, sync_config, args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
