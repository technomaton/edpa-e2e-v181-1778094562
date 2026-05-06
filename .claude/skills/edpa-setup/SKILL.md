---
name: edpa-setup
user-invocable: false
description: >
  Initialize EDPA governance for a project. Creates GitHub Projects with custom fields,
  work item hierarchy (Initiative→Epic→Feature→Story), capacity registry, CW heuristics,
  and branch naming enforcement. Use when starting a new project or onboarding EDPA.
license: MIT
compatibility: GitHub CLI (gh), Python 3.10+
allowed-tools: Read Write Bash(gh *) Bash(git *) Bash(mkdir *) Bash(python3 *) Bash(cp *)
metadata:
  author: Jaroslav Urbánek
  version: 1.0.0
  domain: governance
  phase: setup
  standard: AgentSkills v1.0
---

# EDPA Setup — Project Initialization

## What this does

Initializes complete EDPA governance infrastructure for a GitHub-based project.

## Arguments

`$ARGUMENTS` = project name (e.g., "Medical Platform")

### Argument resolution (when $ARGUMENTS is empty)

If `$ARGUMENTS` is empty, blank, or "help":

1. Check if `.edpa/config/people.yaml` exists (re-initialization scenario):
   - If yes, read `project.name` and present: "EDPA is already initialized for **{name}**. Re-run setup? [y/N]"
   - If re-running, use existing project name as default.
2. If `.edpa/` does not exist (fresh setup):
   - Read the git remote to infer project name: `git remote get-url origin` → extract repo name
   - Present: "Initialize EDPA for project: **{inferred-name}**? Or enter a different name."
3. Ask user to confirm or provide project name before proceeding.

## Steps

### 1. Verify prerequisites

```bash
gh auth status
python3 -c "import yaml, openpyxl; print('OK')"
```

If missing: `pip install pyyaml openpyxl --break-system-packages`

### 2. Create .edpa/ directory structure

```bash
mkdir -p .edpa/config .edpa/backlog/initiatives .edpa/backlog/epics .edpa/backlog/features .edpa/backlog/stories .edpa/iterations .edpa/reports .edpa/snapshots .edpa/data
touch .edpa/changelog.jsonl .edpa/sync_state.json
```

### 2b. Copy CI workflows

```bash
cp .claude/edpa/workflows/*.yml .github/workflows/ 2>/dev/null || true
```

### 3. Initialize capacity registry

Create `.edpa/config/people.yaml` with project name from $ARGUMENTS.
For each team member, ask the user explicitly for: name, role,
team/organization, FTE, email, **and GitHub username**. Calculate
`capacity_per_iteration = fte × hours_per_week × iteration_weeks`.

**CRITICAL** — never invent the `github` field from email patterns
(e.g. `jaroslav@company.com` → `jaroslav`) or from the user's name.
GitHub usernames are not derivable. If the user does not know
someone's login, leave `github: ""` and tell them they can fill it
in later — `sync push --assignee` simply skips people without a
login. Inventing a login risks routing issue assignments to a
stranger with the same handle.

Canonical phrasing per person:
> "GitHub username for {name}? (leave blank if you don't know it
> right now — you can fill it in later in `.edpa/config/people.yaml`)"

Template:
```yaml
project:
  name: "$ARGUMENTS"
  registration: ""
  domain: ""
cadence:
  iteration_weeks: 2
  pi_weeks: 10
  naming_pattern: "PI-{year}-{pi_num}.{iter_num}"
people:
  - id: example
    name: "Example Person"
    role: Dev
    team: ""
    fte: 1.0
    capacity_per_iteration: 80
    email: ""
    github: ""              # GitHub login (or blank — don't guess)
    availability: confirmed  # confirmed, partial, unavailable
```

### 4. Initialize CW heuristics

Create `.edpa/config/heuristics.yaml`:
```yaml
version: "1.8.1-beta"
evidence_threshold: 1.0
role_weights:
  owner: 1.0
  key: 0.6
  reviewer: 0.25
  consulted: 0.15
signals:
  assignee: 4
  contribute_command: 3
  pr_author: 2
  commit_author: 1
  pr_reviewer: 1
  issue_comment: 0.5
```

### 5. Create GitHub Projects custom fields

Read `docs/github-setup.md` (from project root) for field definitions.
Use `gh` CLI to create project and fields:

```bash
gh project create --title "$ARGUMENTS Governance" --owner @me
# Add custom fields via GraphQL — see docs/github-setup.md
```

### 6. Create branch naming CI check

Create `.github/workflows/branch-check.yml` that blocks PRs without S-XXX/F-XXX/E-XXX reference.

### 7. Hierarchy is mandatory — never produce a flat backlog

**CRITICAL** — every backlog item below the Initiative level MUST
declare a `parent:` field referencing a higher-level item. The skill
must refuse to emit flat lists, and the wizard must use the
`backlog.py add` CLI rather than writing YAML files directly or
calling `gh issue create` by hand:

```bash
# Correct — backlog.py enforces parent + assigns the next ID
python .claude/edpa/scripts/backlog.py add --type Initiative --title "Platform"
python .claude/edpa/scripts/backlog.py add --type Epic        --parent I-1 --title "Auth"
python .claude/edpa/scripts/backlog.py add --type Feature     --parent E-1 --title "OAuth"
python .claude/edpa/scripts/backlog.py add --type Story       --parent F-1 --title "Login UI" --js 5

# After items exist, sync push wires parent-child to GitHub sub-issues:
python .claude/edpa/scripts/sync.py push
```

**Forbidden** — these bypass hierarchy enforcement:
- `gh issue create ...` directly (skips `backlog.py add` validation)
- Writing `.edpa/backlog/**/*.yaml` files via the editor without a
  `parent:` field on every non-Initiative entry
- Skipping `sync push` after adding items locally — without it,
  GitHub Issues never get linked as sub-issues

If the user asks "create issues for the backlog", ALWAYS use
`backlog.py add` per item, then a single `sync push` at the end.

### 8. Output confirmation

Print summary: project name, team count, total FTE, capacity/iteration, cadence.

The `project_setup.py` wizard automatically prompts for optional
`create_project_views.py` invocation (Initiative / Epic / Feature /
Story / Status views in the GitHub Project UI). Default is yes.
Failure to create views is non-fatal — the maintainer can re-run
`python .claude/edpa/scripts/create_project_views.py` later.

## Error handling

- `gh` not authenticated → print `gh auth login` instructions
- Missing Python packages → install with pip
- GitHub API rate limit → wait and retry
