---
name: edpa-sync-people
user-invocable: true
description: >
  Reconcile .edpa/config/people.yaml against the repository's GitHub
  collaborator list. Reports adds/removes; on apply, flips removed
  collaborators to availability=unavailable and (with --auto-add)
  appends auto-filled stub entries for new ones via PR review.
license: MIT
compatibility: GitHub CLI (gh), Python 3.10+
allowed-tools: Read Bash(python3 *) Bash(gh *) Bash(git *)
metadata:
  author: Jaroslav Urbánek
  version: 1.0.0
  domain: governance
  phase: maintenance
  standard: AgentSkills v1.0
---

# EDPA Sync People — Reconcile collaborators with people.yaml

## What this does

Pulls the current GitHub collaborator list for the repo configured in
`.edpa/config/edpa.yaml` (`sync.github_org` / `sync.github_repo`) and
diffs it against `.edpa/config/people.yaml`:

- **Adds**: GitHub collaborator with no matching `github` field in
  `people.yaml`. By default the maintainer reviews them; with
  `apply --auto-add` the workflow appends an auto-filled stub
  (login, public name, public email) and leaves role/team/FTE blank.
- **Removes**: person in `people.yaml` whose `github` login is no
  longer on the repo. Their `availability` flips to `unavailable`
  (factual, no review needed).
- **Unchanged**: matched both sides — nothing to do.

## Arguments

`$ARGUMENTS` = optional mode override:
- empty / `status` — read-only diff, no writes
- `apply` — flip availability for removes; leave adds for review
- `apply --auto-add` — also append stubs for adds (PR-friendly)

## Steps

### 1. Verify prerequisites

```bash
gh auth status
test -f .edpa/config/people.yaml
test -f .edpa/config/edpa.yaml
```

### 2. Run the script

For status (default):
```bash
python3 .claude/edpa/scripts/sync_collaborators.py status
```

For apply (removes only — safe default):
```bash
python3 .claude/edpa/scripts/sync_collaborators.py apply
```

For apply with auto-add (appends stubs):
```bash
python3 .claude/edpa/scripts/sync_collaborators.py apply --auto-add
```

### 3. Review pending adds

When the script reports `Adds pending review`, each new collaborator
needs role/team/FTE filled in by the maintainer before EDPA can credit
them capacity. The auto-fill includes:
- `id` — derived from login (lowercase)
- `name` — from `gh api users/{login}` (or login fallback)
- `email` — from public profile (often blank)
- `github` — the login
- `availability: confirmed`

The maintainer fills in:
- `role` — Arch / Dev / DevSecOps / PM / QA
- `team`
- `fte` (e.g. `0.5`, `1.0`)
- `capacity_per_iteration` (FTE × hours_per_week × iteration_weeks)

### 4. Validate the result

```bash
python3 .claude/edpa/scripts/validate_iterations.py
```

`validate_iterations.py` runs both iteration AND people validators —
warnings will surface anyone without a `github` login who is referenced
as an assignee, anyone in `people.yaml` who is referenced nowhere, and
any drift in iteration dates.

## Automation

The same logic is wired up as a workflow that runs on GitHub `member`
events:

- `member.added` / `member.edited` → workflow opens a PR with stubs
  pre-filled (you fill role/team/FTE and merge).
- `member.removed` → workflow commits the `availability=unavailable`
  flip directly on `main` (factual, no review needed).

See `plugin/edpa/workflows/collaborators-sync.yml`. The skill is the
manual-trigger counterpart for back-fills, repairs, or initial sync on
a brand-new EDPA install.

## Error handling

- `gh auth status` failing → instruct: `gh auth login`
- Empty `sync.github_org` / `github_repo` in edpa.yaml → instruct user
  to run `/edpa:setup` first or pass `--repo owner/repo` explicitly
- Network / rate limit → the script returns exit 1 with stderr;
  re-running after a minute usually works
