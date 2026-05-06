#!/usr/bin/env python3
"""
EDPA GitHub Project Views Setup.

GitHub Projects v2 API does not support view creation/configuration.
This script provides two approaches:

1. TEMPLATE: Mark an existing project as template, future projects copy from it
2. MANUAL: Generate step-by-step instructions with exact click targets

Usage:
    # Mark project #4 as template (views will be copied to new projects)
    python .claude/edpa/scripts/project_views.py template --org technomaton --project 4

    # Generate setup instructions for a project
    python .claude/edpa/scripts/project_views.py instructions --org technomaton --project 4

    # Create a new project from template
    python .claude/edpa/scripts/project_views.py create-from-template --org technomaton --template 4 --title "New Project"

    # Verify views are configured correctly
    python .claude/edpa/scripts/project_views.py verify --org technomaton --project 4
"""

import argparse
import json
import subprocess
import sys


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    RED = "\033[31m"
    GRAY = "\033[38;5;245m"
    PURPLE = "\033[38;5;93m"


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def gh_graphql(query):
    r = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                       capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else None


def get_project_id(org, number):
    data = gh_graphql(f'''{{
      organization(login: "{org}") {{
        projectV2(number: {number}) {{
          id
          title
        }}
      }}
    }}''')
    if data:
        p = data["data"]["organization"]["projectV2"]
        return p["id"], p["title"]
    return None, None


def get_org_id(org):
    data = gh_graphql(f'''{{
      organization(login: "{org}") {{ id }}
    }}''')
    return data["data"]["organization"]["id"] if data else None


def get_views(org, number):
    data = gh_graphql(f'''{{
      organization(login: "{org}") {{
        projectV2(number: {number}) {{
          views(first: 20) {{
            nodes {{
              id
              name
              layout
              fields(first: 30) {{
                nodes {{
                  ... on ProjectV2FieldCommon {{
                    id
                    name
                  }}
                }}
              }}
              sortByFields(first: 10) {{
                nodes {{
                  direction
                  field {{
                    ... on ProjectV2FieldCommon {{
                      name
                    }}
                  }}
                }}
              }}
              groupByFields(first: 10) {{
                nodes {{
                  ... on ProjectV2FieldCommon {{
                    name
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}''')
    if data:
        return data["data"]["organization"]["projectV2"]["views"]["nodes"]
    return []


def cmd_template(args):
    """Mark a project as template."""
    project_id, title = get_project_id(args.org, args.project)
    if not project_id:
        print(f"  {C.RED}Project #{args.project} not found{C.RESET}")
        return

    print(f"\n  {C.BOLD}{C.PURPLE}EDPA Project Template Setup{C.RESET}")
    print(f"  {C.GRAY}Project: #{args.project} — {title}{C.RESET}\n")

    # Mark as template
    data = gh_graphql(f'''mutation {{
      markProjectV2AsTemplate(input: {{
        projectId: "{project_id}"
      }}) {{
        projectV2 {{
          id
          title
        }}
      }}
    }}''')

    if data and "data" in data:
        print(f"  {C.GREEN}✓{C.RESET} Project #{args.project} marked as template")
        print(f"  {C.GRAY}Future projects can be created with:{C.RESET}")
        print(f"    python .claude/edpa/scripts/project_views.py create-from-template \\")
        print(f"      --org {args.org} --template {args.project} --title \"New Project\"")
    else:
        print(f"  {C.RED}✗{C.RESET} Failed to mark as template")
        if data:
            print(f"  {C.GRAY}{json.dumps(data.get('errors', []), indent=2)}{C.RESET}")


def cmd_create_from_template(args):
    """Create a new project from template."""
    project_id, title = get_project_id(args.org, args.template)
    org_id = get_org_id(args.org)
    if not project_id or not org_id:
        print(f"  {C.RED}Template #{args.template} or org not found{C.RESET}")
        return

    print(f"\n  {C.BOLD}{C.PURPLE}Creating project from template{C.RESET}")
    print(f"  {C.GRAY}Template: #{args.template} — {title}")
    print(f"  New title: {args.title}{C.RESET}\n")

    data = gh_graphql(f'''mutation {{
      copyProjectV2(input: {{
        projectId: "{project_id}"
        ownerId: "{org_id}"
        title: "{args.title}"
        includeDraftIssues: false
      }}) {{
        projectV2 {{
          id
          number
          title
          url
        }}
      }}
    }}''')

    if data and "data" in data:
        new = data["data"]["copyProjectV2"]["projectV2"]
        print(f"  {C.GREEN}✓{C.RESET} Created project #{new['number']}: {new['title']}")
        print(f"  {C.CYAN}URL: {new['url']}{C.RESET}")
        print(f"\n  {C.GRAY}Views, fields, and layout copied from template.")
        print(f"  Next: populate with issues using project_setup.py{C.RESET}")
    else:
        print(f"  {C.RED}✗{C.RESET} Failed to create from template")
        if data:
            print(f"  {C.GRAY}{json.dumps(data.get('errors', []), indent=2)}{C.RESET}")


