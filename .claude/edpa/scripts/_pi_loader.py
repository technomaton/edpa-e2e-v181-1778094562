"""PI/iteration loader. Reconstructs the PI list at runtime from
``iterations/*.yaml`` files instead of reading a redundant ``pis[]``
block in ``edpa.yaml``.

Filename convention:

  iterations/PI-{year}-{n}.yaml      → PI-level metadata (one per PI)
  iterations/PI-{year}-{n}.{m}.yaml  → per-iteration data

Both shapes use ISO ``start_date`` / ``end_date`` fields. The legacy
Czech ``dates: "D.M.-D.M.YYYY"`` string is no longer read here.

The loader returns ``(pis, diagnostics)``. Diagnostics carry
``severity: "error" | "warning"`` so callers can decide whether to
block (e.g. CLI exit code) or merely surface (e.g. MCP response).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

PI_FILE_RE = re.compile(r"^PI-\d{4}-\d+$")
ITERATION_FILE_RE = re.compile(r"^PI-\d{4}-\d+\.\d+$")

LoaderFn = Callable[[Path], "dict | None"]


def _default_loader(path: Path) -> "dict | None":
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("_pi_loader: failed to read %s: %s", path, exc)
        return None


def _to_date(v) -> "date | None":
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _derived_weeks(start: date, end: date) -> int:
    """Round inclusive day count to nearest week. Mon-Fri = 5d → 1w,
    Mon-Fri + Mon-Fri = 12d → 2w. Bounded ≥1."""
    days = (end - start).days + 1
    return max(1, round(days / 7))


def _diag(severity: str, code: str, message: str, **extra) -> dict:
    return {"severity": severity, "code": code, "message": message, **extra}


def _is_weekend_bridge(start: date, gap_days: int) -> bool:
    """Iteration boundaries on Fri→Mon shouldn't fire a gap warning."""
    bridge = [start - timedelta(days=i + 1) for i in range(gap_days)]
    return all(d.weekday() >= 5 for d in bridge)


def derive_pis(edpa_root: Path, *, loader: LoaderFn | None = None) -> tuple[list[dict], list[dict]]:
    """Build pis[] from iterations/*.yaml. Returns ``(pis, diagnostics)``."""
    iter_dir = edpa_root / "iterations"
    diags: list[dict] = []
    if not iter_dir.is_dir():
        return [], diags

    load = loader or _default_loader

    pi_meta: dict[str, dict] = {}
    iters_by_pi: dict[str, list[dict]] = {}

    for f in sorted(iter_dir.glob("*.yaml")):
        stem = f.stem
        doc = load(f) or {}
        if PI_FILE_RE.fullmatch(stem):
            pi = doc.get("pi") or {}
            pid = pi.get("id")
            if not pid:
                diags.append(_diag("error", "missing_id",
                                   f"{f.name}: pi.id missing", file=f.name))
                continue
            pi_meta[pid] = pi
        elif ITERATION_FILE_RE.fullmatch(stem):
            it = doc.get("iteration") or {}
            it_id = it.get("id")
            if not it_id:
                diags.append(_diag("error", "missing_id",
                                   f"{f.name}: iteration.id missing", file=f.name))
                continue
            pi_id = it.get("pi") or it_id.rsplit(".", 1)[0]
            iters_by_pi.setdefault(pi_id, []).append({**it, "_file": f.name})
        # Other filenames (CHANGELOG, drafts) are silently ignored.

    pis: list[dict] = []
    for pi_id in sorted(set(pi_meta) | set(iters_by_pi)):
        pis.append(_build_pi(pi_id, pi_meta.get(pi_id, {}),
                             iters_by_pi.get(pi_id, []), diags))
    return pis, diags


