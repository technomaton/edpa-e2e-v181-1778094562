#!/usr/bin/env python3
"""
EDPA Board — Generate a self-contained HTML Kanban snapshot from .edpa/backlog/.

Usage:
    python plugin/edpa/scripts/board.py                          # Default: all stories
    python plugin/edpa/scripts/board.py --open                   # Generate & open in browser
    python plugin/edpa/scripts/board.py --iteration PI-2026-1.4  # Filter by iteration
    python plugin/edpa/scripts/board.py --level feature          # Show features instead
    python plugin/edpa/scripts/board.py --output /tmp/board.html # Custom output path
"""

import argparse
import html
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml")
    sys.exit(1)


# -- Repo discovery ------------------------------------------------------------

def find_repo_root():
    p = Path.cwd()
    while p != p.parent:
        if (p / ".edpa" / "config" / "people.yaml").exists():
            return p
        p = p.parent
    return None


# -- Data loading ---------------------------------------------------------------

def load_people(root):
    path = root / ".edpa" / "config" / "people.yaml"
    if not path.exists():
        return {}, ""
    data = yaml.safe_load(open(path, encoding="utf-8"))
    people = {p["id"]: p for p in data.get("people", [])}
    project_name = data.get("project", {}).get("name", "EDPA")
    return people, project_name


def load_items(root):
    items = []
    for type_dir in ["initiatives", "epics", "features", "stories", "defects"]:
        dir_path = root / ".edpa" / "backlog" / type_dir
        if dir_path.exists():
            for f in sorted(dir_path.glob("*.yaml")):
                item = yaml.safe_load(open(f, encoding="utf-8"))
                if item:
                    items.append(item)
    return items


# -- Status mapping -------------------------------------------------------------

STATUS_COLUMNS = [
    ("Planned", ["Planned"]),
    ("In Progress", ["In Progress", "Active"]),
    ("Done", ["Done"]),
]


def status_column(status):
    for col_name, statuses in STATUS_COLUMNS:
        if status in statuses:
            return col_name
    return "Planned"


# -- Type colors ----------------------------------------------------------------

TYPE_COLORS = {
    "Initiative": {"bg": "var(--type-initiative-bg)", "fg": "var(--type-initiative)"},
    "Epic":       {"bg": "var(--type-epic-bg)",       "fg": "var(--type-epic)"},
    "Feature":    {"bg": "var(--type-feature-bg)",     "fg": "var(--type-feature)"},
    "Story":      {"bg": "var(--type-story-bg)",       "fg": "var(--type-story)"},
    "Defect":     {"bg": "var(--type-defect-bg)",      "fg": "var(--type-defect)"},
}


# -- HTML generation ------------------------------------------------------------

def esc(text):
    return html.escape(str(text)) if text else ""


def person_initials(name):
    parts = name.strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper() if name else "?"


def avatar_html(person, fallback_id, *, size=40, klass="card__avatar"):
    """Render a GitHub avatar <img> when the person has a github login on
    file; fall back to colored-initials <div> otherwise. Title text uses
    the display name (or the bare id when name is missing)."""
    name = person.get("name") or fallback_id or "?"
    title = esc(name)
    gh = person.get("github") if person else None
    if gh:
        src = f"https://github.com/{esc(gh)}.png?size={size}"
        return (f'<img class="{klass} {klass}--gh" src="{src}" '
                f'alt="@{esc(gh)}" title="{title} (@{esc(gh)})" '
                f'width="{size}" height="{size}">')
    init = person_initials(name)
    return f'<div class="{klass}" title="{title}">{init}</div>'


