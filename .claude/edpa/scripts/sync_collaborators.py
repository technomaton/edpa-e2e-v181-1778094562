#!/usr/bin/env python3
"""Reconcile .edpa/config/people.yaml against the GitHub repository's
collaborator list.

Two modes mirroring strategy "D" (asymmetric):

  * REMOVE — collaborator is no longer on GitHub.
    Person stays in people.yaml (their work history matters), but their
    `availability` flips to ``unavailable`` and a comment notes the date.
    Auto-applied; commit-friendly because the operation is purely a
    factual update.

  * ADD — collaborator is new on GitHub but missing from people.yaml.
    A stub entry is appended, auto-filled with what `gh api users/{login}`
    can tell us (name, email when public, login itself), and the rest
    (role, team, fte, capacity_per_iteration) is left blank for the
    maintainer to fill in. Designed to be merged via PR review so a
    human assigns role/FTE before the person starts getting credited
    capacity.

Usage:
    sync_collaborators.py status                # report only, no writes
    sync_collaborators.py apply                 # write changes to people.yaml
    sync_collaborators.py apply --auto-add      # also auto-add stubs
    sync_collaborators.py apply --no-removes    # skip availability changes
    sync_collaborators.py apply --json          # emit summary JSON

Designed to be safe to run unattended in a workflow:
    --remove-only is the recommended workflow flag (auto-flip availability
    on remove events; let a PR carry add events for human review).
"""
from __future__ import annotations

import argparse
import json
import logging
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml",
          file=sys.stderr)
    sys.exit(1)

# ruamel.yaml is used ONLY for the read-modify-write cycle on
# .edpa/config/people.yaml so the file's comments, blank lines, key
# order, and quoting style survive the sync. Everywhere else the
# toolchain reads YAML for analysis only and PyYAML stays in charge.
try:
    from ruamel.yaml import YAML  # noqa: E402
    _RUAMEL = YAML()
    _RUAMEL.preserve_quotes = True
    # Match the indent style other tools in the toolchain emit:
    # - block sequences sit one indent in from the parent mapping key
    # - mapping keys at 2 spaces per level
    # offset=2 + sequence=4 keeps `- id: alice` aligned 2 spaces under
    # the `people:` key, which is what hand-edited people.yaml files use.
    _RUAMEL.indent(mapping=2, sequence=4, offset=2)
    _RUAMEL.allow_unicode = True
    _RUAMEL.width = 4096   # avoid silent line-wrap rewrites
except ImportError:
    print("ERROR: ruamel.yaml required for round-trip writes. "
          "Install with: pip install ruamel.yaml", file=sys.stderr)
    sys.exit(1)


