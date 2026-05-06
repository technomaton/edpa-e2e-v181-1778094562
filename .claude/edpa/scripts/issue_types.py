#!/usr/bin/env python3
"""
EDPA Issue Types CLI -- Manage GitHub org-level Issue Types.

Migrates EDPA from GitHub labels to native Issue Types (org-level).

Usage:
    python .claude/edpa/scripts/issue_types.py list --org technomaton
    python .claude/edpa/scripts/issue_types.py setup --org technomaton
    python .claude/edpa/scripts/issue_types.py setup --org technomaton --dry-run
    python .claude/edpa/scripts/issue_types.py assign --org technomaton --repo edpa-simulation --issue 1 --type Epic
    python .claude/edpa/scripts/issue_types.py migrate --org technomaton --repo edpa-simulation
    python .claude/edpa/scripts/issue_types.py migrate --org technomaton --repo edpa-simulation --dry-run

Prerequisite:
    gh auth login (with admin:org scope for issue type management)
"""

import argparse
import json
import subprocess
import sys


# -- ANSI Colors (EDPA palette) -----------------------------------------------

class C:
    """ANSI color codes matching EDPA design palette."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Issue Type colors
    INIT    = "\033[35m"        # Magenta  -- Initiative
    EPIC    = "\033[38;5;93m"   # Purple   -- Epic
    FEAT    = "\033[36m"        # Cyan     -- Feature
    STORY   = "\033[32m"        # Green    -- Story
    DEFECT  = "\033[31m"        # Red      -- Defect
    TASK    = "\033[33m"        # Yellow   -- Task
    # Utility
    OK      = "\033[32m"
    WARN    = "\033[33m"
    ERR     = "\033[31m"
    HEADER  = "\033[38;5;147m"  # Light purple
    MUTED   = "\033[38;5;245m"  # Gray


def color(text, code):
    return f"{code}{text}{C.RESET}"


def bold(text):
    return f"{C.BOLD}{text}{C.RESET}"


# -- Box-drawing / symbols ---------------------------------------------------

DOT   = "\u2022"
DASH  = "\u2500"
CHECK = "\u2713"
CROSS = "\u2717"
ARROW = "\u2192"


# -- EDPA Issue Type definitions ----------------------------------------------

# The canonical set of issue types for an EDPA-managed org.
# GitHub Issue Type color enum values: RED, ORANGE, YELLOW, GREEN, TEAL,
# BLUE, INDIGO, PURPLE, PINK, GRAY.
EDPA_TYPES = {
    "Task":       {"color": "YELLOW", "description": "A unit of work to be completed."},
    "Defect":     {"color": "RED",    "description": "A defect in existing functionality."},
    "Feature":    {"color": "BLUE",   "description": "A service provided by the system that fulfills a stakeholder need — must fit in a PI."},
    "Initiative": {"color": "PINK",   "description": "Strategic business initiative with investment funding (SAFe Portfolio level)."},
    "Epic":       {"color": "PURPLE", "description": "Large body of work decomposed into features — strategic goal spanning 6-9 months (SAFe Essential level)."},
    "Story":      {"color": "GREEN",  "description": "A small, deliverable unit of value completed within a single iteration (SAFe Team level)."},
}

# Map from Issue Type name to ANSI color code for display
TYPE_ANSI = {
    "Task":       C.TASK,
    "Defect":     C.DEFECT,
    "Feature":    C.FEAT,
    "Initiative": C.INIT,
    "Epic":       C.EPIC,
    "Story":      C.STORY,
}

# Map from GitHub Issue Type color enum to ANSI approximation
COLOR_ANSI = {
    "RED":    "\033[31m",
    "ORANGE": "\033[38;5;208m",
    "YELLOW": "\033[33m",
    "GREEN":  "\033[32m",
    "TEAL":   "\033[38;5;30m",
    "BLUE":   "\033[34m",
    "INDIGO": "\033[38;5;63m",
    "PURPLE": "\033[38;5;93m",
    "PINK":   "\033[35m",
    "GRAY":   "\033[38;5;245m",
}


# -- GitHub API helpers -------------------------------------------------------

def gh_graphql(query):
    """Execute a GitHub GraphQL query via the gh CLI."""
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        if err:
            print(f"  {color('GraphQL error:', C.ERR)} {err}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  {color('Failed to parse GraphQL response', C.ERR)}")
        return None


def get_org_id(org):
    """Resolve an organization login to its node ID."""
    data = gh_graphql(f'{{ organization(login: "{org}") {{ id }} }}')
    if data and "data" in data:
        return data["data"]["organization"]["id"]
    return None


def get_org_issue_types(org):
    """Fetch all issue types for an organization."""
    data = gh_graphql(f'''{{
      organization(login: "{org}") {{
        issueTypes(first: 20) {{
          nodes {{
            id
            name
            color
            isEnabled
            description
          }}
        }}
      }}
    }}''')
    if data and "data" in data:
        return data["data"]["organization"]["issueTypes"]["nodes"]
    return []


def get_issue_node_id(org, repo, issue_number):
    """Resolve a repo issue number to its node ID."""
    data = gh_graphql(f'''{{
      repository(owner: "{org}", name: "{repo}") {{
        issue(number: {issue_number}) {{
          id
          title
          issueType {{
            name
          }}
          labels(first: 20) {{
            nodes {{
              name
            }}
          }}
        }}
      }}
    }}''')
    if data and "data" in data:
        return data["data"]["repository"]["issue"]
    return None


def get_repo_issues(org, repo, label=None, cursor=None):
    """Fetch issues from a repo, optionally filtered by label. Returns (nodes, pageInfo)."""
    label_filter = f', labels: ["{label}"]' if label else ""
    after = f', after: "{cursor}"' if cursor else ""
    data = gh_graphql(f'''{{
      repository(owner: "{org}", name: "{repo}") {{
        issues(first: 50, states: [OPEN, CLOSED]{label_filter}{after}) {{
          nodes {{
            id
            number
            title
            issueType {{
              name
            }}
            labels(first: 20) {{
              nodes {{
                name
              }}
            }}
          }}
          pageInfo {{
            hasNextPage
            endCursor
          }}
        }}
      }}
    }}''')
    if data and "data" in data:
        issues_data = data["data"]["repository"]["issues"]
        return issues_data["nodes"], issues_data["pageInfo"]
    return [], {"hasNextPage": False, "endCursor": None}


def create_issue_type(org_id, name, gql_color, description):
    """Create an issue type at the org level."""
    # Escape description for GraphQL string
    safe_desc = description.replace('"', '\\"')
    data = gh_graphql(f'''mutation {{
      createIssueType(input: {{
        ownerId: "{org_id}"
        name: "{name}"
        color: {gql_color}
        description: "{safe_desc}"
      }}) {{
        issueType {{
          id
          name
          color
        }}
      }}
    }}''')
    if data and "data" in data:
        return data["data"]["createIssueType"]["issueType"]
    if data and "errors" in data:
        for err in data["errors"]:
            print(f"      {color(CROSS, C.ERR)} {err.get('message', 'Unknown error')}")
    return None


def update_issue_type(type_id, name=None, gql_color=None, description=None):
    """Update an existing issue type."""
    fields = []
    if name:
        fields.append(f'name: "{name}"')
    if gql_color:
        fields.append(f"color: {gql_color}")
    if description:
        safe_desc = description.replace('"', '\\"')
        fields.append(f'description: "{safe_desc}"')
    if not fields:
        return None
    field_str = ", ".join(fields)
    # GitHub renamed the field on UpdateIssueTypeInput from `id`
    # (deprecated) to `issueTypeId`. The deprecated name now errors out
    # with `Argument 'issueTypeId' on InputObject 'UpdateIssueTypeInput'
    # is required` so we send the canonical name.
    data = gh_graphql(f'''mutation {{
      updateIssueType(input: {{
        issueTypeId: "{type_id}"
        {field_str}
      }}) {{
        issueType {{
          id
          name
          color
          description
        }}
      }}
    }}''')
    if data and "data" in data:
        return data["data"]["updateIssueType"]["issueType"]
    if data and "errors" in data:
        for err in data["errors"]:
            print(f"      {color(CROSS, C.ERR)} {err.get('message', 'Unknown error')}")
    return None


def assign_issue_type(issue_id, type_id):
    """Assign an issue type to an issue."""
    data = gh_graphql(f'''mutation {{
      updateIssueIssueType(input: {{
        issueId: "{issue_id}"
        issueTypeId: "{type_id}"
      }}) {{
        issue {{
          id
          title
          issueType {{
            name
          }}
        }}
      }}
    }}''')
    if data and "data" in data:
        return data["data"]["updateIssueIssueType"]["issue"]
    if data and "errors" in data:
        for err in data["errors"]:
            print(f"      {color(CROSS, C.ERR)} {err.get('message', 'Unknown error')}")
    return None


def remove_label(org, repo, issue_number, label_name):
    """Remove a label from an issue via REST API."""
    result = subprocess.run(
        ["gh", "api", "--method", "DELETE",
         f"repos/{org}/{repo}/issues/{issue_number}/labels/{label_name}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


# -- Output helpers -----------------------------------------------------------

def step(num, text):
    print(f"\n  {C.BOLD}{C.FEAT}[{num}]{C.RESET} {text}")


def ok(text):
    print(f"      {color(CHECK, C.OK)} {text}")


def skip(text):
    print(f"      {color(DASH, C.MUTED)} {text}")


def fail(text):
    print(f"      {color(CROSS, C.ERR)} {text}")


def info(text):
    print(f"      {color(text, C.MUTED)}")


def type_color(name):
    """Get ANSI color for an issue type name."""
    return TYPE_ANSI.get(name, C.MUTED)


# -- Subcommands --------------------------------------------------------------

def cmd_list(args):
    """List all issue types configured for the organization."""
    org = args.org
    types = get_org_issue_types(org)

    print()
    print(bold(color(f"  Issue Types for {org}", C.HEADER)))
    print(color(f"  {DASH * 60}", C.MUTED))
    print()

    if not types:
        print(f"  {color('No issue types found (or insufficient permissions).', C.WARN)}")
        print()
        return

    # Table header
    hdr = f"  {'Name':16s}  {'Color':10s}  {'Enabled':8s}  {'ID':28s}  Description"
    print(color(hdr, C.MUTED))
    print(color(f"  {DASH * 90}", C.MUTED))

    for t in types:
        name = t["name"]
        gql_color = t.get("color", "GRAY")
        enabled = t.get("isEnabled", True)
        desc = t.get("description", "") or ""
        node_id = t.get("id", "")

        # Color the name using EDPA palette if known, else use GH color
        name_ansi = TYPE_ANSI.get(name, COLOR_ANSI.get(gql_color, C.MUTED))
        enabled_str = color("yes", C.OK) if enabled else color("no", C.DIM)

        # Color swatch using the GH color enum
        swatch_ansi = COLOR_ANSI.get(gql_color, C.MUTED)
        color_label = f"{swatch_ansi}{DOT} {gql_color:8s}{C.RESET}"

        print(f"  {color(f'{name:16s}', name_ansi)}  {color_label}  {enabled_str:18s}  "
              f"{color(node_id[:28], C.DIM)}  {color(desc[:50], C.MUTED)}")

    print()
    print(f"  {color(f'{len(types)} issue type(s)', C.MUTED)}")
    print()


def cmd_setup(args):
    """Set up EDPA issue types on the org. Idempotent."""
    org = args.org
    dry_run = args.dry_run

    print()
    print(bold(color("  EDPA Issue Types Setup", C.HEADER)))
    print(color(f"  Organization: {org}", C.MUTED))
    if dry_run:
        print(color("  Mode: DRY RUN", C.WARN))
    print()

    # -- Resolve org ID --
    org_id = None
    if not dry_run:
        org_id = get_org_id(org)
        if not org_id:
            fail(f"Could not resolve org ID for '{org}'")
            sys.exit(1)
        info(f"Org node ID: {org_id}")

    # -- Fetch current types --
    step(1, "Fetching current issue types")
    existing = get_org_issue_types(org)
    existing_by_name = {t["name"]: t for t in existing}
    info(f"Found {len(existing)} existing type(s): {', '.join(existing_by_name.keys()) or '(none)'}")

    # -- Rename Bug -> Defect if needed --
    step(2, "Checking Bug -> Defect rename")
    if "Bug" in existing_by_name and "Defect" not in existing_by_name:
        bug = existing_by_name["Bug"]
        if dry_run:
            ok(f"Would rename Bug (id={bug['id']}) -> Defect")
        else:
            result = update_issue_type(
                bug["id"],
                name="Defect",
                gql_color="RED",
                description=EDPA_TYPES["Defect"]["description"],
            )
            if result:
                ok(f"Renamed Bug -> Defect (id={bug['id']})")
                existing_by_name["Defect"] = result
                del existing_by_name["Bug"]
            else:
                fail("Failed to rename Bug -> Defect")
    elif "Defect" in existing_by_name:
        skip("Defect already exists (no rename needed)")
    elif "Bug" not in existing_by_name:
        skip("Bug not found (will create Defect as new type)")

    # -- Update Feature description --
    step(3, "Updating Feature description")
    if "Feature" in existing_by_name:
        feat = existing_by_name["Feature"]
        target_desc = EDPA_TYPES["Feature"]["description"]
        current_desc = feat.get("description", "") or ""
        if current_desc != target_desc:
            if dry_run:
                ok(f"Would update Feature description")
                info(f"  Current: {current_desc[:60]}...")
                info(f"  Target:  {target_desc[:60]}...")
            else:
                result = update_issue_type(feat["id"], description=target_desc)
                if result:
                    ok("Feature description updated")
                else:
                    fail("Failed to update Feature description")
        else:
            skip("Feature description already matches")
    else:
        skip("Feature not found yet (will be created with correct description)")

    # -- Create missing types --
    step(4, "Creating missing issue types")
    created = 0
    for type_name, spec in EDPA_TYPES.items():
        if type_name in existing_by_name:
            skip(f"{type_name} already exists")
            continue

        if dry_run:
            ok(f"Would create: {color(type_name, TYPE_ANSI.get(type_name, C.MUTED))} "
               f"({spec['color']}) -- {spec['description'][:50]}")
            created += 1
        else:
            result = create_issue_type(org_id, type_name, spec["color"], spec["description"])
            if result:
                ok(f"Created: {color(type_name, TYPE_ANSI.get(type_name, C.MUTED))} "
                   f"(id={result['id']})")
                existing_by_name[type_name] = result
                created += 1
            else:
                fail(f"Failed to create {type_name}")

    # -- Summary --
    print()
    print(color(f"  {DASH * 60}", C.MUTED))
    action = "would be" if dry_run else "were"
    if created:
        print(f"  {color(CHECK, C.OK)} {created} type(s) {action} created/updated")
    else:
        print(f"  {color(CHECK, C.OK)} All EDPA issue types already configured")

    # Verify final state
    final_types = get_org_issue_types(org)
    final_names = {t["name"] for t in final_types}
    expected_names = set(EDPA_TYPES.keys())
    missing = expected_names - final_names
    if missing and not dry_run:
        print(f"  {color(f'Missing after setup: {", ".join(sorted(missing))}', C.WARN)}")
    elif not dry_run:
        print(f"  {color(f'Verified: all {len(expected_names)} EDPA types present', C.OK)}")
    print()


def cmd_assign(args):
    """Assign an issue type to a specific issue."""
    org = args.org
    repo = args.repo
    issue_number = args.issue
    type_name = args.type

    print()
    print(bold(color("  Assign Issue Type", C.HEADER)))
    print(color(f"  {org}/{repo}#{issue_number} {ARROW} {type_name}", C.MUTED))
    print()

    # Resolve issue node ID
    issue_data = get_issue_node_id(org, repo, issue_number)
    if not issue_data:
        fail(f"Issue #{issue_number} not found in {org}/{repo}")
        sys.exit(1)

    issue_id = issue_data["id"]
    current_type = issue_data.get("issueType")
    current_name = current_type["name"] if current_type else "(none)"
    info(f"Issue: #{issue_number} -- {issue_data['title']}")
    info(f"Current type: {current_name}")

    # Resolve issue type ID
    types = get_org_issue_types(org)
    types_by_name = {t["name"]: t for t in types}

    if type_name not in types_by_name:
        fail(f"Issue type '{type_name}' not found in org '{org}'")
        available = ", ".join(sorted(types_by_name.keys()))
        info(f"Available: {available}")
        sys.exit(1)

    type_id = types_by_name[type_name]["id"]

    if current_name == type_name:
        skip(f"Already set to {type_name}")
        print()
        return

    # Assign
    result = assign_issue_type(issue_id, type_id)
    if result:
        assigned_name = result.get("issueType", {}).get("name", type_name)
        ok(f"#{issue_number} {ARROW} {color(assigned_name, type_color(assigned_name))}")
    else:
        fail(f"Failed to assign type '{type_name}' to issue #{issue_number}")
        sys.exit(1)

    print()


def cmd_migrate(args):
    """Migrate issues from labels to native Issue Types."""
    org = args.org
    repo = args.repo
    dry_run = args.dry_run
    remove_labels = args.remove_labels

    print()
    print(bold(color("  EDPA Issue Type Migration", C.HEADER)))
    print(color(f"  Repository: {org}/{repo}", C.MUTED))
    if dry_run:
        print(color("  Mode: DRY RUN", C.WARN))
    if remove_labels:
        print(color("  Remove labels after migration: yes", C.MUTED))
    print()

    # Resolve issue types
    step(1, "Resolving org issue types")
    types = get_org_issue_types(org)
    types_by_name = {t["name"]: t for t in types}

    # Labels that map to issue types
    label_to_type = {
        "Epic":       "Epic",
        "Feature":    "Feature",
        "Story":      "Story",
        "Initiative": "Initiative",
        "Bug":        "Defect",
        "Defect":     "Defect",
        "Task":       "Task",
    }

    available_mappings = {}
    for label, type_name in label_to_type.items():
        if type_name in types_by_name:
            available_mappings[label] = types_by_name[type_name]
            ok(f"Label '{label}' {ARROW} Issue Type '{type_name}' (id={types_by_name[type_name]['id'][:20]}...)")
        else:
            info(f"Label '{label}' {ARROW} Type '{type_name}' NOT FOUND in org (skipping)")

    if not available_mappings:
        fail("No matching issue types found. Run 'setup' first.")
        sys.exit(1)

    # Fetch all issues and find those with matching labels
    step(2, "Scanning issues")
    all_issues = []
    cursor = None
    page = 0
    while True:
        page += 1
        nodes, page_info = get_repo_issues(org, repo, cursor=cursor)
        all_issues.extend(nodes)
        info(f"Page {page}: fetched {len(nodes)} issues (total: {len(all_issues)})")
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    # Filter issues that have a label matching our mapping and no type already set
    step(3, "Migrating issues")
    migrated = 0
    skipped = 0
    failed = 0

    for issue in all_issues:
        issue_labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
        current_type = issue.get("issueType")
        current_type_name = current_type["name"] if current_type else None

        # Find first matching label
        matched_label = None
        matched_type_info = None
        for label in issue_labels:
            if label in available_mappings:
                matched_label = label
                matched_type_info = available_mappings[label]
                break

        if not matched_label:
            continue  # no relevant label

        target_type_name = label_to_type[matched_label]

        # Already has the correct type
        if current_type_name == target_type_name:
            skip(f"#{issue['number']} {issue['title'][:40]} -- already {current_type_name}")
            skipped += 1
            continue

        issue_id = issue["id"]
        type_id = matched_type_info["id"]
        tc = type_color(target_type_name)

        if dry_run:
            ok(f"#{issue['number']} {issue['title'][:40]} -- "
               f"[{matched_label}] {ARROW} {color(target_type_name, tc)}")
            migrated += 1
        else:
            result = assign_issue_type(issue_id, type_id)
            if result:
                ok(f"#{issue['number']} {issue['title'][:40]} -- "
                   f"[{matched_label}] {ARROW} {color(target_type_name, tc)}")
                migrated += 1

                # Optionally remove the label
                if remove_labels:
                    if remove_label(org, repo, issue["number"], matched_label):
                        info(f"  Removed label '{matched_label}'")
                    else:
                        info(f"  Could not remove label '{matched_label}'")
            else:
                fail(f"#{issue['number']} {issue['title'][:40]} -- FAILED")
                failed += 1

    # Summary
    print()
    print(color(f"  {DASH * 60}", C.MUTED))
    action = "would be" if dry_run else ""
    print(f"  Migrated: {color(str(migrated), C.OK)} {action}")
    print(f"  Skipped:  {color(str(skipped), C.MUTED)} (already correct)")
    if failed:
        print(f"  Failed:   {color(str(failed), C.ERR)}")
    print(f"  Total scanned: {len(all_issues)}")
    print()


# -- Main ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="issue_types",
        description="EDPA Issue Types CLI -- manage GitHub org-level Issue Types",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # list
    p_list = sub.add_parser("list", help="List all issue types for the org")
    p_list.add_argument("--org", required=True, help="GitHub organization")

    # setup
    p_setup = sub.add_parser("setup", help="Create/configure EDPA issue types (idempotent)")
    p_setup.add_argument("--org", required=True, help="GitHub organization")
    p_setup.add_argument("--dry-run", action="store_true",
                         help="Show what would be done without making changes")

    # assign
    p_assign = sub.add_parser("assign", help="Assign an issue type to a single issue")
    p_assign.add_argument("--org", required=True, help="GitHub organization")
    p_assign.add_argument("--repo", required=True, help="Repository name")
    p_assign.add_argument("--issue", required=True, type=int, help="Issue number")
    p_assign.add_argument("--type", required=True, help="Issue type name (e.g. Epic, Story)")

    # migrate
    p_migrate = sub.add_parser("migrate", help="Migrate label-based types to native Issue Types")
    p_migrate.add_argument("--org", required=True, help="GitHub organization")
    p_migrate.add_argument("--repo", required=True, help="Repository name")
    p_migrate.add_argument("--dry-run", action="store_true",
                           help="Show what would be done without making changes")
    p_migrate.add_argument("--remove-labels", action="store_true",
                           help="Remove old labels after assigning Issue Types")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "list":
        cmd_list(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "assign":
        cmd_assign(args)
    elif args.command == "migrate":
        cmd_migrate(args)


if __name__ == "__main__":
    main()