def _build_pi(pi_id: str, meta: dict, iters_raw: list[dict], diags: list[dict]) -> dict:
    if not meta and iters_raw:
        diags.append(_diag("warning", "missing_pi_yaml",
                           f"iterations/{pi_id}.yaml missing — PI metadata derived from iterations",
                           pi=pi_id))

    iterations_out: list[dict] = []
    prev_end: date | None = None

    for it_raw in sorted(iters_raw, key=lambda x: x["id"]):
        it_id = it_raw["id"]
        start = _to_date(it_raw.get("start_date"))
        end = _to_date(it_raw.get("end_date"))

        if start is None or end is None:
            diags.append(_diag("error", "missing_dates",
                               f"{it_raw.get('_file', it_id)}: start_date/end_date missing or invalid",
                               pi=pi_id, iteration=it_id))
            continue
        if end < start:
            diags.append(_diag("error", "inverted_dates",
                               f"{it_id}: end_date {end} before start_date {start}",
                               pi=pi_id, iteration=it_id))

        derived = _derived_weeks(start, end)
        declared = it_raw.get("weeks")
        if declared is not None and declared != derived:
            diags.append(_diag("error", "weeks_mismatch",
                               f"{it_id}: declared weeks={declared} but dates imply {derived}",
                               pi=pi_id, iteration=it_id))

        if prev_end is not None:
            gap = (start - prev_end).days - 1
            if gap > 0 and not _is_weekend_bridge(start, gap):
                diags.append(_diag("warning", "date_gap",
                                   f"{it_id} starts {start}, previous iteration ended {prev_end} — {gap}-day gap",
                                   pi=pi_id, iteration=it_id))
            elif gap < 0:
                diags.append(_diag("error", "date_overlap",
                                   f"{it_id} starts {start}, previous iteration ended {prev_end} — overlap",
                                   pi=pi_id, iteration=it_id))
        prev_end = end

        entry = {
            "id": it_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "weeks": declared if declared is not None else derived,
            "status": it_raw.get("status", "planned"),
        }
        if it_raw.get("type"):
            entry["type"] = it_raw["type"]
        iterations_out.append(entry)

    pi_status = meta.get("status") or _pi_status_from_iterations(iterations_out)
    pi_iter_weeks = meta.get("iteration_weeks") or (
        Counter(it["weeks"] for it in iterations_out).most_common(1)[0][0]
        if iterations_out else 1)
    pi_count = meta.get("pi_iterations") or len(iterations_out) or 0

    pi_start = _to_date(meta.get("start_date")) or (
        _to_date(iterations_out[0]["start_date"]) if iterations_out else None)
    pi_end = _to_date(meta.get("end_date")) or (
        _to_date(iterations_out[-1]["end_date"]) if iterations_out else None)

    if meta and iterations_out:
        decl_start = _to_date(meta.get("start_date"))
        decl_end = _to_date(meta.get("end_date"))
        actual_start = _to_date(iterations_out[0]["start_date"])
        actual_end = _to_date(iterations_out[-1]["end_date"])
        if decl_start and actual_start and decl_start != actual_start:
            diags.append(_diag("warning", "pi_start_mismatch",
                               f"{pi_id}: PI start_date={decl_start} but first iteration starts {actual_start}",
                               pi=pi_id))
        if decl_end and actual_end and decl_end != actual_end:
            diags.append(_diag("warning", "pi_end_mismatch",
                               f"{pi_id}: PI end_date={decl_end} but last iteration ends {actual_end}",
                               pi=pi_id))

    pi_obj: dict = {
        "id": pi_id,
        "status": pi_status,
        "iteration_weeks": pi_iter_weeks,
        "pi_iterations": pi_count,
        "iterations": iterations_out,
    }
    if pi_start: pi_obj["start_date"] = pi_start.isoformat()
    if pi_end: pi_obj["end_date"] = pi_end.isoformat()
    return pi_obj


def _pi_status_from_iterations(iterations: list[dict]) -> str:
    statuses = {it.get("status") for it in iterations}
    if "active" in statuses:
        return "active"
    if statuses and statuses.issubset({"closed"}):
        return "closed"
    return "planning"


def find_active_pi(pis: list[dict]) -> dict:
    """Return the active PI, falling back to the first one (or {})."""
    return next((p for p in pis if p.get("status") == "active"),
                pis[0] if pis else {})


def split_diagnostics(diags: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into (errors, warnings) by severity."""
    errors = [d for d in diags if d.get("severity") == "error"]
    warnings = [d for d in diags if d.get("severity") == "warning"]
    return errors, warnings


def format_iteration_dates(it: dict) -> str:
    """Format iteration dates for human-readable display."""
    sd = it.get("start_date")
    ed = it.get("end_date")
    if sd and ed:
        return f"{sd}–{ed}"
    return ""
