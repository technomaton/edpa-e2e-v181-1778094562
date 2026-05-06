# EDPA Plugin for Claude Code

This directory contains the installable EDPA plugin for Claude Code and compatible AI coding assistants.

## Installation

```bash
curl -fsSL https://edpa.technomaton.com/install.sh | sh
```

The installer copies this directory's contents into `.claude/` in your project.

## Structure

```
plugin/
├── edpa/
│   ├── scripts/                   # Python engine + utilities
│   │   ├── engine.py              # Core EDPA engine (Score, DerivedHours, invariants)
│   │   ├── evaluate_cw.py         # CW evaluator for auto-calibration (LOCKED)
│   │   ├── backlog.py             # Git-native backlog CLI (tree, wsjf, validate)
│   │   ├── sync.py                # GitHub Projects <-> Git bidirectional sync
│   │   ├── issue_types.py         # GitHub Issue Types management
│   │   ├── project_setup.py       # Automated GitHub Project initialization
│   │   ├── project_views.py       # GitHub Project view configuration
│   │   └── create_project_views.py
│   ├── templates/                 # Config templates
│   │   ├── people.yaml.tmpl       # Team members, FTE, capacity
│   │   ├── cw_heuristics.yaml.tmpl # Evidence scoring weights
│   │   └── project.yaml.tmpl     # Project metadata
│   └── workflows/                 # GitHub Actions
│       ├── branch-check.yml       # Branch naming enforcement
│       ├── iteration-close.yml    # Iteration close automation
│       ├── sync-projects-to-git.yml
│       └── sync-git-to-projects.yml
├── commands/edpa/                 # Claude Code slash commands
│   ├── setup.md                   # /edpa setup
│   ├── close-iteration.md         # /edpa close-iteration
│   ├── reports.md                 # /edpa reports
│   ├── calibrate.md               # /edpa calibrate
│   └── sync.md                    # /edpa sync
├── skills/                        # Claude Code skills
│   ├── edpa-setup/SKILL.md        # Project initialization
│   ├── edpa-engine/SKILL.md       # Evidence-driven calculation
│   ├── edpa-reports/SKILL.md      # Timesheet and export generation
│   ├── edpa-autocalib/SKILL.md    # CW heuristic optimization
│   └── edpa-sync/SKILL.md         # GitHub Projects <-> Git sync
├── .mcp.json                      # GitHub MCP server configuration
└── .claude-plugin/plugin.json     # Plugin manifest
```

## 5 Skills

| Skill | Command | Description |
|-------|---------|-------------|
| **edpa-setup** | `/edpa setup` | Initialize governance (GitHub Projects, config, CI) |
| **edpa-engine** | `/edpa close-iteration` | Compute hours from evidence + validate invariants |
| **edpa-reports** | `/edpa reports` | Generate timesheets, snapshots, Excel exports |
| **edpa-autocalib** | `/edpa calibrate` | Auto-calibrate CW heuristics (Karpathy loop) |
| **edpa-sync** | `/edpa sync` | Sync GitHub Projects <-> Git backlog |

## Cross-Platform Compatibility

Skills work on 26+ platforms:

```bash
# Claude Code — auto-detected from .claude/
# Codex CLI
cp -r .claude/skills/* ~/.codex/skills/
# Cursor — auto-detected
# Gemini CLI
cp -r .claude/skills/* ~/.gemini/skills/
```

## Target Directory Layout

After installation, the plugin creates this structure in the target project:

```
.claude/
├── edpa/scripts/       # Engine + utilities
├── edpa/templates/     # Config templates
├── edpa/workflows/     # GitHub Actions
├── commands/edpa/      # Slash commands (5)
├── skills/             # Skills (5)
└── .mcp.json           # MCP config

.edpa/
├── config/             # people.yaml, heuristics.yaml
├── backlog/            # Work items (file-per-item)
├── iterations/         # Iteration definitions
├── reports/            # Generated timesheets
├── snapshots/          # Frozen iteration snapshots
└── data/               # Raw evidence data
```
