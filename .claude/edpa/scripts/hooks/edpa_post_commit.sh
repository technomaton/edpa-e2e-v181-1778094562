#!/bin/sh
# EDPA post-commit hook — generates commit info JSON after git commit.
# Reads Claude Code tool_input JSON from stdin.
# Exit 0 always (non-blocking). Outputs JSON to stdout only for git commit commands.
set -e

# Read stdin (Claude Code passes JSON with tool_input)
INPUT=$(cat)

# Extract command from JSON
COMMAND=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    cmd = data.get('tool_input', {}).get('command', '')
    print(cmd)
except Exception:
    print('')
" 2>/dev/null)

# Only trigger on git commit (not amend, not other git commands)
case "$COMMAND" in
    git\ commit\ *)
        # Skip amend commits
        case "$COMMAND" in
            *--amend*) exit 0 ;;
        esac
        ;;
    *) exit 0 ;;
esac

# Find the script directory (resolve symlinks)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Run edpa_commit_info.py
python3 "$SCRIPT_DIR/../edpa_commit_info.py" 2>/dev/null || true

exit 0
