#!/bin/bash
# Install EDPA git hooks
ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$ROOT/.claude/edpa/scripts/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
    echo "ERROR: Hooks directory not found at $HOOKS_DIR"
    echo "Make sure the EDPA plugin is installed."
    exit 1
fi

git config core.hooksPath "$HOOKS_DIR"
echo "Git hooks installed from $HOOKS_DIR"
