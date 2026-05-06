#!/usr/bin/env python3
"""
EDPA MCP Server — exposes .edpa/ project data to AI assistants.

Read-only server that provides structured access to EDPA configuration,
iterations, people, and backlog items. Works with any MCP client
(Claude Code, Cursor, Codex CLI, etc.).

Usage:
    python3 .claude/edpa/scripts/mcp_server.py

Environment:
    EDPA_ROOT       Override .edpa/ lookup (default: walk up from cwd)
    EDPA_LOG_LEVEL  DEBUG | INFO (default) | WARNING | ERROR
    EDPA_LOG_FILE   Optional path; falls back to stderr only
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml",
          file=sys.stderr)
    sys.exit(1)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Resource, TextContent, Tool
except ImportError:
    print("ERROR: 'mcp' package required. Install with: pip install mcp",
          file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging — stderr only (stdout is reserved for JSON-RPC)
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    level_name = os.environ.get("EDPA_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log = logging.getLogger("edpa.mcp")
    log.setLevel(level)
    if log.handlers:
        return log
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s edpa.mcp %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    log.addHandler(stderr_handler)
    log_file = os.environ.get("EDPA_LOG_FILE")
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            log.addHandler(file_handler)
        except OSError as exc:
            log.warning("Could not open EDPA_LOG_FILE=%s (%s); stderr only",
                        log_file, exc)
    return log


logger = _setup_logging()

# ---------------------------------------------------------------------------
# Server identity (version comes from plugin.json — single source of truth)
# ---------------------------------------------------------------------------

def _read_plugin_version() -> str:
    """Read version from plugin.json next to the script's plugin root.

    Walks up from this file: scripts/mcp_server.py -> edpa/ -> plugin root,
    where .claude-plugin/plugin.json lives. Falls back to "unknown" if the
    manifest is missing (e.g. running from a checkout without symlinks).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        manifest = parent / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            try:
                return json.loads(manifest.read_text()).get("version", "unknown")
            except (OSError, ValueError):
                return "unknown"
    return "unknown"


SERVER_VERSION = _read_plugin_version()

# ---------------------------------------------------------------------------
# Input validation — guards against path traversal in item_id parameter
# ---------------------------------------------------------------------------

# Item IDs are <type-prefix>-<digits>, e.g. S-200, F-12, I-1, D-3, T-99.
ITEM_ID_RE = re.compile(r"^[A-Z]-\d{1,9}$")


def _safe_item_id(item_id: str) -> str | None:
    """Return item_id if it matches the allowed shape, else None."""
    if not isinstance(item_id, str):
        return None
    return item_id if ITEM_ID_RE.match(item_id) else None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def find_edpa_root() -> Path | None:
    """Find .edpa/ directory. Checks EDPA_ROOT env var first, then walks up from CWD."""
    env_root = os.environ.get("EDPA_ROOT")
    if env_root:
        p = Path(env_root)
        if p.is_dir():
            return p
    p = Path.cwd()
    while p != p.parent:
        if (p / ".edpa").is_dir():
            return p / ".edpa"
        p = p.parent
    return None


# Bounded LRU cache for parsed YAML, keyed by (path, st_mtime_ns).
# Repeated `tools/call` against an unchanged backlog is the common case
# (Claude Code asks "what's in PI-X?" then immediately "show me S-1
# from there?"); without a cache each invocation re-parses every YAML
# file from scratch. Bound at 64 entries — large enough for a 3-level
# hierarchy plus per-iteration files, small enough that a one-shot
# scan of a 1000-item backlog can't balloon resident memory.
_LOAD_YAML_CACHE: "OrderedDict[Path, tuple[int, dict]]" = OrderedDict()
_LOAD_YAML_CACHE_MAX = 64


def _load_yaml_cache_clear() -> None:
    """Test helper — drop all cached entries."""
    _LOAD_YAML_CACHE.clear()