def render_card(item, people, items_by_id):
    item_type = item.get("type", "Story")
    tc = TYPE_COLORS.get(item_type, TYPE_COLORS["Story"])
    item_id = esc(item.get("id", ""))
    title = esc(item.get("title", ""))
    status = item.get("status", "Planned")
    assignee_id = item.get("assignee") or item.get("owner", "")
    assignee = people.get(assignee_id, {})
    avatar = avatar_html(assignee, assignee_id, size=40, klass="card__avatar")
    iteration = esc(item.get("iteration", ""))

    # Parent breadcrumb
    breadcrumb = ""
    parent_id = item.get("parent")
    if parent_id and parent_id in items_by_id:
        parent = items_by_id[parent_id]
        breadcrumb = f'<span class="card__parent">{esc(parent.get("id", ""))} {esc(parent.get("title", ""))}</span>'

    # WSJF dots
    wsjf_html = ""
    bv = item.get("bv")
    tc_val = item.get("tc")
    rr = item.get("rr")
    if any(v is not None for v in [bv, tc_val, rr]):
        dots = []
        if bv is not None:
            dots.append(f'<span class="wsjf-dot wsjf-bv" title="BV: {bv}">{bv}</span>')
        if tc_val is not None:
            dots.append(f'<span class="wsjf-dot wsjf-tc" title="TC: {tc_val}">{tc_val}</span>')
        if rr is not None:
            dots.append(f'<span class="wsjf-dot wsjf-rr" title="RR: {rr}">{rr}</span>')
        wsjf_html = f'<div class="card__wsjf">{"".join(dots)}</div>'

    # JS badge
    js = item.get("js")
    js_html = f'<span class="card__js" title="Job Size">{js}</span>' if js else ""

    iter_html = f'<span class="card__iter">{iteration}</span>' if iteration else ""

    done_class = " card--done" if status == "Done" else ""

    return f"""<article class="card{done_class}"
        data-assignee="{esc(assignee_id)}"
        data-iteration="{esc(item.get('iteration', ''))}"
        data-type="{esc(item_type)}"
        data-id="{item_id}">
  <div class="card__head">
    <span class="card__id" style="background:{tc['bg']};color:{tc['fg']}">{item_id}</span>
    {js_html}
  </div>
  <h3 class="card__title">{title}</h3>
  {breadcrumb}
  {wsjf_html}
  <div class="card__foot">
    {avatar}
    {iter_html}
  </div>
</article>"""