def cmd_verify(args):
    """Verify project views are properly configured."""
    _, title = get_project_id(args.org, args.project)
    views = get_views(args.org, args.project)

    print(f"\n  {C.BOLD}{C.PURPLE}EDPA Project Views Verification{C.RESET}")
    print(f"  {C.GRAY}Project: #{args.project} — {title}{C.RESET}\n")

    expected_views = {
        "All Items": {"layout": "TABLE_LAYOUT", "sort": "WSJF Score", "fields": ["Issue Type", "Job Size", "WSJF Score"]},
        "Stories Board": {"layout": "BOARD_LAYOUT"},
        "Features Board": {"layout": "BOARD_LAYOUT"},
        "Epics Board": {"layout": "BOARD_LAYOUT"},
        "WSJF Ranking": {"layout": "TABLE_LAYOUT", "fields": ["Job Size", "WSJF Score"]},
        "Current Iteration": {"layout": "BOARD_LAYOUT"},
        "My Work": {"layout": "TABLE_LAYOUT", "fields": ["Job Size"]},
        "Roadmap": {"layout": "ROADMAP_LAYOUT"},
    }

    found_views = {v["name"]: v for v in views}
    total = 0
    passed = 0

    for exp_name, exp_config in expected_views.items():
        total += 1
        if exp_name in found_views:
            v = found_views[exp_name]
            layout_ok = v.get("layout") == exp_config.get("layout", "TABLE_LAYOUT")
            field_names = [f["name"] for f in v.get("fields", {}).get("nodes", [])]
            fields_ok = all(f in field_names for f in exp_config.get("fields", []))

            if layout_ok and fields_ok:
                print(f"  {C.GREEN}✓{C.RESET} {exp_name} — {v['layout']}, {len(field_names)} columns")
                passed += 1

                # Show sort
                sorts = v.get("sortByFields", {}).get("nodes", [])
                if sorts:
                    for s in sorts:
                        fname = s.get("field", {}).get("name", "?")
                        print(f"    {C.GRAY}Sort: {fname} {s['direction']}{C.RESET}")

                # Show group
                groups = v.get("groupByFields", {}).get("nodes", [])
                if groups:
                    for g in groups:
                        print(f"    {C.GRAY}Group: {g.get('name', '?')}{C.RESET}")
            else:
                issues = []
                if not layout_ok:
                    issues.append(f"layout={v['layout']} (expected {exp_config['layout']})")
                missing = [f for f in exp_config.get("fields", []) if f not in field_names]
                if missing:
                    issues.append(f"missing columns: {', '.join(missing)}")
                print(f"  {C.YELLOW}⚠{C.RESET} {exp_name} — {', '.join(issues)}")
        else:
            print(f"  {C.RED}✗{C.RESET} {exp_name} — not found")

    # Show any extra views
    for name in found_views:
        if name not in expected_views:
            print(f"  {C.GRAY}?{C.RESET} {name} — extra view (not required)")

    print(f"\n  {C.BOLD}Result: {passed}/{total} views OK{C.RESET}")

    if passed < total:
        print(f"\n  {C.YELLOW}Run 'instructions' command for setup guide:{C.RESET}")
        print(f"    python .claude/edpa/scripts/project_views.py instructions --org {args.org} --project {args.project}")


