#!/usr/bin/env python3
"""
EDPA PI Close — aggregate iteration results into PI-level summary.

Reads:
  - .edpa/iterations/PI-YYYY-M.{1,2,3}.yaml (closed iteration plans)
  - .edpa/reports/iteration-PI-YYYY-M.{1,2,3}/edpa_results.json (optional)
  - .edpa/backlog/features/*.yaml (to identify Features completed in PI)

Writes:
  - .edpa/reports/pi-PI-YYYY-M/pi_results.json
  - .edpa/reports/pi-PI-YYYY-M/summary.md

Usage:
    python3 .claude/edpa/scripts/pi_close.py --pi PI-2026-1
    python3 .claude/edpa/scripts/pi_close.py --pi PI-2026-1 --edpa-root .edpa
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def load_yaml(path: Path):
    """Returns parsed dict, empty dict for empty file, None if missing/unparseable."""
    if not path.is_file():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        print(f"WARNING: load_yaml({path}) failed: {exc}", file=sys.stderr)
        return None


def load_json(path: Path):
    """Returns parsed content, None if missing/unparseable."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: load_json({path}) failed: {exc}", file=sys.stderr)
        return None


def find_iterations(edpa_root: Path, pi_id: str):
    """Return sorted list of iteration YAMLs for given PI."""
    iter_dir = edpa_root / "iterations"
    if not iter_dir.is_dir():
        return []
    return sorted(iter_dir.glob(f"{pi_id}.*.yaml"))


def aggregate_iterations(iteration_files):
    """Aggregate planning + delivery metrics across iterations."""
    iterations = []
    total_planned = 0
    total_delivered = 0
    total_capacity = 0
    spillover_ids = []
    unplanned_ids = []

    for f in iteration_files:
        data = load_yaml(f)
        if not data:
            continue
        it = data.get("iteration", {})
        planning = data.get("planning", {})
        delivery = data.get("delivery", {})

        planned = planning.get("planned_sp", 0) or 0
        delivered = delivery.get("delivered_sp", 0) or 0
        capacity = planning.get("capacity", 0) or 0
        predictability = (
            round(100 * delivered / planned, 1) if planned else None
        )

        total_planned += planned
        total_delivered += delivered
        total_capacity += capacity
        spillover_ids.extend(delivery.get("spillover", []) or [])
        unplanned_ids.extend(delivery.get("unplanned", []) or [])

        iterations.append({
            "id": it.get("id"),
            "status": it.get("status"),
            # PyYAML parses ISO dates into date objects; coerce to string
            # so the report serializes cleanly to JSON.
            "start_date": str(it["start_date"]) if it.get("start_date") else None,
            "end_date": str(it["end_date"]) if it.get("end_date") else None,
            "planned_sp": planned,
            "delivered_sp": delivered,
            "velocity": delivery.get("velocity", delivered),
            "predictability_pct": predictability,
            "spillover_count": len(delivery.get("spillover", []) or []),
            "unplanned_count": len(delivery.get("unplanned", []) or []),
        })

    avg_predictability = (
        round(100 * total_delivered / total_planned, 1) if total_planned else None
    )

    return {
        "iterations": iterations,
        "total_planned_sp": total_planned,
        "total_delivered_sp": total_delivered,
        "total_capacity_hours": total_capacity,
        "avg_predictability_pct": avg_predictability,
        "spillover_ids": spillover_ids,
        "unplanned_ids": unplanned_ids,
    }


def aggregate_engine_results(edpa_root: Path, pi_id: str, iteration_ids):
    """Sum per-person DerivedHours across iterations if engine results exist."""
    per_person = defaultdict(lambda: {"hours": 0.0, "iterations": []})
    any_results = False
    for it_id in iteration_ids:
        if not it_id:
            continue
        results_path = (
            edpa_root / "reports" / f"iteration-{it_id}" / "edpa_results.json"
        )
        data = load_json(results_path)
        if not data:
            continue
        any_results = True
        for entry in data.get("allocations", []) or []:
            person = entry.get("person")
            hours = entry.get("derived_hours", 0) or 0
            if person:
                per_person[person]["hours"] += hours
                per_person[person]["iterations"].append(it_id)
    if not any_results:
        return None
    return [
        {"person": p, "derived_hours": round(v["hours"], 2),
         "iterations": v["iterations"]}
        for p, v in sorted(per_person.items())
    ]


