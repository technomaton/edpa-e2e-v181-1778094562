#!/usr/bin/env python3
"""
EDPA Contributor Auto-Detection

Two modes:

1. CI mode (default — used by contributor-detect.yml GitHub Action):
   reads PR_NUMBER / PR_AUTHOR / PR_TITLE / PR_BRANCH from the environment
   and updates contributors[] for every item ID referenced in that PR.

2. CLI / audit mode:
     detect_contributors.py --item S-200 --since 7days
     detect_contributors.py --pr 42
   Walks merged PRs touching the named item (or scoped to a single PR
   number) and updates the same contributors[] field. Use --dry-run to
   see what would change without touching backlog YAMLs.

Evidence signals detected:
  - PR author → key contributor
  - PR reviewers → reviewer
  - Commit authors → reviewer (when distinct from PR author)
  - Item IDs from branch name, PR title, and commit messages

Environment variables (CI mode):
  PR_NUMBER, PR_AUTHOR, PR_TITLE, PR_BRANCH, GH_TOKEN
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required")
    sys.exit(1)


def run_gh(args, *, repo: str | None = None):
    """Run gh CLI command and return JSON output."""
    cmd = ["gh"] + list(args)
    if repo and "--repo" not in cmd:
        cmd.extend(["--repo", repo])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gh error: {result.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(result.stdout) if result.stdout.strip() else None


def extract_item_ids(text):
    """Extract EDPA item IDs (S-200, F-100, E-10, I-1, T-3, D-2) from text."""
    return re.findall(r'\b([SFEITD]-\d+)\b', text or "")


# Map type prefix → backlog directory
PREFIX_TO_DIR = {
    "S": "stories",
    "F": "features",
    "E": "epics",
    "I": "initiatives",
    "T": "tasks",
    "D": "defects",
}


def find_backlog_file(edpa_root: Path, item_id: str):
    """Find the YAML file for an item ID."""
    prefix = item_id.split("-")[0]
    type_dir = PREFIX_TO_DIR.get(prefix, "stories")
    path = edpa_root / "backlog" / type_dir / f"{item_id}.yaml"
    if path.exists():
        return path
    for d in PREFIX_TO_DIR.values():
        p = edpa_root / "backlog" / d / f"{item_id}.yaml"
        if p.exists():
            return p
    return None


def load_people_map(edpa_root: Path):
    """Load people.yaml and create github_login → person_id map."""
    people_path = edpa_root / "config" / "people.yaml"
    if not people_path.exists():
        return {}
    data = yaml.safe_load(people_path.read_text()) or {}
    mapping = {}
    for p in data.get("people", []):
        pid = p.get("id", "")
        email = p.get("email", "")
        name = p.get("name", "")
        github = p.get("github", "")
        if github:
            mapping[github.lower()] = pid
        if email:
            mapping[email.lower()] = pid
        if name:
            mapping[name.lower()] = pid
    return mapping


def update_contributors(yaml_path: Path, new_contributors: list, *, dry_run=False):
    """Update contributors list in a backlog YAML file.

    Only adds new contributors — never removes existing ones.
    If a contributor already exists with a higher CW, keeps the higher
    value. Returns True when something changed. New entries use the v1.7
    schema (`as:` for evidence role, `cw:` for weight).
    """
    data = yaml.safe_load(yaml_path.read_text()) or {}
    existing = data.get("contributors", []) or []

    existing_map = {}
    for c in existing:
        existing_map[c["person"]] = c

    changed = False
    for nc in new_contributors:
        person = nc["person"]
        if person in existing_map:
            if existing_map[person].get("cw", 0) < nc.get("cw", 0):
                existing_map[person]["cw"] = nc["cw"]
                existing_map[person]["as"] = nc["as"]
                changed = True
        else:
            existing.append(nc)
            existing_map[person] = nc
            changed = True

    if changed and not dry_run:
        data["contributors"] = existing
        yaml_path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True)
        )
    return changed


def _parse_relative_since(since: str) -> datetime | None:
    """Convert '7days' / '2weeks' / '1month' / 'YYYY-MM-DD' into UTC datetime."""
    if not since:
        return None
    s = since.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(day|days|d|week|weeks|w|month|months|m)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("d"):
            delta = timedelta(days=n)
        elif unit.startswith("w"):
            delta = timedelta(weeks=n)
        else:  # month — approximate as 30 days
            delta = timedelta(days=30 * n)
        return datetime.now(timezone.utc) - delta
    try:
        return datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def gather_pr_evidence(repo: str, pr_number: str | int):
    """Pull author / reviewers / commit authors / item IDs for a single PR."""
    info = {"author": "", "title": "", "branch": "", "reviewers": set(),
            "commit_authors": set(), "item_ids": set()}
    pr = run_gh(["pr", "view", str(pr_number),
                 "--json", "author,title,headRefName,commits,reviews"],
                repo=repo)
    if not pr:
        return info
    info["author"] = pr.get("author", {}).get("login", "") or ""
    info["title"] = pr.get("title", "") or ""
    info["branch"] = pr.get("headRefName", "") or ""
    info["item_ids"].update(extract_item_ids(info["title"]))
    info["item_ids"].update(extract_item_ids(info["branch"]))
    for c in pr.get("commits", []) or []:
        msg = (c.get("messageHeadline", "") + " "
               + (c.get("messageBody") or ""))
        info["item_ids"].update(extract_item_ids(msg))
        for field in ("authors", "committers"):
            for a in c.get(field, []) or []:
                login = a.get("login", "")
                if login and login != info["author"]:
                    info["commit_authors"].add(login)
    for r in pr.get("reviews", []) or []:
        login = r.get("author", {}).get("login", "")
        if login and login != info["author"]:
            info["reviewers"].add(login)
    return info


def find_prs_touching_item(repo: str, item_id: str, since: datetime):
    """Best-effort: list merged PRs whose title/branch references item_id
    since the given timestamp. Uses gh search for portability.

    `gh search prs` wants `--repo OWNER/NAME` as a flag plus the rest of
    the query as positional terms; baking everything into one big
    "repo:owner/name ..." string causes the CLI to wrap the lot in
    quotes and reject it as an invalid query.
    """
    since_iso = since.strftime("%Y-%m-%d")
    query = f"is:pr is:merged merged:>={since_iso} {item_id}"
    res = run_gh(["search", "prs", "--repo", repo,
                  "--json", "number,title", "--limit", "100", query])
    return res or []


def process_pr_evidence(edpa_root: Path, repo: str, pr_number: str,
                        scope_item_id: str | None = None,
                        dry_run: bool = False):
    """Update backlog YAMLs for every item referenced by the given PR.

    If scope_item_id is given, only that one item is updated (and only if
    it appears in the PR's references).
    """
    info = gather_pr_evidence(repo, pr_number)
    if not info["author"]:
        print(f"PR #{pr_number}: not found or no author info", file=sys.stderr)
        return 0

    print(f"PR #{pr_number} by {info['author']}: {info['title']}")
    if info["reviewers"]:
        print(f"  reviewers: {sorted(info['reviewers'])}")
    if info["commit_authors"]:
        print(f"  commit authors: {sorted(info['commit_authors'])}")
    target_ids = sorted(info["item_ids"])
    if scope_item_id:
        target_ids = [scope_item_id] if scope_item_id in info["item_ids"] else []
    if not target_ids:
        print("  no item IDs to credit")
        return 0
    print(f"  items: {target_ids}")

    people_map = load_people_map(edpa_root)
    heuristics_path = edpa_root / "config" / "heuristics.yaml"
    weights = {"owner": 1.0, "key": 0.6, "reviewer": 0.25, "consulted": 0.15}
    if heuristics_path.exists():
        h = yaml.safe_load(heuristics_path.read_text()) or {}
        weights.update(h.get("role_weights") or {})

    def resolve(login):
        return people_map.get(login.lower(), login)

    updated = 0
    for item_id in target_ids:
        yaml_path = find_backlog_file(edpa_root, item_id)
        if not yaml_path:
            print(f"  {item_id}: no YAML file found, skipping")
            continue
        new_contribs = []
        if info["author"]:
            new_contribs.append({
                "person": resolve(info["author"]),
                "as": "key",
                "cw": weights.get("key", 0.6),
                "source": f"pr_author:#{pr_number}",
            })
        for login in sorted(info["reviewers"]):
            new_contribs.append({
                "person": resolve(login),
                "as": "reviewer",
                "cw": weights.get("reviewer", 0.25),
                "source": f"pr_reviewer:#{pr_number}",
            })
        for login in sorted(info["commit_authors"]):
            new_contribs.append({
                "person": resolve(login),
                "as": "reviewer",
                "cw": weights.get("reviewer", 0.25),
                "source": f"commit_author:#{pr_number}",
            })
        changed = update_contributors(yaml_path, new_contribs, dry_run=dry_run)
        verb = "would update" if dry_run else ("updated" if changed else "unchanged")
        print(f"  {item_id}: {len(new_contribs)} contributors → {verb}")
        if changed:
            updated += 1
    return updated


def detect_repo_from_config(edpa_root: Path) -> str | None:
    cfg = edpa_root / "config" / "edpa.yaml"
    if not cfg.exists():
        return None
    data = yaml.safe_load(cfg.read_text()) or {}
    sync = data.get("sync") or {}
    org = sync.get("github_org")
    repo = sync.get("github_repo")
    if org and repo:
        return f"{org}/{repo}"
    return None


def cli_audit_mode(edpa_root: Path, repo: str, item_id: str | None,
                   since: str, dry_run: bool):
    since_dt = _parse_relative_since(since)
    if not since_dt:
        print(f"ERROR: cannot parse --since {since!r} (use 7days / 2weeks / "
              f"1month / YYYY-MM-DD)", file=sys.stderr)
        sys.exit(2)
    if item_id:
        prs = find_prs_touching_item(repo, item_id, since_dt)
    else:
        # Walk every merged PR in the window so we credit all items.
        since_iso = since_dt.strftime("%Y-%m-%d")
        prs = run_gh(["search", "prs", "--repo", repo,
                      "--json", "number,title", "--limit", "100",
                      f"is:pr is:merged merged:>={since_iso}"]) or []
    if not prs:
        print(f"No merged PRs found in window since {since_dt.isoformat()}")
        return 0
    print(f"Found {len(prs)} PR(s) since {since_dt.date()}")
    total_updated = 0
    for pr in prs:
        total_updated += process_pr_evidence(
            edpa_root, repo, str(pr["number"]),
            scope_item_id=item_id, dry_run=dry_run)
    return total_updated


def ci_mode(edpa_root: Path, repo: str, dry_run: bool):
    pr_number = os.environ.get("PR_NUMBER", "")
    if not pr_number:
        print("ERROR: PR_NUMBER not set. Use --pr <N> or --item <ID> for CLI mode.",
              file=sys.stderr)
        sys.exit(1)
    return process_pr_evidence(edpa_root, repo, pr_number, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(
        description="EDPA contributor auto-detection (CI + CLI audit modes)"
    )
    parser.add_argument("--pr", help="PR number to scan (CLI alternative to PR_NUMBER env var)")
    parser.add_argument("--item", help="Restrict updates to a single item ID (e.g. S-200)")
    parser.add_argument(
        "--since",
        help="Look-back window for --item / audit mode "
             "(e.g. 7days, 2weeks, 1month, YYYY-MM-DD). Default: 30days",
        default="30days",
    )
    parser.add_argument("--repo",
                        help="GitHub owner/repo (default: read from .edpa/config/edpa.yaml)")
    parser.add_argument("--edpa-root", default=".edpa",
                        help="Path to .edpa/ directory (default: .edpa)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing YAML")
    args = parser.parse_args()

    edpa_root = Path(args.edpa_root)
    if not edpa_root.exists():
        print(f"ERROR: {edpa_root} not found", file=sys.stderr)
        sys.exit(1)

    repo = args.repo or detect_repo_from_config(edpa_root) or os.environ.get("GH_REPO", "")
    if not repo:
        print("ERROR: cannot determine repo. Pass --repo owner/name or configure "
              "sync.github_org + sync.github_repo in .edpa/config/edpa.yaml.",
              file=sys.stderr)
        sys.exit(1)

    if args.pr:
        process_pr_evidence(edpa_root, repo, args.pr,
                            scope_item_id=args.item,
                            dry_run=args.dry_run)
        return

    if args.item or os.environ.get("PR_NUMBER", "") == "":
        # CLI / audit mode
        if not args.item:
            print("ERROR: provide --item <ID> or set PR_NUMBER (CI mode)",
                  file=sys.stderr)
            sys.exit(1)
        cli_audit_mode(edpa_root, repo, args.item, args.since, args.dry_run)
        return

    # Fallback to CI mode (PR_NUMBER set)
    ci_mode(edpa_root, repo, args.dry_run)


if __name__ == "__main__":
    main()