def cmd_instructions(args):
    """Generate step-by-step view setup instructions."""
    _, title = get_project_id(args.org, args.project)

    print(f"\n  {C.BOLD}{C.PURPLE}EDPA Project Views — Setup Instructions{C.RESET}")
    print(f"  {C.GRAY}Project: #{args.project} — {title}{C.RESET}")
    print(f"  {C.GRAY}URL: https://github.com/orgs/{args.org}/projects/{args.project}{C.RESET}")

    views = [
        {
            "name": "All Items",
            "type": "Table",
            "columns": ["Title", "Issue Type", "Status", "Assignees", "Job Size",
                         "Business Value", "Time Criticality", "Risk Reduction",
                         "WSJF Score", "Team"],
            "sort": "WSJF Score ↓ (descending)",
            "group": "Status",
            "filter": None,
        },
        {
            "name": "Board",
            "type": "Board",
            "columns": None,
            "sort": None,
            "group": "Status (automatic: Todo / In Progress / In Review / Done)",
            "filter": None,
        },
        {
            "name": "Epics",
            "type": "Table",
            "columns": ["Title", "Issue Type", "Status", "Job Size",
                         "Business Value", "Time Criticality", "Risk Reduction",
                         "WSJF Score", "Team"],
            "sort": "WSJF Score ↓",
            "group": "Status",
            "filter": 'type:Epic',
        },
        {
            "name": "Features",
            "type": "Table",
            "columns": ["Title", "Issue Type", "Status", "Assignees", "Job Size",
                         "Business Value", "Time Criticality", "Risk Reduction",
                         "WSJF Score"],
            "sort": "WSJF Score ↓",
            "group": "Status",
            "filter": 'type:Feature',
        },
        {
            "name": "WSJF Ranking",
            "type": "Table",
            "columns": ["Title", "Issue Type", "Job Size", "Business Value",
                         "Time Criticality", "Risk Reduction", "WSJF Score"],
            "sort": "WSJF Score ↓",
            "group": "Issue Type",
            "filter": None,
        },
        {
            "name": "Current Iteration",
            "type": "Board",
            "columns": None,
            "sort": None,
            "group": "Status (automatic: Todo / In Progress / In Review / Done)",
            "filter": "iteration:@current (or specific PI-YYYY-N.X)",
        },
        {
            "name": "My Work",
            "type": "Table",
            "columns": ["Title", "Status", "Job Size", "WSJF Score", "Team"],
            "sort": "Status",
            "group": "Status",
            "filter": "assignee:@me",
        },
        {
            "name": "Roadmap",
            "type": "Roadmap",
            "columns": None,
            "sort": None,
            "group": "Planning Interval (date range for timeline)",
            "filter": None,
        },
    ]

    for i, view in enumerate(views, 1):
        print(f"\n  {C.CYAN}{C.BOLD}View {i}: {view['name']}{C.RESET}")
        print(f"  {'─' * 60}")

        print(f"  {C.BOLD}1.{C.RESET} Klikni '+ New view' (vedle existujících tabů)")
        print(f"  {C.BOLD}2.{C.RESET} Pojmenuj: {C.CYAN}{view['name']}{C.RESET}")

        if view["type"] == "Board":
            print(f"  {C.BOLD}3.{C.RESET} Zvol layout: {C.YELLOW}Board{C.RESET}")
            print(f"  {C.GRAY}   Board automaticky seskupí podle Status (Todo/In Progress/In Review/Done){C.RESET}")
        elif view["type"] == "Roadmap":
            print(f"  {C.BOLD}3.{C.RESET} Zvol layout: {C.YELLOW}Roadmap{C.RESET}")
            print(f"  {C.GRAY}   Roadmap zobrazí timeline podle Iteration/Planning Interval{C.RESET}")
            if view["group"]:
                print(f"  {C.BOLD}4.{C.RESET} Date field: {view['group']}")
            if view["filter"]:
                print(f"  {C.BOLD}5.{C.RESET} Filtr: {C.YELLOW}{view['filter']}{C.RESET}")
            print(f"  {C.BOLD}→{C.RESET} Ulož view (Ctrl+S nebo klikni 'Save changes')")
            continue
        else:
            print(f"  {C.BOLD}3.{C.RESET} Zvol layout: {C.YELLOW}Table{C.RESET}")

            if view["columns"]:
                print(f"  {C.BOLD}4.{C.RESET} Přidej sloupce (klikni '+' v headeru):")
                for col in view["columns"]:
                    print(f"       {C.GREEN}☑{C.RESET} {col}")

            if view["sort"]:
                print(f"  {C.BOLD}5.{C.RESET} Seřaď: klikni na header '{view['sort'].replace(' ↓', '')}' → Sort descending")

            if view["group"]:
                print(f"  {C.BOLD}6.{C.RESET} Seskup: View menu (⚙) → Group by → {view['group']}")

            if view["filter"]:
                print(f"  {C.BOLD}7.{C.RESET} Filtr: do search baru napiš: {C.YELLOW}{view['filter']}{C.RESET}")

        print(f"  {C.BOLD}→{C.RESET} Ulož view (Ctrl+S nebo klikni 'Save changes')")

    print(f"\n  {'═' * 60}")
    print(f"  {C.BOLD}Po vytvoření views:{C.RESET}")
    print(f"  Označ projekt jako template pro budoucí kopírování:")
    print(f"    python .claude/edpa/scripts/project_views.py template --org {args.org} --project {args.project}")
    print(f"\n  Ověř konfiguraci:")
    print(f"    python .claude/edpa/scripts/project_views.py verify --org {args.org} --project {args.project}")
    print()


def main():
    parser = argparse.ArgumentParser(description="EDPA Project Views Setup")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tmpl = sub.add_parser("template", help="Mark project as template")
    p_tmpl.add_argument("--org", required=True)
    p_tmpl.add_argument("--project", type=int, required=True)

    p_copy = sub.add_parser("create-from-template", help="Create project from template")
    p_copy.add_argument("--org", required=True)
    p_copy.add_argument("--template", type=int, required=True)
    p_copy.add_argument("--title", required=True)

    p_verify = sub.add_parser("verify", help="Verify view configuration")
    p_verify.add_argument("--org", required=True)
    p_verify.add_argument("--project", type=int, required=True)

    p_instr = sub.add_parser("instructions", help="Generate setup instructions")
    p_instr.add_argument("--org", required=True)
    p_instr.add_argument("--project", type=int, required=True)

    args = parser.parse_args()

    commands = {
        "template": cmd_template,
        "create-from-template": cmd_create_from_template,
        "verify": cmd_verify,
        "instructions": cmd_instructions,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