def load_yaml(path: Path) -> dict | None:
    """Load a YAML file, return None on failure.

    Caches parsed contents keyed by (path, st_mtime_ns). On the next
    call against the same path:
      - if the file's mtime hasn't changed, returns the cached dict
      - if it has, re-reads, replaces the cache entry
      - if the file disappeared, returns None
    Cache is bounded; the least-recently-used entry is evicted when
    the cap is reached. Specific exceptions only — KeyboardInterrupt
    / SystemExit propagate.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("load_yaml stat(%s) failed: %s", path, exc)
        return None

    cached = _LOAD_YAML_CACHE.get(path)
    if cached is not None and cached[0] == st.st_mtime_ns:
        # Move to end so it counts as recently-used for eviction.
        _LOAD_YAML_CACHE.move_to_end(path)
        return cached[1]

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("load_yaml(%s) failed: %s", path, exc)
        # Drop a stale cached version; next caller should re-attempt.
        _LOAD_YAML_CACHE.pop(path, None)
        return None

    # Insert / refresh; evict oldest when over the cap.
    _LOAD_YAML_CACHE[path] = (st.st_mtime_ns, data)
    _LOAD_YAML_CACHE.move_to_end(path)
    while len(_LOAD_YAML_CACHE) > _LOAD_YAML_CACHE_MAX:
        _LOAD_YAML_CACHE.popitem(last=False)
    return data

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = Server("edpa", version=SERVER_VERSION)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="edpa_status",
            description="Get EDPA project status: current PI, active iteration, team size, total capacity.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="edpa_iterations",
            description="List all iterations with id, status, dates, and type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status: closed, active, planned. Omit for all.",
                        "enum": ["closed", "active", "planned"],
                    }
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="edpa_people",
            description="List team members with id, name, role, FTE, capacity, and team.",
            inputSchema={
                "type": "object",
                "properties": {
                    "team": {
                        "type": "string",
                        "description": "Filter by team ID. Omit for all.",
                    }
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="edpa_backlog",
            description="List backlog items from .edpa/backlog/. Filterable by iteration, type, or status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "iteration": {
                        "type": "string",
                        "description": "Filter by iteration ID (e.g., PI-2026-1.3).",
                    },
                    "type": {
                        "type": "string",
                        "description": "Filter by item type.",
                        "enum": ["Story", "Feature", "Epic", "Initiative"],
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status (e.g., Done, In Progress, Planned).",
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="edpa_item",
            description="Get detail for a single backlog item by ID (e.g., S-200, F-100).",
            inputSchema={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "Item ID (e.g., S-200, F-100, E-10).",
                    }
                },
                "required": ["item_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="edpa_validate",
            description="Validate iterations/*.yaml continuity and schema. Returns errors and warnings.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="edpa_sync_people",
            description=("Diff GitHub repo collaborators against .edpa/config/people.yaml. "
                         "Read-only — reports adds/removes/unchanged. To apply, run "
                         "`python plugin/edpa/scripts/sync_collaborators.py apply` "
                         "or use the /edpa:sync-people skill."),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    logger.info("call_tool name=%s args=%s", name, arguments)
    edpa_root = find_edpa_root()
    if edpa_root is None:
        logger.warning("call_tool name=%s: .edpa/ not found", name)
        return [TextContent(type="text", text="ERROR: .edpa/ directory not found. Run `/edpa setup` first.")]

    try:
        if name == "edpa_status":
            return _handle_status(edpa_root)
        elif name == "edpa_iterations":
            return _handle_iterations(edpa_root, arguments.get("status"))
        elif name == "edpa_people":
            return _handle_people(edpa_root, arguments.get("team"))
        elif name == "edpa_backlog":
            return _handle_backlog(edpa_root, arguments.get("iteration"),
                                   arguments.get("type"), arguments.get("status"))
        elif name == "edpa_item":
            raw_id = arguments.get("item_id", "")
            safe_id = _safe_item_id(raw_id)
            if safe_id is None:
                logger.warning("edpa_item: rejected item_id=%r", raw_id)
                return [TextContent(type="text",
                                    text=f"ERROR: invalid item_id {raw_id!r}. "
                                         "Expected pattern: <type-prefix>-<digits>, "
                                         "e.g. S-200, F-12, I-1.")]
            return _handle_item(edpa_root, safe_id)
        elif name == "edpa_validate":
            return _handle_validate(edpa_root)
        elif name == "edpa_sync_people":
            return _handle_sync_people(edpa_root)
        logger.warning("call_tool: unknown tool %s", name)
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception:
        logger.exception("call_tool name=%s raised", name)
        return [TextContent(type="text",
                            text=f"ERROR: internal error in {name}; "
                                 "see server logs for details.")]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_status(edpa_root: Path) -> list[TextContent]:
    config = load_yaml(edpa_root / "config" / "edpa.yaml") or {}
    people_cfg = load_yaml(edpa_root / "config" / "people.yaml") or {}

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _pi_loader import derive_pis, find_active_pi, split_diagnostics  # noqa: E402

    pis, diags = derive_pis(edpa_root)
    _, warnings = split_diagnostics(diags)
    active_pi = find_active_pi(pis)
    iterations = active_pi.get("iterations", [])
    active = next((i for i in iterations if i.get("status") == "active"), None)
    closed_count = sum(1 for i in iterations if i.get("status") == "closed")

    people = people_cfg.get("people", [])
    total_capacity = sum(p.get("capacity_per_iteration") or p.get("capacity", 0) for p in people)

    # Project name lives in edpa.yaml (project.name). Older versions of
    # this server read it from people.yaml, which never had a project
    # section in any shipped template, so edpa_status reported
    # "project: unknown" forever. Fall back to people.yaml only for
    # legacy v0.x configs that still bundled both into one file.
    project = config.get("project") or people_cfg.get("project", {})
    iter_weeks = active_pi.get("iteration_weeks", 1)
    pi_iters = active_pi.get("pi_iterations", len(iterations))

    result = {
        "project": project.get("name", "unknown"),
        "current_pi": active_pi.get("id", "unknown"),
        "iterations_total": len(iterations),
        "iterations_closed": closed_count,
        "active_iteration": active["id"] if active else None,
        "active_iteration_start": active.get("start_date") if active else None,
        "active_iteration_end": active.get("end_date") if active else None,
        "team_size": len(people),
        "total_capacity_per_iteration": total_capacity,
        "cadence": f"{iter_weeks}-week iterations, {pi_iters * iter_weeks}-week PI ({pi_iters} iterations)",
    }
    if warnings:
        result["warnings"] = warnings
    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]


def _handle_iterations(edpa_root: Path, status_filter: str | None) -> list[TextContent]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _pi_loader import derive_pis, find_active_pi, split_diagnostics  # noqa: E402

    pis, diags = derive_pis(edpa_root)
    _, warnings = split_diagnostics(diags)
    active_pi = find_active_pi(pis)
    iterations = active_pi.get("iterations", [])

    if status_filter:
        iterations = [i for i in iterations if i.get("status") == status_filter]

    items = []
    for it in iterations:
        entry = {
            "id": it.get("id"),
            "status": it.get("status"),
            "start_date": it.get("start_date"),
            "end_date": it.get("end_date"),
            "weeks": it.get("weeks"),
        }
        if it.get("type"):
            entry["type"] = it["type"]
        results_path = edpa_root / "reports" / f"iteration-{it.get('id')}" / "edpa_results.json"
        entry["has_results"] = results_path.exists()
        items.append(entry)

    payload: dict = {"iterations": items}
    if warnings:
        payload["warnings"] = warnings
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _handle_validate(edpa_root: Path) -> list[TextContent]:
    """Run iteration + people validation, return structured report."""
    # Local import: keeps the optional helpers out of module-load path so the
    # MCP server can still start even if a plugin upgrade is mid-flight.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _pi_loader import derive_pis, split_diagnostics  # noqa: E402
    from _people_loader import validate_people  # noqa: E402

    pis, iter_diags = derive_pis(edpa_root)
    people_diags = validate_people(edpa_root)
    all_diags = list(iter_diags) + list(people_diags)
    errors, warnings = split_diagnostics(all_diags)
    payload = {
        "ok": not errors,
        "pi_count": len(pis),
        "iteration_count": sum(len(p.get("iterations", [])) for p in pis),
        "errors": errors,
        "warnings": warnings,
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _handle_sync_people(edpa_root: Path) -> list[TextContent]:
    """Read-only diff between repo collaborators and .edpa/config/people.yaml.

    Returns the same shape as `sync_collaborators.py status --json`.
    Apply paths (write) are intentionally not exposed via MCP — keep the
    server read-only; use the /edpa:sync-people skill or the CLI for that.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sync_collaborators import (  # noqa: E402
        diff, list_collaborators, resolve_repo_from_config,
    )

    people_cfg = load_yaml(edpa_root / "config" / "people.yaml") or {}
    people = people_cfg.get("people", []) or []

    repo = resolve_repo_from_config(edpa_root)
    if not repo:
        return [TextContent(type="text", text=json.dumps({
            "ok": False,
            "error": "edpa.yaml has no sync.github_org / sync.github_repo configured",
        }, indent=2, ensure_ascii=False))]

    collabs = list_collaborators(repo)
    if collabs is None:
        return [TextContent(type="text", text=json.dumps({
            "ok": False,
            "error": f"could not fetch collaborators for {repo} (gh auth / rate limit?)",
            "repo": repo,
        }, indent=2, ensure_ascii=False))]

    d = diff(people, collabs)
    payload = {
        "ok": True,
        "repo": repo,
        "adds": [a["login"] for a in d["adds"]],
        "removes": [{"login": r["login"], "person_id": r["person"].get("id")}
                    for r in d["removes"]],
        "unchanged_count": len(d["unchanged"]),
        "hint": ("Apply via `python plugin/edpa/scripts/sync_collaborators.py "
                 "apply --auto-add` or the /edpa:sync-people skill — MCP is "
                 "read-only.") if (d["adds"] or d["removes"]) else None,
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _handle_people(edpa_root: Path, team_filter: str | None) -> list[TextContent]:
    people_cfg = load_yaml(edpa_root / "config" / "people.yaml") or {}
    people = people_cfg.get("people", [])

    if team_filter:
        people = [p for p in people if p.get("team") == team_filter]

    result = []
    for p in people:
        entry = {
            "id": p.get("id"),
            "name": p.get("name", p.get("id")),
            "role": p.get("role"),
            "team": p.get("team"),
            "fte": p.get("fte"),
            "capacity": p.get("capacity_per_iteration") or p.get("capacity", 0),
            "github": p.get("github") or None,
        }
        result.append(entry)

    return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]


