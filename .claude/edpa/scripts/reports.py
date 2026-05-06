#!/usr/bin/env python3
"""
EDPA Reports — batch generator for per-person timesheets and PI summaries.

Reads engine output from .edpa/reports/iteration-<ID>/edpa_results.json
and writes:
  - timesheet-<person_id>.md per person with derived hours > 0
  - timesheet-team.md aggregated team rollup
  - (optional) pi-summary-<PI-ID>.md when --pi <PI-ID> aggregates multiple
    iterations under the same PI prefix

Designed to be invoked directly (no LLM in the loop) so the
/edpa:reports skill can shell out to it instead of having Claude
hand-render each timesheet on every iteration close. The Markdown is
also stable enough to diff-check across reruns.

Usage:
    python3 .claude/edpa/scripts/reports.py PI-2026-1.1
    python3 .claude/edpa/scripts/reports.py --pi PI-2026-1
    python3 .claude/edpa/scripts/reports.py PI-2026-1.1 --edpa-root .edpa --out .edpa/reports
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_results(results_path: Path) -> dict:
    if not results_path.is_file():
        print(
            f"ERROR: engine results not found at {results_path}. "
            f"Run engine.py --iteration <ID> first.",
            file=sys.stderr,
        )
        sys.exit(2)
    with results_path.open(encoding="utf-8") as f:
        return json.load(f)


def _format_person_md(person: dict, results: dict) -> str:
    iteration = results.get("iteration", "?")
    mode = results.get("mode", "?")
    methodology = results.get("methodology", "EDPA")
    capacity = person.get("capacity", 0)
    derived = person.get("total_derived", 0)
    items = person.get("items", []) or []
    invariant_ok = person.get("invariant_ok", True)

    lines = [
        f"# Timesheet — {person.get('name', person.get('id', '?'))} "
        f"({person.get('role', '?')})",
        "",
        f"- Iteration: **{iteration}**",
        f"- Mode: **{mode}**",
        f"- Methodology: **{methodology}**",
        f"- Capacity: **{capacity}h**",
        f"- Derived: **{derived}h**",
        f"- Invariant: **{'OK' if invariant_ok else 'FAIL'}**",
        "",
    ]
    if items:
        lines += [
            "| Item | Level | JS | CW | Score | Ratio | Hours |",
            "|------|-------|----|----|-------|-------|-------|",
        ]
        for it in items:
            lines.append(
                f"| {it.get('id','?')} | {it.get('level','?')} | "
                f"{it.get('js',0)} | {float(it.get('cw',0)):.2f} | "
                f"{float(it.get('score',0)):.2f} | "
                f"{float(it.get('ratio',0))*100:.1f}% | "
                f"{float(it.get('hours',0)):.2f} |"
            )
    else:
        lines.append("_No items credited this iteration._")
    lines.append("")
    lines.append(f"**Total: {derived}h / {capacity}h capacity**")
    return "\n".join(lines) + "\n"


def _format_team_md(results: dict) -> str:
    iteration = results.get("iteration", "?")
    mode = results.get("mode", "?")
    methodology = results.get("methodology", "EDPA")
    pf = results.get("planning_factor", 0.8)
    people = results.get("people", []) or []
    team_total = results.get("team_total", 0)
    capacity_total = sum(p.get("capacity", 0) for p in people)
    lines = [
        f"# Team Rollup — {iteration}",
        "",
        f"- Mode: **{mode}**",
        f"- Methodology: **{methodology}**",
        f"- Planning factor: **{pf}**",
        f"- Team capacity: **{capacity_total}h**",
        f"- Team derived: **{team_total}h**",
        "",
        "| Person | Role | Capacity | Derived | Items | Invariant |",
        "|--------|------|----------|---------|-------|-----------|",
    ]
    for p in people:
        lines.append(
            f"| {p.get('name', p.get('id', '?'))} | {p.get('role', '?')} | "
            f"{p.get('capacity', 0)}h | {p.get('total_derived', 0)}h | "
            f"{len(p.get('items', []) or [])} | "
            f"{'OK' if p.get('invariant_ok', True) else 'FAIL'} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_iteration_reports(edpa_root: Path, iteration_id: str,
                             out_dir: Path | None = None) -> dict:
    """Materialise per-person + team rollup MD for one iteration.

    Returns a summary dict suitable for printing and for PI aggregation.
    """
    results_path = edpa_root / "reports" / f"iteration-{iteration_id}" / "edpa_results.json"
    results = _load_results(results_path)

    if out_dir is None:
        out_dir = results_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for p in results.get("people", []) or []:
        pid = p.get("id") or p.get("name", "person").lower().replace(" ", "-")
        path = out_dir / f"timesheet-{pid}.md"
        path.write_text(_format_person_md(p, results), encoding="utf-8")
        written.append((pid, p.get("total_derived", 0), path))

    team_path = out_dir / "timesheet-team.md"
    team_path.write_text(_format_team_md(results), encoding="utf-8")

    return {
        "iteration": iteration_id,
        "people": written,
        "team": team_path,
        "results": results,
        "out_dir": out_dir,
    }


def write_pi_summary(edpa_root: Path, pi_id: str,
                     out_dir: Path | None = None) -> dict:
    """Aggregate all iteration-PI-X.Y/ results that share the PI prefix."""
    base = edpa_root / "reports"
    if not base.is_dir():
        print(f"ERROR: {base} not found", file=sys.stderr)
        sys.exit(2)

    pi_iterations = []
    for d in sorted(base.glob(f"iteration-{pi_id}.*")):
        if d.is_dir() and (d / "edpa_results.json").is_file():
            pi_iterations.append(d.name.replace("iteration-", ""))

    if not pi_iterations:
        print(
            f"ERROR: no iterations under {pi_id}.* found in {base}",
            file=sys.stderr,
        )
        sys.exit(2)

    if out_dir is None:
        out_dir = base / f"pi-{pi_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    person_totals: dict[str, dict] = {}  # pid → {name, role, capacity_sum, derived_sum, iters: [...]}
    iteration_results = []
    for iter_id in pi_iterations:
        results_path = base / f"iteration-{iter_id}" / "edpa_results.json"
        results = _load_results(results_path)
        iteration_results.append(results)
        for p in results.get("people", []) or []:
            pid = p.get("id") or p.get("name", "?")
            agg = person_totals.setdefault(pid, {
                "id": pid,
                "name": p.get("name", pid),
                "role": p.get("role", "?"),
                "capacity_sum": 0,
                "derived_sum": 0,
                "iters": [],
            })
            agg["capacity_sum"] += p.get("capacity", 0)
            agg["derived_sum"] += p.get("total_derived", 0)
            agg["iters"].append({
                "iteration": iter_id,
                "capacity": p.get("capacity", 0),
                "derived": p.get("total_derived", 0),
                "items": len(p.get("items", []) or []),
            })

    lines = [
        f"# PI Summary — {pi_id}",
        "",
        f"- Iterations: {', '.join(pi_iterations)}",
        f"- Methodology: **{iteration_results[0].get('methodology', 'EDPA')}**",
        "",
        "## Per-person totals",
        "",
        "| Person | Role | Capacity Σ | Derived Σ | Iterations |",
        "|--------|------|------------|-----------|------------|",
    ]
    for pid, agg in sorted(person_totals.items()):
        lines.append(
            f"| {agg['name']} | {agg['role']} | "
            f"{agg['capacity_sum']}h | {agg['derived_sum']}h | "
            f"{len(agg['iters'])} |"
        )

    lines += ["", "## Per-iteration breakdown", ""]
    for r in iteration_results:
        lines.append(
            f"- **{r.get('iteration')}** ({r.get('mode')}): "
            f"team_total={r.get('team_total', 0)}h, "
            f"invariants_passed={r.get('all_invariants_passed', '?')}"
        )

    summary_path = out_dir / f"pi-summary-{pi_id}.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "pi_id": pi_id,
        "iterations": pi_iterations,
        "summary": summary_path,
        "out_dir": out_dir,
    }


def main():
    parser = argparse.ArgumentParser(
        description="EDPA reports — generate per-person timesheets + PI summaries"
    )
    parser.add_argument(
        "iteration",
        nargs="?",
        help="Iteration ID (e.g. PI-2026-1.1). Required unless --pi is given.",
    )
    parser.add_argument(
        "--pi",
        help="PI ID (e.g. PI-2026-1). Aggregates all iterations under this PI.",
    )
    parser.add_argument(
        "--edpa-root",
        default=".edpa",
        help="Path to .edpa/ directory (default: .edpa)",
    )
    parser.add_argument(
        "--out",
        help="Override output directory (default: <edpa-root>/reports/iteration-<ID>/)",
    )
    args = parser.parse_args()

    edpa_root = Path(args.edpa_root)
    out_dir = Path(args.out) if args.out else None

    if args.pi:
        info = write_pi_summary(edpa_root, args.pi, out_dir=out_dir)
        print(
            f"✓ PI summary {args.pi} → {info['summary']} "
            f"({len(info['iterations'])} iteration(s) aggregated)"
        )
        return

    if not args.iteration:
        parser.error("either an iteration ID or --pi <PI-ID> is required")

    info = write_iteration_reports(edpa_root, args.iteration, out_dir=out_dir)
    iteration = info["iteration"]
    people = info["people"]
    print(f"✓ Reports for {iteration} → {info['out_dir']}")
    for pid, derived, path in people:
        print(f"  - {path.name:<32} {derived:6.1f}h")
    print(f"  - {info['team'].name:<32} (team rollup)")


if __name__ == "__main__":
    main()