def render_html(items, people, project_name, level_filter=None, iteration_filter=None):
    # Filter items
    filtered = items
    if level_filter:
        level_map = {"initiative": "Initiative", "epic": "Epic", "feature": "Feature", "story": "Story", "defect": "Defect"}
        target = level_map.get(level_filter.lower(), level_filter)
        filtered = [i for i in filtered if i.get("type") == target]
    if iteration_filter:
        filtered = [i for i in filtered if (i.get("iteration") or "").startswith(iteration_filter)]

    items_by_id = {i["id"]: i for i in items if "id" in i}

    # Collect unique iterations & assignees for filters
    iterations = sorted(set(i.get("iteration", "") for i in filtered if i.get("iteration")))
    assignees = sorted(set(i.get("assignee") or i.get("owner", "") for i in filtered if i.get("assignee") or i.get("owner")))
    types = sorted(set(i.get("type", "Story") for i in filtered))

    # Group by column
    columns = {col: [] for col, _ in STATUS_COLUMNS}
    for item in filtered:
        col = status_column(item.get("status", "Planned"))
        columns[col].append(item)

    # Sort within columns by WSJF descending
    for col in columns:
        columns[col].sort(key=lambda i: i.get("wsjf") or 0, reverse=True)

    # Render columns
    cols_html = ""
    for col_name, _ in STATUS_COLUMNS:
        col_items = columns[col_name]
        cards = "\n".join(render_card(i, people, items_by_id) for i in col_items)
        dot_class = {"Planned": "dot--planned", "In Progress": "dot--progress", "Done": "dot--done"}.get(col_name, "")
        cols_html += f"""<div class="column">
  <div class="column__head">
    <span class="column__dot {dot_class}"></span>
    <span class="column__title">{col_name}</span>
    <span class="column__count">{len(col_items)}</span>
  </div>
  <div class="column__list">{cards}</div>
</div>\n"""

    # Assignee chips
    assignee_chips = ""
    for a_id in assignees:
        p = people.get(a_id, {})
        chip_inner = avatar_html(p, a_id, size=24, klass="chip__avatar")
        assignee_chips += (
            f'<button class="chip chip--assignee" '
            f'data-filter-assignee="{esc(a_id)}">{chip_inner}</button>\n'
        )

    # Iteration options
    iter_options = '<option value="">All iterations</option>\n'
    for it in iterations:
        sel = ' selected' if iteration_filter and it.startswith(iteration_filter) else ''
        iter_options += f'<option value="{esc(it)}"{sel}>{esc(it)}</option>\n'

    # Type options
    type_options = '<option value="">All types</option>\n'
    for t in types:
        type_options += f'<option value="{esc(t)}">{esc(t)}</option>\n'

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(filtered)

    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EDPA Board — {esc(project_name)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
/* -- Light theme (default) ------------------------------------------ */
:root{{
  --bg:#f4f5f7;--s1:#ffffff;--s2:#f8f9fb;--s3:#eef0f4;
  --bd:#d8dce6;--t1:#1a1d26;--t2:#4a5068;--t3:#8892a8;
  --ac:#6366f1;--ac2:#4f46e5;--ac3:#818cf8;
  --cy:#0891b2;--gn:#059669;--rd:#dc2626;--yl:#d97706;
  --or:#ea580c;--pk:#db2777;
  --shadow:0 1px 3px rgba(0,0,0,0.08);
  --shadow-hover:0 4px 12px rgba(0,0,0,0.12);
  --r:12px;
  /* type accent colors */
  --type-initiative:#db2777;--type-initiative-bg:rgba(219,39,119,0.08);
  --type-epic:#6366f1;--type-epic-bg:rgba(99,102,241,0.08);
  --type-feature:#0891b2;--type-feature-bg:rgba(8,145,178,0.08);
  --type-story:#ea580c;--type-story-bg:rgba(234,88,12,0.08);
  --type-defect:#dc2626;--type-defect-bg:rgba(220,38,38,0.08);
}}
/* -- Dark theme ----------------------------------------------------- */
.dark{{
  --bg:#080a10;--s1:#111520;--s2:#181d2e;--s3:#222840;
  --bd:#2a3050;--t1:#e8ecf4;--t2:#8892b0;--t3:#5a6380;
  --ac:#6366f1;--ac2:#818cf8;--ac3:#a5b4fc;
  --cy:#22d3ee;--gn:#34d399;--rd:#f87171;--yl:#fbbf24;
  --or:#f97316;--pk:#f472b6;
  --shadow:0 1px 3px rgba(0,0,0,0.3);
  --shadow-hover:0 4px 12px rgba(0,0,0,0.5);
  --type-initiative:#f472b6;--type-initiative-bg:rgba(244,114,182,0.12);
  --type-epic:#818cf8;--type-epic-bg:rgba(129,140,248,0.12);
  --type-feature:#22d3ee;--type-feature-bg:rgba(34,211,238,0.12);
  --type-story:#f97316;--type-story-bg:rgba(249,115,22,0.12);
  --type-defect:#f87171;--type-defect-bg:rgba(248,113,113,0.12);
}}
body{{
  font-family:'DM Sans',system-ui,sans-serif;
  background:var(--bg);color:var(--t1);
  min-height:100vh;padding:24px;
  transition:background 0.2s,color 0.2s;
}}
/* -- Header --------------------------------------------------------- */
.header{{
  display:flex;flex-wrap:wrap;align-items:center;gap:16px;
  margin-bottom:24px;padding-bottom:16px;
  border-bottom:1px solid var(--bd);
}}
.header__title{{
  font-family:'JetBrains Mono',monospace;font-size:1.1rem;
  font-weight:700;color:var(--ac2);flex-shrink:0;
}}
.header__meta{{font-size:0.8rem;color:var(--t2);}}
.filters{{
  display:flex;flex-wrap:wrap;align-items:center;gap:8px;
  margin-left:auto;
}}
.filters select,.filters input{{
  font-family:'JetBrains Mono',monospace;font-size:0.75rem;
  background:var(--s1);color:var(--t1);border:1px solid var(--bd);
  border-radius:6px;padding:6px 10px;outline:none;
  box-shadow:var(--shadow);
}}
.filters select:focus,.filters input:focus{{border-color:var(--ac);}}
.filters input{{width:160px;}}
/* -- Theme toggle --------------------------------------------------- */
.theme-toggle{{
  width:32px;height:32px;border-radius:8px;border:1px solid var(--bd);
  background:var(--s1);color:var(--t2);cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  font-size:1rem;transition:all 0.15s;box-shadow:var(--shadow);
}}
.theme-toggle:hover{{border-color:var(--ac);color:var(--ac);}}
.chip{{
  display:inline-flex;align-items:center;justify-content:center;
  width:28px;height:28px;border-radius:50%;
  font-family:'JetBrains Mono',monospace;font-size:0.6rem;font-weight:700;
  background:var(--s3);color:var(--t2);border:1px solid var(--bd);
  cursor:pointer;transition:all 0.15s;
}}
.chip:hover,.chip.active{{background:var(--ac);color:#fff;border-color:var(--ac);}}
/* -- Columns -------------------------------------------------------- */
.columns{{
  display:grid;grid-template-columns:repeat(3,1fr);
  gap:16px;
}}
.column{{
  background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);
  padding:12px;display:flex;flex-direction:column;min-height:400px;
  box-shadow:var(--shadow);
}}
.column__head{{
  display:flex;align-items:center;gap:8px;
  padding-bottom:12px;margin-bottom:8px;
  border-bottom:1px solid var(--bd);
}}
.column__dot{{
  width:8px;height:8px;border-radius:50%;
}}
.dot--planned{{background:var(--t3);}}
.dot--progress{{background:var(--ac);animation:pulse 2s ease-in-out infinite;}}
.dot--done{{background:var(--gn);}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.4}}}}
.column__title{{
  font-family:'JetBrains Mono',monospace;font-size:0.8rem;
  font-weight:700;text-transform:uppercase;letter-spacing:0.05em;
  color:var(--t1);
}}
.column__count{{
  font-family:'JetBrains Mono',monospace;font-size:0.7rem;
  background:var(--s3);color:var(--t2);padding:2px 8px;
  border-radius:10px;margin-left:auto;
}}
.column__list{{
  flex:1;display:flex;flex-direction:column;gap:8px;
  overflow-y:auto;padding:2px;
}}
/* -- Cards ---------------------------------------------------------- */
.card{{
  background:var(--s2);border:1px solid var(--bd);border-radius:10px;
  padding:12px;transition:all 0.15s;
  border-left:3px solid var(--bd);
  box-shadow:var(--shadow);
}}
.card[data-type="Initiative"]{{border-left-color:var(--type-initiative);background:var(--type-initiative-bg);}}
.card[data-type="Epic"]{{border-left-color:var(--type-epic);background:var(--type-epic-bg);}}
.card[data-type="Feature"]{{border-left-color:var(--type-feature);background:var(--type-feature-bg);}}
.card[data-type="Story"]{{border-left-color:var(--type-story);background:var(--type-story-bg);}}
.card[data-type="Defect"]{{border-left-color:var(--type-defect);background:var(--type-defect-bg);}}
.card:hover{{
  border-color:var(--ac);
  box-shadow:var(--shadow-hover);
  transform:translateY(-1px);
}}
.card:hover{{border-left-width:3px;}}
.card[data-type="Initiative"]:hover{{border-left-color:var(--type-initiative);}}
.card[data-type="Epic"]:hover{{border-left-color:var(--type-epic);}}
.card[data-type="Feature"]:hover{{border-left-color:var(--type-feature);}}
.card[data-type="Story"]:hover{{border-left-color:var(--type-story);}}
.card[data-type="Defect"]:hover{{border-left-color:var(--type-defect);}}
.card--done{{opacity:0.55;}}
.card--done:hover{{opacity:0.85;}}
.card.hidden{{display:none;}}
.card__head{{display:flex;align-items:center;gap:6px;margin-bottom:6px;}}
.card__id{{
  font-family:'JetBrains Mono',monospace;font-size:0.65rem;font-weight:700;
  padding:2px 8px;border-radius:4px;
}}
.card__js{{
  font-family:'JetBrains Mono',monospace;font-size:0.65rem;font-weight:600;
  color:var(--yl);margin-left:auto;
}}
.card__js::before{{content:'JS ';color:var(--t3);}}
.card__title{{
  font-size:0.85rem;font-weight:600;line-height:1.35;
  margin-bottom:4px;color:var(--t1);
}}
.card__parent{{
  font-size:0.7rem;color:var(--t3);display:block;
  margin-bottom:6px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;
}}
.card__wsjf{{display:flex;gap:6px;margin-bottom:8px;}}
.wsjf-dot{{
  font-family:'JetBrains Mono',monospace;font-size:0.6rem;font-weight:700;
  padding:1px 6px;border-radius:3px;
}}
.wsjf-bv{{background:rgba(5,150,105,0.1);color:var(--gn);}}
.wsjf-tc{{background:rgba(234,88,12,0.1);color:var(--or);}}
.wsjf-rr{{background:rgba(8,145,178,0.1);color:var(--cy);}}
.card__foot{{
  display:flex;align-items:center;justify-content:space-between;
  margin-top:auto;
}}
.card__avatar{{
  width:24px;height:24px;border-radius:50%;
  background:var(--s3);color:var(--t2);
  display:flex;align-items:center;justify-content:center;
  font-family:'JetBrains Mono',monospace;font-size:0.55rem;font-weight:700;
}}
.card__iter{{
  font-family:'JetBrains Mono',monospace;font-size:0.6rem;
  color:var(--t2);
}}
/* -- Footer --------------------------------------------------------- */
.footer{{
  margin-top:24px;padding-top:12px;border-top:1px solid var(--bd);
  display:flex;gap:16px;align-items:center;
  font-family:'JetBrains Mono',monospace;font-size:0.7rem;color:var(--t3);
}}
/* -- Responsive ----------------------------------------------------- */
@media(max-width:900px){{
  .columns{{grid-template-columns:repeat(2,1fr);}}
}}
@media(max-width:520px){{
  .columns{{grid-template-columns:1fr;}}
  .column{{min-height:200px;}}
}}
</style>
</head>
<body>

