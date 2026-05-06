---
description: Generate visual HTML Kanban board from .edpa/backlog/ YAML files
allowed-tools: Read, Bash
model: sonnet
---

# EDPA Board

Generate a self-contained HTML Kanban snapshot from the local backlog.

## Steps

1. Run the board generator:
```bash
python plugin/edpa/scripts/board.py --open
```

2. Pass any arguments the user specified:
   - `--iteration PI-2026-1.4` — filter by iteration
   - `--level story|feature|epic|initiative` — which level to show (default: story)
   - `--output /path/to/file.html` — custom output location

3. Report the output path and how many items were rendered.
