---
name: edpa-sync
user-invocable: false
description: >
  Bidirectional sync between GitHub Projects and .edpa/backlog/ YAML files.
  Pull updates from GitHub Projects into local YAML, push local changes to GitHub,
  show diff, or check sync status. Use when user says "sync", "pull from GitHub",
  "push to GitHub", or "check sync status".
license: MIT
compatibility: GitHub CLI (gh), Python 3.10+, .edpa/config/edpa.yaml
allowed-tools: Read Write Bash(python3 *) Bash(gh *) Bash(git *) Grep
metadata:
  author: Jaroslav Urbánek
  version: 1.0.0
  domain: governance
  phase: sync
  standard: AgentSkills v1.0
---

# EDPA Sync — GitHub Projects ↔ .edpa/backlog/

## What this does

Synchronizes work items between GitHub Projects v2 and local `.edpa/backlog/` YAML files.
Supports pull (GitHub → local), push (local → GitHub), diff, and status commands.

## Arguments

`$ARGUMENTS` = command: "pull", "push", "diff", "status", or "pull --commit" (auto-commit after pull).

### Argument resolution (when $ARGUMENTS is empty)

If `$ARGUMENTS` is empty, blank, or "help":

1. Present available sync commands:
   ```
   Available commands:
     status          Show last sync time, local/remote changes, conflicts
     diff            Show what would change (dry-run, no modifications)
     pull            GitHub Projects -> .edpa/backlog/ YAML files
     pull --commit   Pull + auto-commit changes
     push            .edpa/backlog/ YAML files -> GitHub Projects
   ```
2. **Default suggestion:** "status" (safe, read-only overview).
3. Ask user: "Which sync command? [status]"

## Prerequisites

- `.edpa/config/edpa.yaml` exists with sync settings (github_org, github_project_number)
- `gh auth status` passes
- `.edpa/backlog/` directory exists with per-item YAML files

## Operations

### Pull (GitHub → Local)

```bash
python3 .claude/edpa/scripts/sync.py pull
```

Fetches all items from GitHub Project, updates `.edpa/backlog/{type}/{ID}.yaml` files.
With `--commit`: auto-commits changes after pull.

### Push (Local → GitHub)

```bash
python3 .claude/edpa/scripts/sync.py push
```

Reads `.edpa/backlog/` YAML files, updates GitHub Project items (field values, status, assignees).

### Diff

```bash
python3 .claude/edpa/scripts/sync.py diff
```

Shows differences between local YAML and GitHub Project state without modifying anything.

### Status

```bash
python3 .claude/edpa/scripts/sync.py status
```

Shows last sync timestamp, number of local/remote changes, conflict count.

## Conflict handling

When both local and remote changed the same field:
1. Show conflict to user with both values
2. Ask user to choose: local, remote, or manual merge
3. Log resolution in `.edpa/changelog.jsonl`

## Error handling

- GitHub API unavailable → "Cannot reach GitHub API. Check `gh auth status`."
- Missing project number → "Configure github_project_number in .edpa/config/edpa.yaml first."
- Sync state corrupt → "Reset sync state? This will do a full pull."