<div class="header">
  <span class="header__title">EDPA Board</span>
  <span class="header__meta">{esc(project_name)} &middot; {now}</span>
  <div class="filters">
    <select id="fIteration" onchange="applyFilters()">{iter_options}</select>
    <select id="fType" onchange="applyFilters()">{type_options}</select>
    <input id="fSearch" type="text" placeholder="Search..." oninput="applyFilters()">
    {assignee_chips}
    <button class="theme-toggle" id="themeToggle" onclick="toggleTheme()" title="Toggle dark/light">&#9790;</button>
  </div>
</div>

<div class="columns">{cols_html}</div>

<div class="footer">
  <span>{total} items</span>
  <span>&middot;</span>
  <span>Generated {now} by EDPA</span>
</div>

<script>
// Theme toggle
function toggleTheme() {{
  document.body.classList.toggle('dark');
  const btn = document.getElementById('themeToggle');
  btn.innerHTML = document.body.classList.contains('dark') ? '&#9788;' : '&#9790;';
  localStorage.setItem('edpa-board-theme', document.body.classList.contains('dark') ? 'dark' : 'light');
}}
// Restore saved preference
if (localStorage.getItem('edpa-board-theme') === 'dark') {{
  document.body.classList.add('dark');
  document.getElementById('themeToggle').innerHTML = '&#9788;';
}}

