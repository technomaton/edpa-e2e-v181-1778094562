"""Shared helper for auto-committing EDPA-managed config / state files.

Why this module exists
----------------------
Several scripts (project_setup.py, sync.py push, sync.py setup-refresh)
mutate `.edpa/config/edpa.yaml`, `.edpa/config/issue_map.yaml`, and
`.edpa/iterations/*.yaml`. Those mutations contain GitHub Project IDs,
field IDs, and option IDs — without them the next sync push silently
pushes against the wrong project. The 2026-05-06 v1.8.0-beta E2E run
hit this directly: the maintainer ran setup, made an unrelated PR,
merged it, and the post-merge `git pull --ff-only` collided with the
uncommitted setup state, leaving the working tree without `field_ids`.

Auto-committing the specific files we mutate (with `git add <paths>`
NOT `git add -a`) is the smallest fix that doesn't drag unrelated
in-progress work into the same commit.

Public API
----------
- ``maybe_commit(paths, message, *, root=None, dry_run=False)`` —
  best-effort commit. Returns one of "committed" | "no-op" | "skipped".
  Never raises.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable


def _run(args: list[str], cwd: Path):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)


def _is_git_repo(root: Path) -> bool:
    res = _run(["git", "rev-parse", "--is-inside-work-tree"], root)
    return res.returncode == 0 and res.stdout.strip() == "true"


def _has_diff(paths: list[Path], root: Path) -> bool:
    """True if any of `paths` differs from HEAD or is untracked."""
    rels = [str(p.relative_to(root)) if p.is_absolute() else str(p)
            for p in paths]
    if not rels:
        return False
    diff = _run(["git", "status", "--porcelain", "--"] + rels, root)
    return bool(diff.stdout.strip())


def _resolve_user(root: Path) -> tuple[str, str] | None:
    """Return (name, email) from git config; None when either is missing.

    We deliberately don't synthesize a fallback identity here. If the
    repo has no `user.name` / `user.email` set, auto-commit skips and
    the caller prints the manual command — it's better to surface the
    missing identity to the operator than to commit under a synthetic
    name that obscures who actually ran the script.
    """
    name_res = _run(["git", "config", "user.name"], root)
    email_res = _run(["git", "config", "user.email"], root)
    name = (name_res.stdout or "").strip()
    email = (email_res.stdout or "").strip()
    if name and email:
        return name, email
    return None


def maybe_commit(
    paths: Iterable[str | os.PathLike],
    message: str,
    *,
    root: str | os.PathLike | None = None,
    dry_run: bool = False,
) -> str:
    """Stage `paths` (only those that exist) and commit them.

    Returns:
        "committed"  — a new commit was created.
        "no-op"      — nothing to commit (paths matched HEAD).
        "skipped"    — not a git repo / disabled / git unavailable / error.

    Never raises. Designed to be a no-op in environments where git is
    missing or not appropriate (e.g., running engine on a tarball).
    """
    root_path = Path(root) if root else Path.cwd()
    if not _is_git_repo(root_path):
        return "skipped"

    resolved = []
    for p in paths:
        ap = Path(p)
        if not ap.is_absolute():
            ap = root_path / ap
        if ap.exists():
            resolved.append(ap)
    if not resolved:
        return "no-op"

    if not _has_diff(resolved, root_path):
        return "no-op"

    rels = [str(p.relative_to(root_path)) for p in resolved]
    if dry_run:
        return "committed"  # caller logs intent; we don't actually run anything

    add = _run(["git", "add", "--"] + rels, root_path)
    if add.returncode != 0:
        return "skipped"

    user = _resolve_user(root_path)
    if user is None:
        return "skipped"
    name, email = user

    commit_args = [
        "git",
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        "commit", "-m", message, "--",
    ] + rels
    res = _run(commit_args, root_path)
    if res.returncode != 0:
        return "skipped"
    return "committed"
