#!/usr/bin/env python3
"""
EDPA Commit Info — enriches commit metadata with EDPA context.

Resolves:
  - person: from people.yaml via git config email/name
  - evidence: from cw_heuristics.yaml
  - item: from .edpa/backlog/ matching branch or diff content

Also re-exports compute_cw from engine for convenience.
"""

import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml")
    sys.exit(1)

# Re-export compute_cw from engine
sys.path.insert(0, str(Path(__file__).parent))
from engine import compute_cw  # noqa: E402, F401


def git_config(key):
    """Get a git config value, or None."""
    try:
        result = subprocess.run(
            ["git", "config", key],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def git_branch():
    """Get the current git branch name, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def git_diff_staged():
    """Get the staged diff, or empty string."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


_GH_NOREPLY_RE = re.compile(r"^(?:\d+\+)?(?P<login>[A-Za-z0-9-]+)@users\.noreply\.github\.com$")


def _github_login_from_email(email):
    """Extract the GitHub login from a `noreply` commit email.

    GitHub-routed commits use `<login>@users.noreply.github.com` (or the
    privacy-protected form `<id>+<login>@users.noreply.github.com`). Pull
    the login out so callers can match against people.yaml's github field.
    Returns None when the email is not a GitHub noreply address.
    """
    if not email:
        return None
    m = _GH_NOREPLY_RE.match(email.strip())
    return m.group("login") if m else None


def resolve_person(people, email=None, name=None):
    """Resolve a person from people list by GitHub login, email, or name.

    Match priority (highest first):
      1. github field matches the login extracted from a GitHub noreply
         email — most reliable when commits go through GitHub UI / web edits
      2. github field matches the git user.name as a literal handle
      3. email field matches git email exactly
      4. id matches email prefix (before @)
      5. id matches git user.name (case-insensitive)

    Returns the person dict or None.
    """
    if not people:
        return None

    # 1. GitHub noreply email → login → people.yaml.github
    gh_from_email = _github_login_from_email(email)
    if gh_from_email:
        for person in people:
            if person.get("github") and person["github"].lower() == gh_from_email.lower():
                return person

    # 2. git user.name literally matches a github handle
    if name:
        for person in people:
            if person.get("github") and person["github"].lower() == name.lower():
                return person

    # 3. email field matches git email exactly
    if email:
        for person in people:
            if person.get("email") == email:
                return person

    # 4. id matches email prefix
    if email and "@" in email:
        prefix = email.split("@")[0]
        if prefix:
            for person in people:
                if person.get("id") == prefix:
                    return person

    # 5. id matches git user.name (case-insensitive)
    if name:
        for person in people:
            if person.get("id", "").lower() == name.lower():
                return person

    return None


def load_people(edpa_root):
    """Load people from .edpa/config/people.yaml, or return empty list."""
    people_path = Path(edpa_root) / "config" / "people.yaml"
    try:
        data = yaml.safe_load(people_path.read_text())
        return data.get("people", []) if data else []
    except (FileNotFoundError, yaml.YAMLError):
        return []


def load_heuristics(edpa_root):
    """Load heuristics from .edpa/config/cw_heuristics.yaml, or return None."""
    for name in ("cw_heuristics.yaml", "heuristics.yaml"):
        path = Path(edpa_root) / "config" / name
        try:
            return yaml.safe_load(path.read_text())
        except (FileNotFoundError, yaml.YAMLError):
            continue
    return None


def find_backlog_item(edpa_root, branch=None, diff=None):
    """Find a matching backlog item from branch name or diff content.

    Looks for item references like S-123, F-45, E-7 in branch name or diff.
    Returns the item ID string or None.
    """
    backlog_dir = Path(edpa_root) / "backlog"
    if not backlog_dir.is_dir():
        return None

    # Collect all known item IDs from backlog files
    known_ids = set()
    for yaml_file in backlog_dir.rglob("*.yaml"):
        known_ids.add(yaml_file.stem)

    # Extract references from branch and diff
    text = ""
    if branch:
        text += branch + " "
    if diff:
        text += diff

    refs = re.findall(r'[SFEITD]-\d+', text)

    for ref in refs:
        if ref in known_ids:
            return ref

    return None


def get_commit_info(edpa_root=None):
    """Build commit info dict with EDPA context.

    Returns dict with keys: branch, diff, person, evidence, item
    """
    if edpa_root is None:
        edpa_root = ".edpa"

    branch = git_branch()
    diff = git_diff_staged()

    # Resolve person
    email = git_config("user.email")
    name = git_config("user.name")
    people = load_people(edpa_root)
    person = resolve_person(people, email=email, name=name)

    # Load heuristics for evidence context
    heuristics = load_heuristics(edpa_root)

    # Find backlog item
    item = find_backlog_item(edpa_root, branch=branch, diff=diff)

    return {
        "schema": "edpa-commit-info/1.0",
        "branch": branch,
        "diff": diff,
        "person": person,
        "evidence": heuristics,
        "item": item,
    }


def main():
    import json
    info = get_commit_info()
    print(json.dumps(info, indent=2, default=str))


if __name__ == "__main__":
    main()