def _handle_backlog(edpa_root: Path, iteration: str | None, type_filter: str | None, status_filter: str | None) -> list[TextContent]:
    backlog_dir = edpa_root / "backlog"
    if not backlog_dir.exists():
        return [TextContent(type="text", text="[]")]

    type_dirs = {
        "stories": "Story",
        "features": "Feature",
        "epics": "Epic",
        "initiatives": "Initiative",
    }

    items = []
    for dir_name, level in type_dirs.items():
        type_dir = backlog_dir / dir_name
        if not type_dir.exists():
            continue
        if type_filter and level != type_filter:
            continue

        for yaml_file in sorted(type_dir.glob("*.yaml")):
            data = load_yaml(yaml_file)
            if not data or not isinstance(data, dict):
                continue

            if iteration and data.get("iteration") != iteration:
                continue
            if status_filter and (data.get("status", "").lower() != status_filter.lower()):
                continue

            items.append({
                "id": data.get("id", yaml_file.stem),
                "type": data.get("type", level),
                "title": data.get("title", ""),
                "status": data.get("status", ""),
                "js": data.get("js") or data.get("job_size", 0),
                "iteration": data.get("iteration", ""),
                "assignee": data.get("assignee") or data.get("owner", ""),
                "parent": data.get("parent", ""),
            })

    return [TextContent(type="text", text=json.dumps(items, indent=2, ensure_ascii=False))]