const chips = document.querySelectorAll('.chip--assignee');
let activeAssignee = '';

chips.forEach(c => c.addEventListener('click', () => {{
  if (c.classList.contains('active')) {{
    c.classList.remove('active');
    activeAssignee = '';
  }} else {{
    chips.forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    activeAssignee = c.dataset.filterAssignee;
  }}
  applyFilters();
}}));

function applyFilters() {{
  const iteration = document.getElementById('fIteration').value;
  const type = document.getElementById('fType').value;
  const search = document.getElementById('fSearch').value.toLowerCase();

  document.querySelectorAll('.card').forEach(card => {{
    let show = true;
    if (activeAssignee && card.dataset.assignee !== activeAssignee) show = false;
    if (iteration && card.dataset.iteration !== iteration) show = false;
    if (type && card.dataset.type !== type) show = false;
    if (search && !card.textContent.toLowerCase().includes(search)) show = false;
    card.classList.toggle('hidden', !show);
  }});

  // Update counts
  document.querySelectorAll('.column').forEach(col => {{
    const visible = col.querySelectorAll('.card:not(.hidden)').length;
    col.querySelector('.column__count').textContent = visible;
  }});
}}
</script>
</body>
</html>"""


# -- Main -----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="EDPA Board — HTML Kanban snapshot")
    parser.add_argument("--output", "-o", help="Output file path (default: .edpa/board.html)")
    parser.add_argument("--open", action="store_true", help="Open in browser after generating")
    parser.add_argument("--iteration", "-i", help="Filter by iteration prefix (e.g. PI-2026-1.4)")
    parser.add_argument("--level", "-l", help="Item level: initiative, epic, feature, story (default: story)")
    args = parser.parse_args()

    root = find_repo_root()
    if not root:
        print("Error: cannot find .edpa/ directory. Run from project root.")
        sys.exit(1)

    people, project_name = load_people(root)
    items = load_items(root)

    if not items:
        print("No backlog items found in .edpa/backlog/")
        sys.exit(1)

    level = args.level
    html_content = render_html(items, people, project_name, level_filter=level, iteration_filter=args.iteration)

    output = Path(args.output) if args.output else root / ".edpa" / "board.html"
    output.write_text(html_content, encoding="utf-8")
    print(f"Board written to {output}  ({len(items)} items loaded)")

    if args.open:
        if platform.system() == "Darwin":
            subprocess.run(["open", str(output)])
        elif platform.system() == "Linux":
            subprocess.run(["xdg-open", str(output)])
        else:
            print(f"Open {output} in your browser")


if __name__ == "__main__":
    main()