def features_completed(edpa_root: Path, pi_id: str):
    """Features with iteration in this PI and status=Done."""
    feat_dir = edpa_root / "backlog" / "features"
    if not feat_dir.is_dir():
        return []
    done = []
    for f in sorted(feat_dir.glob("*.yaml")):
        data = load_yaml(f)
        if not data:
            continue
        it = data.get("iteration", "")
        if not it.startswith(pi_id):
            continue
        if data.get("status") == "Done":
            done.append({
                "id": data.get("id", f.stem),
                "title": data.get("title", ""),
                "wsjf": data.get("wsjf"),
                "js": data.get("js"),
            })
    return done


def build_pi_results(edpa_root: Path, pi_id: str):
    iteration_files = find_iterations(edpa_root, pi_id)
    if not iteration_files:
        return None, f"No iterations found for {pi_id} in {edpa_root}/iterations/"

    agg = aggregate_iterations(iteration_files)
    iteration_ids = [it["id"] for it in agg["iterations"]]
    engine = aggregate_engine_results(edpa_root, pi_id, iteration_ids)
    done_features = features_completed(edpa_root, pi_id)

    return {
        "pi": pi_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "iterations": agg["iterations"],
        "summary": {
            "iteration_count": len(agg["iterations"]),
            "total_planned_sp": agg["total_planned_sp"],
            "total_delivered_sp": agg["total_delivered_sp"],
            "total_capacity_hours": agg["total_capacity_hours"],
            "avg_predictability_pct": agg["avg_predictability_pct"],
            "total_spillover": len(agg["spillover_ids"]),
            "total_unplanned": len(agg["unplanned_ids"]),
        },
        "spillover_ids": agg["spillover_ids"],
        "unplanned_ids": agg["unplanned_ids"],
        "features_completed": done_features,
        "per_person_hours": engine,
    }, None


def render_summary_md(result: dict) -> str:
    pi = result["pi"]
    s = result["summary"]
    lines = [
        f"# PI Summary — {pi}",
        "",
        f"_Generated: {result['generated_at']}_",
        "",
        "## Delivery",
        "",
        f"- Iterations closed: **{s['iteration_count']}**",
        f"- Planned SP: **{s['total_planned_sp']}**",
        f"- Delivered SP: **{s['total_delivered_sp']}**",
        f"- Average predictability: **{s['avg_predictability_pct']}%**",
        f"- Capacity hours: **{s['total_capacity_hours']}**",
        f"- Spillover: **{s['total_spillover']}**, Unplanned: **{s['total_unplanned']}**",
        "",
        "## Iterations",
        "",
        "| ID | Dates | Planned | Delivered | Predictability | Velocity |",
        "|---|---|---:|---:|---:|---:|",
    ]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _pi_loader import format_iteration_dates  # noqa: E402

    for it in result["iterations"]:
        lines.append(
            f"| {it['id']} | {format_iteration_dates(it)} | {it['planned_sp']} | "
            f"{it['delivered_sp']} | {it['predictability_pct']}% | {it['velocity']} |"
        )

    if result["features_completed"]:
        lines += ["", "## Features Completed", ""]
        for f in result["features_completed"]:
            lines.append(
                f"- **{f['id']}** — {f['title']} (JS={f.get('js')}, WSJF={f.get('wsjf')})"
            )

    if result["per_person_hours"]:
        lines += ["", "## Derived Hours by Person", "", "| Person | Hours |", "|---|---:|"]
        for p in result["per_person_hours"]:
            lines.append(f"| {p['person']} | {p['derived_hours']} |")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="EDPA PI Close — aggregate PI metrics")
    parser.add_argument("--pi", required=True, help="PI ID (e.g., PI-2026-1)")
    parser.add_argument("--edpa-root", default=".edpa", type=Path)
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Output directory (default: <edpa-root>/reports/pi-<PI>)")
    args = parser.parse_args()

    if not args.edpa_root.is_dir():
        print(f"ERROR: {args.edpa_root} not found", file=sys.stderr)
        return 2

    result, err = build_pi_results(args.edpa_root, args.pi)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    out_dir = args.output_dir or (args.edpa_root / "reports" / f"pi-{args.pi}")
    out_dir.mkdir(parents=True, exist_ok=True)

    results_path = out_dir / "pi_results.json"
    summary_path = out_dir / "summary.md"
    results_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_path.write_text(render_summary_md(result), encoding="utf-8")

    print(f"PI {args.pi}: {result['summary']['iteration_count']} iterations, "
          f"{result['summary']['total_delivered_sp']}/{result['summary']['total_planned_sp']} SP, "
          f"{result['summary']['avg_predictability_pct']}% predictability")
    print(f"  -> {results_path}")
    print(f"  -> {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