def _handle_item(edpa_root: Path, item_id: str) -> list[TextContent]:
    backlog_dir = edpa_root / "backlog"
    if not backlog_dir.exists():
        return [TextContent(type="text", text=f"ERROR: Backlog not found.")]

    # Determine type directory from prefix
    prefix_map = {"S": "stories", "F": "features", "E": "epics", "I": "initiatives",
                  "T": "stories", "D": "defects"}
    prefix = item_id.split("-")[0] if "-" in item_id else ""
    dir_name = prefix_map.get(prefix)

    search_dirs = [backlog_dir / dir_name] if dir_name else list(backlog_dir.iterdir())

    for d in search_dirs:
        if not d.is_dir():
            continue
        candidate = d / f"{item_id}.yaml"
        if candidate.exists():
            data = load_yaml(candidate)
            if data:
                return [TextContent(type="text", text=json.dumps(data, indent=2, ensure_ascii=False, default=str))]

    return [TextContent(type="text", text=f"ERROR: Item {item_id} not found in backlog.")]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@server.list_resources()
async def list_resources() -> list[Resource]:
    edpa_root = find_edpa_root()
    resources = []
    if edpa_root:
        if (edpa_root / "config" / "edpa.yaml").exists():
            resources.append(Resource(uri="edpa://config", name="EDPA Configuration", description="Master config: PI, iterations, cadence, sync settings", mimeType="application/x-yaml"))
        if (edpa_root / "config" / "people.yaml").exists():
            resources.append(Resource(uri="edpa://people", name="EDPA Team Registry", description="Team members, roles, FTE, capacity", mimeType="application/x-yaml"))
        # Add iteration resources for each iteration
        for it_dir in sorted((edpa_root / "reports").glob("iteration-*")) if (edpa_root / "reports").exists() else []:
            results_file = it_dir / "edpa_results.json"
            if results_file.exists():
                it_id = it_dir.name.replace("iteration-", "")
                resources.append(Resource(uri=f"edpa://results/{it_id}", name=f"EDPA Results: {it_id}", description=f"Engine results for iteration {it_id}", mimeType="application/json"))
    return resources


@server.read_resource()
async def read_resource(uri: str) -> str:
    edpa_root = find_edpa_root()
    if not edpa_root:
        return "ERROR: .edpa/ directory not found."

    if uri == "edpa://config":
        path = edpa_root / "config" / "edpa.yaml"
    elif uri == "edpa://people":
        path = edpa_root / "config" / "people.yaml"
    elif uri.startswith("edpa://results/"):
        it_id = uri.replace("edpa://results/", "")
        path = edpa_root / "reports" / f"iteration-{it_id}" / "edpa_results.json"
    else:
        return f"ERROR: Unknown resource URI: {uri}"

    if not path.exists():
        return f"ERROR: File not found: {path}"

    return path.read_text()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
