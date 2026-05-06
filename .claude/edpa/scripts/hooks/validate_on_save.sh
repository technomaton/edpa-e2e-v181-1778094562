#!/bin/sh
# EDPA validate_on_save hook — validates YAML, JSON, and Python files when Claude Code writes them.
# Reads Claude Code tool_input JSON from stdin.
# Exit 0 always (non-blocking), but prints validation errors to stderr.
set -e

# Read stdin (Claude Code passes JSON with tool_input)
INPUT=$(cat)

# Extract file_path from JSON
FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    path = data.get('tool_input', {}).get('file_path', '')
    print(path)
except Exception:
    print('')
" 2>/dev/null)

# Skip if no file path or not a supported file type
case "$FILE_PATH" in
    *.yaml|*.yml|*.json|*.py) ;;
    *) exit 0 ;;
esac

# Skip if file doesn't exist
[ -f "$FILE_PATH" ] || exit 0

# Validate syntax (pass path via env to avoid shell injection)
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$SCRIPT_DIR" EDPA_VALIDATE_PATH="$FILE_PATH" python3 -c "
import os, sys
path = os.environ['EDPA_VALIDATE_PATH']
script_dir = os.environ.get('SCRIPT_DIR', '')
sys.path.insert(0, script_dir)
try:
    from validate_syntax import validate_file
    errors = validate_file(path)
    for e in errors:
        print(f'EDPA: validation error: {e}', file=sys.stderr)
except Exception as exc:
    # Non-blocking hook: failure to validate must not block the user's edit.
    # Surface the cause on stderr so debugging is still possible without
    # polluting stdout (which Claude Code may parse for hook output).
    print(f'EDPA: validate hook internal error: {exc}', file=sys.stderr)
"
# stderr stays on stderr — earlier '2>&1' redirected validation errors
# into stdout, which made Claude Code render them as if they were tool
# output rather than diagnostics.

# Iteration-schema validation: when the edited file is .edpa/iterations/*.yaml,
# also run the structural validator so date gaps, weeks mismatches, etc.
# surface immediately. Non-blocking — exit 0 even if errors are found.
case "$FILE_PATH" in
    */.edpa/iterations/*.yaml)
        VALIDATOR="$SCRIPT_DIR/validate_iterations.py"
        if [ -f "$VALIDATOR" ]; then
            python3 "$VALIDATOR" 2>&1 | grep -E "^(✗|⚠)" | sed 's/^/EDPA: /' >&2 || true
        fi
        ;;
esac

exit 0