def _gh(args: list[str]) -> "str | None":
    """Run `gh ...` and return stdout. None on non-zero exit."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.error("gh subprocess failed: %s", exc)
        return None
    if r.returncode != 0:
        logger.warning("gh %s → exit %d: %s",
                       " ".join(shlex.quote(a) for a in args),
                       r.returncode, r.stderr.strip())
        return None
    return r.stdout


def list_collaborators(repo: str) -> "list[dict] | None":
    """Return the repo's collaborator list as a list of dicts. None on
    failure (auth, rate limit, missing repo)."""
    out = _gh(["api", f"repos/{repo}/collaborators",
               "--paginate", "-H", "Accept: application/vnd.github+json"])
    if out is None:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # --paginate joins JSON arrays — split on `][` boundary.
        try:
            data = json.loads(out.replace("][", ","))
        except json.JSONDecodeError:
            logger.error("could not parse collaborator JSON")
            return None
    if not isinstance(data, list):
        return None
    return data


def fetch_user_profile(login: str) -> dict:
    """Pull what's publicly available for a github user. Best-effort —
    blanks on failure are fine, the maintainer fills the rest."""
    out = _gh(["api", f"users/{login}"])
    if out is None:
        return {}
    try:
        return json.loads(out) or {}
    except json.JSONDecodeError:
        return {}


def _propose_id(login: str, existing_ids: set[str]) -> str:
    """Pick an internal id for a new person. Lowercase login is the
    canonical default; if it collides, append `-2`, `-3`, … until free."""
    base = login.lower().replace("_", "-")
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}-{n}" in existing_ids:
        n += 1
    return f"{base}-{n}"


def diff(people: list[dict], collaborators: list[dict]) -> dict:
    """Return three buckets: ``adds`` (collaborator not in people.yaml),
    ``removes`` (person with github login no longer on the repo),
    ``unchanged`` (matched). Both buckets carry login + person dict
    references so the apply step can write back without reloading."""
    by_login: dict[str, dict] = {p["github"].lower(): p
                                  for p in people
                                  if p.get("github")}
    repo_logins = {c["login"].lower(): c for c in collaborators if c.get("login")}

    adds = []
    for login_lower, c in repo_logins.items():
        if login_lower in by_login:
            continue
        adds.append({"login": c["login"], "collaborator": c})

    removes = []
    for login_lower, person in by_login.items():
        if login_lower in repo_logins:
            continue
        # Already flagged as unavailable — skip
        if person.get("availability") == "unavailable":
            continue
        removes.append({"login": person["github"], "person": person})

    unchanged = []
    for login_lower in by_login.keys() & repo_logins.keys():
        unchanged.append({
            "login": by_login[login_lower]["github"],
            "person": by_login[login_lower],
        })

    return {"adds": adds, "removes": removes, "unchanged": unchanged}


def make_stub(login: str, profile: dict, existing_ids: set[str]) -> dict:
    """Build the new people.yaml entry. Auto-fills name + email when
    public; leaves role/team/fte/capacity blank for maintainer review."""
    pid = _propose_id(login, existing_ids)
    name = profile.get("name") or login
    email = profile.get("email") or ""
    return {
        "id": pid,
        "name": name,
        "role": "",
        "team": "",
        "fte": 0.0,
        "capacity_per_iteration": 0,
        "email": email,
        "github": login,
        "availability": "confirmed",
    }


def apply_removes(people: list[dict], removes: list[dict]) -> int:
    """Flip availability to ``unavailable`` for each removed collaborator.
    Returns count of mutations."""
    n = 0
    today = date.today().isoformat()
    for rec in removes:
        person = rec["person"]
        person["availability"] = "unavailable"
        person["availability_changed"] = today
        n += 1
    return n


def apply_adds(people: list[dict], adds: list[dict]) -> int:
    """Append stub entries for new collaborators, auto-filling from gh
    api users/{login}. Returns count of additions."""
    existing_ids = {p["id"] for p in people if p.get("id")}
    n = 0
    for rec in adds:
        login = rec["login"]
        profile = fetch_user_profile(login)
        stub = make_stub(login, profile, existing_ids)
        existing_ids.add(stub["id"])
        people.append(stub)
        n += 1
    return n


def load_people_yaml_round_trip(path: Path) -> dict:
    """Load people.yaml in a comment-preserving mode. The returned object
    is a ruamel CommentedMap that behaves like a dict for read/write but
    carries comment, key-order, and quoting metadata so a later
    ``write_people_yaml`` round-trips them faithfully."""
    with open(path, encoding="utf-8") as f:
        doc = _RUAMEL.load(f)
    return doc or {}


def write_people_yaml(path: Path, doc: dict) -> None:
    """Persist people.yaml using ruamel round-trip — comments, blank
    lines, key order, and quoting style on entries we did not touch
    survive the sync."""
    with open(path, "w", encoding="utf-8") as f:
        _RUAMEL.dump(doc, f)


def find_edpa_root(start: Path) -> "Path | None":
    p = start.resolve()
    if p.name == ".edpa" and p.is_dir():
        return p
    if (p / ".edpa").is_dir():
        return p / ".edpa"
    while p != p.parent:
        if (p / ".edpa").is_dir():
            return p / ".edpa"
        p = p.parent
    return None


def resolve_repo_from_config(edpa_root: Path) -> "str | None":
    """Read sync.github_org / sync.github_repo from edpa.yaml."""
    cfg_path = edpa_root / "config" / "edpa.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    sync = cfg.get("sync") or {}
    org = sync.get("github_org")
    repo = sync.get("github_repo")
    if org and repo:
        return f"{org}/{repo}"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    common_args = argparse.ArgumentParser(add_help=False)
    common_args.add_argument("--edpa-root", default=".",
                             help="Path to project root or .edpa/ dir")
    common_args.add_argument("--repo", help="owner/repo (default: read from edpa.yaml)")
    common_args.add_argument("--json", action="store_true",
                             help="Emit a JSON summary instead of human text")

    p_status = sub.add_parser("status", parents=[common_args],
                              help="Diff repo collaborators vs people.yaml; no writes.")

    p_apply = sub.add_parser("apply", parents=[common_args],
                             help="Apply remove diffs (and add diffs with --auto-add).")
    p_apply.add_argument("--auto-add", action="store_true",
                         help="Also append stub entries for new collaborators "
                              "(default: only mark removes as unavailable; "
                              "leave adds for human PR review).")
    p_apply.add_argument("--no-removes", action="store_true",
                         help="Skip availability flips for removed collaborators.")

    args = ap.parse_args(argv)

    edpa_root = find_edpa_root(Path(args.edpa_root))
    if edpa_root is None:
        print(f"ERROR: no .edpa/ found from {args.edpa_root}", file=sys.stderr)
        return 1

    repo = args.repo or resolve_repo_from_config(edpa_root)
    if not repo:
        print("ERROR: no --repo given and edpa.yaml has no sync.github_org / "
              "sync.github_repo configured", file=sys.stderr)
        return 1

    people_path = edpa_root / "config" / "people.yaml"
    try:
        doc = load_people_yaml_round_trip(people_path)
    except (OSError, yaml.YAMLError) as exc:
        print(f"ERROR: cannot read {people_path}: {exc}", file=sys.stderr)
        return 1
    people = doc.get("people", []) or []

    collabs = list_collaborators(repo)
    if collabs is None:
        print(f"ERROR: could not fetch collaborators for {repo}",
              file=sys.stderr)
        return 1

    diffs = diff(people, collabs)

    if args.cmd == "status":
        if args.json:
            print(json.dumps({
                "repo": repo,
                "adds": [d["login"] for d in diffs["adds"]],
                "removes": [d["login"] for d in diffs["removes"]],
                "unchanged": [d["login"] for d in diffs["unchanged"]],
            }, indent=2))
        else:
            print(f"Repo:        {repo}")
            print(f"Adds:        {len(diffs['adds'])}")
            for d in diffs["adds"]:
                print(f"  + {d['login']}")
            print(f"Removes:     {len(diffs['removes'])}")
            for d in diffs["removes"]:
                print(f"  - {d['login']}  ({d['person'].get('id')})")
            print(f"Unchanged:   {len(diffs['unchanged'])}")
        return 0

    # apply
    n_removes = 0
    n_adds = 0
    if not args.no_removes:
        n_removes = apply_removes(people, diffs["removes"])
    if args.auto_add:
        n_adds = apply_adds(people, diffs["adds"])

    if n_removes or n_adds:
        doc["people"] = people
        write_people_yaml(people_path, doc)

    summary = {
        "repo": repo,
        "removes_applied": n_removes,
        "adds_applied": n_adds,
        "adds_pending_review": [d["login"] for d in diffs["adds"]] if not args.auto_add else [],
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Repo:                  {repo}")
        print(f"Removes applied:       {n_removes}")
        print(f"Adds applied:          {n_adds}")
        if not args.auto_add and diffs["adds"]:
            print(f"Adds pending review:   {len(diffs['adds'])}")
            for d in diffs["adds"]:
                print(f"  ? {d['login']}")
            print("(re-run with --auto-add to append stubs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
