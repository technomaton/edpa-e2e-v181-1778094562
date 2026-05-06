#!/usr/bin/env python3
"""
EDPA Backlog CLI -- Git-native backlog management tool.

Usage:
    python .claude/edpa/scripts/backlog.py tree                    # Full hierarchy
    python .claude/edpa/scripts/backlog.py tree --level epic       # Epics only
    python .claude/edpa/scripts/backlog.py tree --iteration PI-2026-1.1
    python .claude/edpa/scripts/backlog.py show S-200              # Item details
    python .claude/edpa/scripts/backlog.py status                  # Project status
    python .claude/edpa/scripts/backlog.py status --iteration PI-2026-1.1
    python .claude/edpa/scripts/backlog.py wsjf                    # WSJF ranking
    python .claude/edpa/scripts/backlog.py wsjf --level feature
    python .claude/edpa/scripts/backlog.py validate                # Integrity check
    python .claude/edpa/scripts/backlog.py add --type Story --parent F-100 --title "New story" --js 5 --assignee turyna
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml")
    sys.exit(1)


def _people_index(root: Path) -> dict:
    """Cache: people-by-id index loaded from .edpa/config/people.yaml.
    Returns {} if the file is absent so callers can fall back to bare ids."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _people_loader import load_people  # noqa: E402
    edpa_root = root if root else Path(".edpa")
    if not edpa_root.is_dir() and (Path.cwd() / ".edpa").is_dir():
        edpa_root = Path.cwd() / ".edpa"
    _, by_id = load_people(edpa_root)
    return by_id


def _assignee_display(person_id, by_id: dict) -> str:
    """Render @github_login for an assignee id when the github field is set;
    fall back to the bare internal id otherwise. Empty/None → '?'."""
    if not person_id:
        return "?"
    person = by_id.get(person_id)
    if not person:
        return person_id   # unknown — show what we have
    gh = person.get("github")
    return f"@{gh}" if gh else person_id


# -- ANSI Colors (EDPA palette) -----------------------------------------------

class C:
    """ANSI color codes matching EDPA design palette."""
    RESET    = "\033[0m"
    BOLD     = "\033[1m"
    DIM      = "\033[2m"
    # Level colors
    INIT     = "\033[35m"       # Magenta -- Initiative
    EPIC     = "\033[38;5;93m"  # Purple -- Epic
    FEAT     = "\033[36m"       # Cyan -- Feature
    STORY    = "\033[32m"       # Green -- Story
    # Status colors (SAFe workflow)
    DONE     = "\033[32m"       # Green
    IMPL     = "\033[33m"       # Yellow -- implementing
    VALIDATE = "\033[35m"       # Magenta -- validating
    DEPLOY   = "\033[38;5;208m" # Orange -- deploying
    RELEASE  = "\033[38;5;147m" # Light purple -- releasing
    ANALYZE  = "\033[34m"       # Blue -- analyzing
    BACKLOG  = "\033[37m"       # Light gray -- in backlog
    READY    = "\033[36m"       # Cyan -- ready to pull
    REVIEW   = "\033[38;5;81m"  # Cyan-blue -- reviewing
    FUNNEL   = "\033[38;5;245m" # Gray -- not yet started
    # Legacy aliases (kept for non-status uses)
    ACTIVE   = "\033[33m"       # Yellow
    PROGRESS = "\033[34m"       # Blue
    PLANNED  = "\033[37m"       # Light gray
    # Utility
    WARN     = "\033[33m"
    ERR      = "\033[31m"
    OK       = "\033[32m"
    HEADER   = "\033[38;5;147m"  # Light purple
    MUTED    = "\033[38;5;245m"  # Gray


def color(text, code):
    return f"{code}{text}{C.RESET}"


def bold(text):
    return f"{C.BOLD}{text}{C.RESET}"


# -- Box-drawing characters ----------------------------------------------------

PIPE   = "\u2502"   # |
TEE    = "\u251c"   # |-
ELBOW  = "\u2514"   # L
DASH   = "\u2500"   # -
DOT    = "\u2022"   # bullet
ARROW  = "\u2192"   # ->


# -- Type-directory mapping ----------------------------------------------------

TYPE_DIRS = {
    "Initiative": "initiatives",
    "Epic":       "epics",
    "Feature":    "features",
    "Story":      "stories",
    "Defect":     "defects",
}

PREFIX_TO_DIR = {
    "I": "initiatives",
    "E": "epics",
    "F": "features",
    "S": "stories",
    "D": "defects",
    "T": "stories",
}

# -- SAFe Status Workflows -----------------------------------------------------

PORTFOLIO_STATUSES = ["Funnel", "Reviewing", "Analyzing", "Ready", "Implementing", "Done"]
DELIVERY_STATUSES = ["Funnel", "Analyzing", "Backlog", "Implementing", "Validating", "Deploying", "Releasing", "Done"]


# -- Data Loading (file-per-item) ----------------------------------------------

def find_repo_root():
    """Walk up from CWD to find the repo root (contains .edpa/)."""
    p = Path.cwd()
    while p != p.parent:
        if (p / ".edpa" / "config" / "people.yaml").exists():
            return p
        p = p.parent
    return None


def load_backlog(root):
    """Load backlog from file-per-item directory structure.

    Reads project/people metadata from people.yaml, then globs all item
    files from backlog/initiatives/, backlog/epics/, backlog/features/, backlog/stories/ subdirectories.
    """
    edpa = root / ".edpa"

    # Load project/people metadata
    people_path = edpa / "config" / "people.yaml"
    backlog = yaml.safe_load(open(people_path, encoding="utf-8")) if people_path.exists() else {}

    # Load all items from type directories
    items = []
    for type_dir in ["initiatives", "epics", "features", "stories", "defects"]:
        dir_path = edpa / "backlog" / type_dir
        if dir_path.exists():
            for f in sorted(dir_path.glob("*.yaml")):
                item = yaml.safe_load(open(f, encoding="utf-8"))
                if item:
                    items.append(item)

    backlog["items"] = items
    return backlog


def load_item_direct(root, item_id):
    """Load a single item by reading its file directly (O(1) file access)."""
    prefix = item_id.split("-")[0] if "-" in item_id else ""
    type_dir = PREFIX_TO_DIR.get(prefix)
    if type_dir:
        path = root / ".edpa" / "backlog" / type_dir / f"{item_id}.yaml"
        if path.exists():
            item = yaml.safe_load(open(path, encoding="utf-8"))
            if item:
                item["level"] = TYPE_TO_LEVEL.get(item.get("type", ""), item.get("type", ""))
                return item
    return None


def load_iteration(root, iteration_id):
    path = root / ".edpa" / "iterations" / f"{iteration_id}.yaml"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(root):
    path = root / ".edpa" / "config" / "edpa.yaml"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# -- Utility: collect all items flat -------------------------------------------

TYPE_TO_LEVEL = {
    "Initiative": "Initiative",
    "Epic": "Epic",
    "Feature": "Feature",
    "Story": "Story",
}


def collect_items(backlog):
    """Collect all items into a flat list with level annotation.

    Works with the flat 'items' structure. Each item has a 'type' field
    (Initiative, Epic, Feature, Story) and a 'parent' reference.
    """
    items = []
    for item in backlog.get("items", []):
        entry = dict(item)
        entry["level"] = TYPE_TO_LEVEL.get(item.get("type", ""), item.get("type", ""))
        items.append(entry)
    return items


def find_item(backlog, item_id, root=None):
    """Find a single item by ID.

    Tries direct file access first (fast path), then falls back to
    searching the loaded items list.
    """
    # Fast path: direct file read
    if root:
        direct = load_item_direct(root, item_id)
        if direct:
            return direct

    # Fallback: search in-memory items list
    for item in backlog.get("items", []):
        if item.get("id") == item_id:
            entry = dict(item)
            entry["level"] = TYPE_TO_LEVEL.get(item.get("type", ""), item.get("type", ""))
            return entry
    return None


def get_children(backlog, parent_id):
    """Find all direct children of a given parent ID."""
    children = []
    for item in backlog.get("items", []):
        if item.get("parent") == parent_id:
            entry = dict(item)
            entry["level"] = TYPE_TO_LEVEL.get(item.get("type", ""), item.get("type", ""))
            children.append(entry)
    return children


def status_badge(status):
    """Return colored status badge."""
    s = status or "Unknown"
    status_color_map = {
        "Done": C.DONE,
        "Implementing": C.IMPL,
        "Validating": C.VALIDATE,
        "Deploying": C.DEPLOY,
        "Releasing": C.RELEASE,
        "Analyzing": C.ANALYZE,
        "Backlog": C.BACKLOG,
        "Ready": C.READY,
        "Reviewing": C.REVIEW,
        "Funnel": C.FUNNEL,
    }
    c = status_color_map.get(s)
    if c:
        return color(f"[{s}]", c)
    return f"[{s}]"


def level_color(level):
    if level == "Initiative":
        return C.INIT
    elif level == "Epic":
        return C.EPIC
    elif level == "Feature":
        return C.FEAT
    elif level == "Story":
        return C.STORY
    return C.RESET


def wsjf_score(item):
    """Compute WSJF = (bv + tc + rr) / js. Returns 0 if js is 0."""
    js = item.get("js", 0)
    if not js or js == 0:
        return 0.0
    bv = item.get("bv", 0)
    tc = item.get("tc", 0)
    rr = item.get("rr", 0)
    return round((bv + tc + rr) / js, 2)


def next_id_for_type(root, item_type):
    """Determine the next available numeric ID for a given type.

    Scans existing files in the type directory and returns the next
    sequential ID string (e.g. 'S-227').
    """
    prefix_map = {
        "Initiative": "I",
        "Epic":       "E",
        "Feature":    "F",
        "Story":      "S",
    }
    prefix = prefix_map.get(item_type)
    if not prefix:
        raise ValueError(f"Unknown item type: {item_type}")

    type_dir = TYPE_DIRS[item_type]
    dir_path = root / ".edpa" / "backlog" / type_dir

    max_num = 0
    if dir_path.exists():
        for f in dir_path.glob("*.yaml"):
            stem = f.stem  # e.g. "S-226"
            parts = stem.split("-")
            if len(parts) == 2:
                try:
                    num = int(parts[1])
                    if num > max_num:
                        max_num = num
                except ValueError:
                    pass

    return f"{prefix}-{max_num + 1}"


# -- Commands ------------------------------------------------------------------

def cmd_tree(backlog, args):
    """Display the work item hierarchy as a tree, built from parent references."""
    level_filter = getattr(args, "level", None)
    iter_filter = getattr(args, "iteration", None)

    print()
    print(bold(color("  EDPA Backlog Tree", C.HEADER)))
    print(color(f"  {backlog['project']['name']}", C.MUTED))
    print()

    # Get all initiatives (items with no parent / parent=null)
    initiatives = [i for i in backlog.get("items", []) if i.get("type") == "Initiative"]

    for init in initiatives:
        print(f"  {color(DOT, C.INIT)} {color(bold(init['id']), C.INIT)} {color(init['title'], C.INIT)}  {status_badge(init.get('status'))}")

        epics = get_children(backlog, init["id"])
        for ei, epic in enumerate(epics):
            is_last_epic = ei == len(epics) - 1
            econ = ELBOW if is_last_epic else TEE
            epad = "   " if is_last_epic else f"  {PIPE}"

            wsjf_val = epic.get("wsjf", wsjf_score(epic))
            epic_js = epic.get("js", 0)
            print(f"  {econ}{DASH}{DASH} {color(bold(epic['id']), C.EPIC)} {color(epic['title'], C.EPIC)}  "
                  f"{status_badge(epic.get('status'))}  "
                  f"{color(f'WSJF={wsjf_val}', C.MUTED)}  "
                  f"{color(f'JS={epic_js}', C.DIM)}")

            if level_filter in ("epic", "epics"):
                continue

            features = get_children(backlog, epic["id"])
            for fi, feat in enumerate(features):
                is_last_feat = fi == len(features) - 1
                fcon = ELBOW if is_last_feat else TEE
                fpad_char = " " if is_last_feat else PIPE

                wsjf_val = feat.get("wsjf", wsjf_score(feat))
                feat_js = feat.get("js", 0)
                print(f" {epad} {fcon}{DASH}{DASH} {color(bold(feat['id']), C.FEAT)} {color(feat['title'], C.FEAT)}  "
                      f"{status_badge(feat.get('status'))}  "
                      f"{color(f'WSJF={wsjf_val}', C.MUTED)}  "
                      f"{color(f'JS={feat_js}', C.DIM)}")

                if level_filter in ("feature", "features"):
                    continue

                stories = get_children(backlog, feat["id"])
                # Apply iteration filter if provided
                if iter_filter:
                    stories = [s for s in stories if s.get("iteration") == iter_filter]

                by_id = _people_index(getattr(args, "_root", None))
                for si, story in enumerate(stories):
                    is_last_story = si == len(stories) - 1
                    scon = ELBOW if is_last_story else TEE

                    story_iter = story.get('iteration', '?')
                    story_js = story.get('js', 0)
                    iter_tag = color(f"@{story_iter}", C.MUTED) if story.get("iteration") else ""
                    assignee_tag = color(f"-> {_assignee_display(story.get('assignee'), by_id)}", C.DIM) if story.get("assignee") else ""

                    inner_pad = " " if is_last_feat else PIPE
                    print(f" {epad}  {inner_pad}  {scon}{DASH}{DASH} {color(story['id'], C.STORY)} "
                          f"{color(story['title'], C.STORY)}  "
                          f"{status_badge(story.get('status'))}  "
                          f"{color(f'JS={story_js}', C.DIM)}  "
                          f"{iter_tag}  {assignee_tag}")

    print()


def cmd_show(backlog, args, root=None):
    """Show detailed information about a single item."""
    item_id = args.item_id
    item = find_item(backlog, item_id, root=root)

    if not item:
        print(color(f"  Error: Item '{item_id}' not found.", C.ERR))
        sys.exit(1)

    level = item.get("level", "?")
    lc = level_color(level)

    print()
    item_id_str = item['id']
    header_line = f"{DASH * 3} {item_id_str} {DASH * 40}"
    print(f"  {color(bold(header_line), lc)}")
    print(f"  {bold('Title:')}    {color(item.get('title', ''), lc)}")
    print(f"  {bold('Level:')}    {level}")
    print(f"  {bold('Status:')}   {status_badge(item.get('status'))}")

    by_id = _people_index(root)
    if item.get("owner"):
        print(f"  {bold('Owner:')}    {_assignee_display(item['owner'], by_id)}")
    if item.get("assignee"):
        print(f"  {bold('Assignee:')} {_assignee_display(item['assignee'], by_id)}")
    if item.get("iteration"):
        print(f"  {bold('Iteration:')} {item['iteration']}")
    if item.get("parent"):
        print(f"  {bold('Parent:')}   {item['parent']}")

    # SAFe scores
    js = item.get("js", 0)
    if js:
        bv = item.get("bv", 0)
        tc = item.get("tc", 0)
        rr = item.get("rr", 0)
        w = wsjf_score(item)
        print()
        print(f"  {bold('SAFe Scores:')}")
        print(f"    Job Size (JS):          {js}")
        print(f"    Business Value (BV):     {bv}")
        print(f"    Time Criticality (TC):   {tc}")
        print(f"    Risk Reduction (RR):     {rr}")
        print(f"    WSJF:                    {color(str(w), C.HEADER)}")

    # Epic Hypothesis Statement (SAFe 6)
    eh = item.get("epic_hypothesis")
    if eh:
        print()
        print(f"  {bold('Epic Hypothesis Statement:')}")
        print(f"    {bold('For')}     {eh.get('for', '')}")
        print(f"    {bold('Who')}     {eh.get('who', '')}")
        print(f"    {bold('The')}     {color(eh.get('the', ''), lc)}")
        print(f"    {bold('Is a')}    {eh.get('is_a', '')}")
        print(f"    {bold('That')}    {eh.get('that', '')}")
        print(f"    {bold('Unlike')}  {eh.get('unlike', '')}")
        print(f"    {bold('Our')}     {eh.get('our_solution', '')}")

        bh = eh.get("benefit_hypothesis", {})
        if bh:
            print()
            print(f"  {bold('Benefit Hypothesis:')}")
            print(f"    {color(bh.get('metric',''), C.FEAT)}: {bh.get('baseline','')} {ARROW} {color(bh.get('target',''), C.DONE)}")
            print(f"    Timeframe: {bh.get('timeframe', '')}")

        li = eh.get("leading_indicators", [])
        if li:
            print()
            print(f"  {bold('Leading Indicators:')}")
            for ind in li:
                print(f"    {color(DOT, C.DONE)} {ind}")

        la = eh.get("lagging_indicators", [])
        if la:
            print(f"  {bold('Lagging Indicators:')}")
            for ind in la:
                print(f"    {color(DOT, C.FEAT)} {ind}")

        kc = eh.get("kill_criteria", [])
        if kc:
            print()
            print(f"  {bold('Kill Criteria:')}")
            for k in kc:
                print(f"    {color(DOT, C.ERR)} {k}")

        lbc = eh.get("lean_business_case", {})
        if lbc:
            print()
            print(f"  {bold('Lean Business Case:')}")
            print(f"    Problem:     {lbc.get('problem', '')}")
            print(f"    Opportunity: {lbc.get('opportunity', '')}")
            print(f"    MVP:         {color(lbc.get('mvp', ''), C.DONE)}")
            opts = lbc.get("options_considered", [])
            if opts:
                print(f"    Options:")
                for o in opts:
                    print(f"      {DOT} {o}")

    elif item.get("hypothesis"):
        print()
        print(f"  {bold('Hypothesis:')}")
        print(f"    {color(item['hypothesis'], C.MUTED)}")

    # Contributors
    contribs = item.get("contributors") or []
    if contribs:
        print()
        print(f"  {bold('Contributors:')}")
        for c in contribs:
            person = c.get("person", "?")
            role = c.get("role", "?")
            cw = c.get("cw", "?")
            rs = c.get("rs", "")
            rs_str = f"  rs={rs}" if rs else ""
            print(f"    {DOT} {person:12s}  role={role:12s}  cw={cw}{rs_str}")

    # Child items -- find children via parent references
    children = get_children(backlog, item_id)
    child_epics = [c for c in children if c.get("type") == "Epic"]
    child_features = [c for c in children if c.get("type") == "Feature"]
    child_stories = [c for c in children if c.get("type") == "Story"]

    if child_epics:
        print()
        print(f"  {bold('Epics:')}")
        for e in child_epics:
            w = e.get("wsjf", wsjf_score(e))
            print(f"    {TEE}{DASH}{DASH} {color(e['id'], C.EPIC)} {e['title']}  "
                  f"{status_badge(e.get('status'))}  WSJF={w}")

    if child_features:
        print()
        print(f"  {bold('Features:')}")
        for f in child_features:
            w = f.get("wsjf", wsjf_score(f))
            print(f"    {TEE}{DASH}{DASH} {color(f['id'], C.FEAT)} {f['title']}  "
                  f"{status_badge(f.get('status'))}  WSJF={w}")

    if child_stories:
        print()
        print(f"  {bold('Stories:')}")
        for s in child_stories:
            print(f"    {TEE}{DASH}{DASH} {color(s['id'], C.STORY)} {s['title']}  "
                  f"{status_badge(s.get('status'))}  JS={s.get('js',0)}  "
                  f"@{s.get('iteration','?')}")

    print()


def cmd_status(backlog, args):
    """Show project or iteration status summary."""
    iter_filter = getattr(args, "iteration", None)

    if iter_filter:
        _show_iteration_status(backlog, iter_filter, args)
        return

    # Overall project status
    items = collect_items(backlog)
    stories = [i for i in items if i["level"] == "Story"]
    features = [i for i in items if i["level"] == "Feature"]
    epics = [i for i in items if i["level"] == "Epic"]

    done_stories = [s for s in stories if s.get("status") == "Done"]
    implementing = [s for s in stories if s.get("status") == "Implementing"]
    not_started = [s for s in stories if s.get("status") not in ("Done", "Implementing")]

    total_sp = sum(s.get("js", 0) for s in stories)
    done_sp = sum(s.get("js", 0) for s in done_stories)
    ip_sp = sum(s.get("js", 0) for s in implementing)

    pct = round(done_sp / total_sp * 100) if total_sp else 0

    print()
    print(bold(color("  EDPA Project Status", C.HEADER)))
    print(color(f"  {backlog['project']['name']}", C.MUTED))
    print(color(f"  {backlog['project']['registration']}  |  {backlog['project']['program']}", C.MUTED))
    print()

    # Progress bar
    bar_width = 40
    filled = int(bar_width * pct / 100)
    bar = color("\u2588" * filled, C.DONE) + color("\u2591" * (bar_width - filled), C.MUTED)
    print(f"  Progress: {bar} {bold(f'{pct}%')}")
    print()

    print(f"  {bold('Story Points:')}")
    print(f"    Total:        {total_sp} SP")
    print(f"    {color('Done:', C.DONE)}         {done_sp} SP  ({len(done_stories)} stories)")
    print(f"    {color('Implementing:', C.IMPL)}  {ip_sp} SP  ({len(implementing)} stories)")
    print(f"    {color('Remaining:', C.BACKLOG)}    {sum(s.get('js', 0) for s in not_started)} SP  ({len(not_started)} stories)")
    print()

    print(f"  {bold('Hierarchy:')}")
    print(f"    Epics:    {len(epics)}")
    print(f"    Features: {len(features)}")
    print(f"    Stories:  {len(stories)}")
    print()

    # Per-iteration velocity
    iter_ids = sorted(set(s.get("iteration") for s in stories if s.get("iteration")))
    if iter_ids:
        print(f"  {bold('Iteration Velocity:')}")
        for it_id in iter_ids:
            it_stories = [s for s in done_stories if s.get("iteration") == it_id]
            sp = sum(s.get("js", 0) for s in it_stories)
            bar_mini = color("\u2588" * (sp // 2), C.DONE) if sp else ""
            print(f"    {it_id:16s}  {sp:3d} SP  {bar_mini}")
        print()


def _show_iteration_status(backlog, iteration_id, args):
    """Show status for a specific iteration."""
    root = find_repo_root()
    iter_data = load_iteration(root, iteration_id) if root else None

    items = collect_items(backlog)
    stories = [i for i in items if i["level"] == "Story" and i.get("iteration") == iteration_id]

    if not stories:
        print(color(f"  No stories found for iteration '{iteration_id}'.", C.WARN))
        return

    done = [s for s in stories if s.get("status") == "Done"]
    total_sp = sum(s.get("js", 0) for s in stories)
    done_sp = sum(s.get("js", 0) for s in done)
    pct = round(done_sp / total_sp * 100) if total_sp else 0

    print()
    print(bold(color(f"  Iteration: {iteration_id}", C.HEADER)))

    if iter_data:
        it = iter_data.get("iteration", {})
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _pi_loader import format_iteration_dates  # noqa: E402
        weeks = it.get("weeks", "?")
        print(color(f"  {format_iteration_dates(it)}  |  Status: {it.get('status', '?')}  |  Weeks: {weeks}", C.MUTED))

    print()

    bar_width = 40
    filled = int(bar_width * pct / 100)
    bar = color("\u2588" * filled, C.DONE) + color("\u2591" * (bar_width - filled), C.MUTED)
    print(f"  Delivery: {bar} {bold(f'{pct}%')}  ({done_sp}/{total_sp} SP)")
    print()

    print(f"  {bold('Stories:')}")
    by_id = _people_index(getattr(args, "_root", None))
    for s in stories:
        assignee = _assignee_display(s.get("assignee"), by_id)
        print(f"    {color(s['id'], C.STORY):20s} {s['title']:30s}  {status_badge(s.get('status'))}  "
              f"JS={s.get('js',0)}  {color(f'-> {assignee}', C.DIM)}")
    print()

    if iter_data:
        edpa = iter_data.get("edpa", {})
        if edpa:
            inv = color("PASS", C.OK) if edpa.get("invariants_passed") else color("FAIL", C.ERR)
            print(f"  {bold('EDPA:')}")
            print(f"    Mode:       {edpa.get('mode', '?')}")
            print(f"    Invariants: {inv}")
            print()


def cmd_wsjf(backlog, args):
    """Display items ranked by WSJF score."""
    level_filter = getattr(args, "level", None)

    items = collect_items(backlog)

    if level_filter in ("epic", "epics"):
        candidates = [i for i in items if i["level"] == "Epic"]
    elif level_filter in ("feature", "features"):
        candidates = [i for i in items if i["level"] == "Feature"]
    else:
        # Default: show both epics and features
        candidates = [i for i in items if i["level"] in ("Epic", "Feature")]

    # Compute and sort by WSJF descending
    for c in candidates:
        c["_wsjf"] = c.get("wsjf", wsjf_score(c))

    candidates.sort(key=lambda x: x["_wsjf"], reverse=True)

    print()
    print(bold(color("  WSJF Priority Ranking", C.HEADER)))
    print()

    # Table header
    header = f"  {'Rank':>4}  {'ID':8s}  {'Title':30s}  {'WSJF':>6}  {'JS':>4}  {'BV':>4}  {'TC':>4}  {'RR':>4}  {'Status':12s}  Level"
    print(color(header, C.MUTED))
    print(color(f"  {'─' * 105}", C.MUTED))

    for rank, c in enumerate(candidates, 1):
        lc = level_color(c["level"])
        wsjf_val = c["_wsjf"]

        # WSJF color: high=green, medium=yellow, low=gray
        if wsjf_val >= 4.0:
            wsjf_str = color(f"{wsjf_val:6.2f}", C.OK)
        elif wsjf_val >= 2.5:
            wsjf_str = color(f"{wsjf_val:6.2f}", C.WARN)
        else:
            wsjf_str = color(f"{wsjf_val:6.2f}", C.MUTED)

        title = c.get("title", "")[:30]
        status = c.get("status", "?")

        print(f"  {rank:>4}  {color(c['id'], lc):18s}  {title:30s}  {wsjf_str}  "
              f"{c.get('js',0):>4}  {c.get('bv',0):>4}  {c.get('tc',0):>4}  {c.get('rr',0):>4}  "
              f"{status_badge(status):22s}  {color(c['level'], lc)}")

    print()
    print(color(f"  WSJF = (BV + TC + RR) / JS   |   Higher = prioritize first", C.MUTED))
    print()


def cmd_validate(backlog, args):
    """Validate backlog integrity."""
    items = collect_items(backlog)
    stories = [i for i in items if i["level"] == "Story"]
    features = [i for i in items if i["level"] == "Feature"]
    epics = [i for i in items if i["level"] == "Epic"]

    errors = []
    warnings = []

    # Build a set of all IDs for parent reference validation
    all_ids = {i.get("id") for i in items if i.get("id")}

    print()
    print(bold(color("  EDPA Backlog Validation", C.HEADER)))
    print()

    # 1. All stories must have assignee
    for s in stories:
        if not s.get("assignee"):
            errors.append(f"{s['id']} ({s.get('title','')}): missing assignee")

    # 2. All stories must have JS
    for s in stories:
        if not s.get("js") and s.get("js") != 0:
            errors.append(f"{s['id']} ({s.get('title','')}): missing JS (job size)")

    # 3. Story JS should be <= 8
    for s in stories:
        js = s.get("js", 0)
        if js and js > 8:
            warnings.append(f"{s['id']} ({s.get('title','')}): JS={js} exceeds recommended max of 8")

    # 4. All non-Initiative items must have a valid parent reference
    for item in items:
        if item.get("level") == "Initiative":
            continue
        parent = item.get("parent")
        if not parent:
            errors.append(f"{item['id']} ({item.get('title','')}): missing parent reference")
        elif parent not in all_ids:
            errors.append(f"{item['id']} ({item.get('title','')}): parent '{parent}' does not exist")

    # 5. All stories should have iteration
    for s in stories:
        if not s.get("iteration"):
            warnings.append(f"{s['id']} ({s.get('title','')}): missing iteration assignment")

    # 6. Check WSJF consistency
    for item in epics + features:
        stored_wsjf = item.get("wsjf")
        if stored_wsjf is not None:
            computed = wsjf_score(item)
            if abs(stored_wsjf - computed) > 0.05:
                warnings.append(f"{item['id']}: stored WSJF={stored_wsjf} != computed {computed}")

    # 7. Check for duplicate IDs
    ids = [i.get("id") for i in items if i.get("id")]
    seen = set()
    for item_id in ids:
        if item_id in seen:
            errors.append(f"Duplicate ID: {item_id}")
        seen.add(item_id)

    # 8. Check contributors CW values are reasonable
    for s in stories:
        contribs = s.get("contributors", [])
        for c in contribs:
            cw = c.get("cw", 0)
            if cw < 0 or cw > 1.5:
                warnings.append(f"{s['id']}: contributor {c.get('person','?')} has unusual cw={cw}")

    # 9. Validate type field exists on all items
    for item in items:
        if not item.get("type"):
            errors.append(f"{item.get('id', '?')}: missing 'type' field")

    # 10. Validate parent type hierarchy (Initiative > Epic > Feature > Story)
    valid_parent_type = {
        "Epic": "Initiative",
        "Feature": "Epic",
        "Story": "Feature",
    }
    items_by_id = {i.get("id"): i for i in items if i.get("id")}
    for item in items:
        if item.get("level") == "Initiative":
            continue
        parent_id = item.get("parent")
        if parent_id and parent_id in items_by_id:
            parent_item = items_by_id[parent_id]
            expected_parent_type = valid_parent_type.get(item.get("type"))
            actual_parent_type = parent_item.get("type")
            if expected_parent_type and actual_parent_type != expected_parent_type:
                errors.append(
                    f"{item['id']} ({item.get('title','')}): parent {parent_id} is "
                    f"{actual_parent_type}, expected {expected_parent_type}"
                )

    # 11. Validate Fibonacci values for JS, BV, TC, RR
    fibonacci = {1, 2, 3, 5, 8, 13, 20}
    for item in items:
        for field in ("js", "bv", "tc", "rr"):
            val = item.get(field)
            if val is not None and val != 0:
                try:
                    num = int(float(val))
                    if num > 0 and num not in fibonacci:
                        warnings.append(
                            f"{item.get('id', '?')}: {field}={num} is not Fibonacci "
                            f"(valid: {', '.join(str(f) for f in sorted(fibonacci))})"
                        )
                except (ValueError, TypeError):
                    pass

    # 12. Validate statuses against SAFe workflows per item type
    portfolio_types = {"Initiative", "Epic"}
    delivery_types = {"Feature", "Story", "Defect"}
    for item in items:
        item_type = item.get("type") or item.get("level", "")
        item_status = item.get("status", "")
        if not item_status:
            continue
        if item_type in portfolio_types:
            if item_status not in PORTFOLIO_STATUSES:
                errors.append(
                    f"{item.get('id', '?')}: status '{item_status}' is not valid for {item_type} "
                    f"(valid: {', '.join(PORTFOLIO_STATUSES)})"
                )
        elif item_type in delivery_types:
            if item_status not in DELIVERY_STATUSES:
                errors.append(
                    f"{item.get('id', '?')}: status '{item_status}' is not valid for {item_type} "
                    f"(valid: {', '.join(DELIVERY_STATUSES)})"
                )

    # Print results
    checks = [
        ("Story assignees present", not any("missing assignee" in e for e in errors)),
        ("Story JS values present", not any("missing JS" in e for e in errors)),
        ("Story JS <= 8", not any("exceeds recommended" in w for w in warnings)),
        ("Parent references valid", not any("parent" in e.lower() for e in errors)),
        ("Parent type hierarchy", not any("expected" in e.lower() for e in errors)),
        ("Iteration assignments", not any("missing iteration" in w for w in warnings)),
        ("WSJF consistency", not any("stored WSJF" in w for w in warnings)),
        ("No duplicate IDs", not any("Duplicate ID" in e for e in errors)),
        ("CW values valid", not any("unusual cw" in w for w in warnings)),
        ("Type fields present", not any("missing 'type'" in e for e in errors)),
        ("Fibonacci values", not any("not Fibonacci" in w for w in warnings)),
        ("SAFe status values", not any("is not valid for" in e for e in errors)),
    ]

    for label, passed in checks:
        icon = color("PASS", C.OK) if passed else color("FAIL", C.ERR)
        print(f"  [{icon}]  {label}")

    if errors:
        print()
        print(f"  {bold(color('Errors:', C.ERR))}")
        for e in errors:
            print(f"    {color('x', C.ERR)} {e}")

    if warnings:
        print()
        print(f"  {bold(color('Warnings:', C.WARN))}")
        for w in warnings:
            print(f"    {color('!', C.WARN)} {w}")

    print()
    print(f"  {bold('Summary:')}")
    print(f"    Items:    {len(items)}")
    print(f"    Stories:  {len(stories)}")
    print(f"    Errors:   {color(str(len(errors)), C.ERR if errors else C.OK)}")
    print(f"    Warnings: {color(str(len(warnings)), C.WARN if warnings else C.OK)}")

    if not errors and not warnings:
        print()
        print(f"  {color('All checks passed. Backlog is valid.', C.OK)}")
    elif not errors:
        print()
        print(f"  {color('No errors. Backlog is valid (with warnings).', C.WARN)}")
    else:
        print()
        print(f"  {color('Backlog has errors that should be fixed.', C.ERR)}")

    print()
    return len(errors)


def cmd_add(root, backlog, args):
    """Add a new work item, creating a YAML file in the appropriate directory."""
    item_type = args.type
    parent_id = args.parent
    title = args.title
    js = args.js
    assignee = getattr(args, "assignee", None)
    status = getattr(args, "status", None) or "Funnel"
    iteration = getattr(args, "iteration", None)
    bv = getattr(args, "bv", None)
    tc = getattr(args, "tc", None)
    rr = getattr(args, "rr", None)

    # Validate type
    if item_type not in TYPE_DIRS:
        print(color(f"  Error: Invalid type '{item_type}'. Must be one of: {', '.join(TYPE_DIRS.keys())}", C.ERR))
        sys.exit(1)

    # Validate parent exists (unless Initiative)
    if item_type != "Initiative":
        if not parent_id:
            print(color(f"  Error: --parent is required for type '{item_type}'.", C.ERR))
            sys.exit(1)
        parent_item = find_item(backlog, parent_id, root=root)
        if not parent_item:
            print(color(f"  Error: Parent '{parent_id}' not found.", C.ERR))
            sys.exit(1)

    # Generate next ID
    new_id = next_id_for_type(root, item_type)

    # Build item data
    item_data = {
        "id": new_id,
        "type": item_type,
        "title": title,
        "status": status,
        "parent": parent_id,
    }

    if js is not None:
        item_data["js"] = js
    if bv is not None:
        item_data["bv"] = bv
    if tc is not None:
        item_data["tc"] = tc
    if rr is not None:
        item_data["rr"] = rr
    if assignee:
        item_data["assignee"] = assignee
    if iteration:
        item_data["iteration"] = iteration

    # Compute WSJF if we have enough data
    if js and js > 0:
        _bv = bv or 0
        _tc = tc or 0
        _rr = rr or 0
        if _bv or _tc or _rr:
            item_data["wsjf"] = round((_bv + _tc + _rr) / js, 2)

    # Ensure directory exists
    type_dir = TYPE_DIRS[item_type]
    dir_path = root / ".edpa" / "backlog" / type_dir
    dir_path.mkdir(parents=True, exist_ok=True)

    # Write YAML file
    file_path = dir_path / f"{new_id}.yaml"
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(item_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print()
    print(f"  {color('Created:', C.OK)} {color(bold(new_id), level_color(item_type))} {title}")
    print(f"  {color('File:', C.MUTED)}    {file_path}")
    print()


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="backlog",
        description="EDPA Git-native backlog management CLI",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # tree
    p_tree = sub.add_parser("tree", help="Display work item hierarchy")
    p_tree.add_argument("--level", choices=["epic", "epics", "feature", "features", "story", "stories"],
                        help="Filter to specific level")
    p_tree.add_argument("--iteration", help="Filter stories to specific iteration (e.g. PI-2026-1.1)")

    # show
    p_show = sub.add_parser("show", help="Show details for a specific item")
    p_show.add_argument("item_id", help="Work item ID (e.g. S-200, E-10, F-100)")

    # status
    p_status = sub.add_parser("status", help="Show project/iteration status")
    p_status.add_argument("--iteration", help="Show status for specific iteration")

    # wsjf
    p_wsjf = sub.add_parser("wsjf", help="Show WSJF-ranked backlog")
    p_wsjf.add_argument("--level", choices=["epic", "epics", "feature", "features"],
                        help="Filter to specific level")

    # validate
    sub.add_parser("validate", help="Validate backlog integrity")

    # add
    p_add = sub.add_parser("add", help="Add a new work item")
    p_add.add_argument("--type", required=True, choices=["Initiative", "Epic", "Feature", "Story"],
                       help="Item type")
    p_add.add_argument("--parent", help="Parent item ID (required for Epic, Feature, Story)")
    p_add.add_argument("--title", required=True, help="Item title")
    p_add.add_argument("--js", type=int, help="Job Size")
    p_add.add_argument("--bv", type=int, help="Business Value")
    p_add.add_argument("--tc", type=int, help="Time Criticality")
    p_add.add_argument("--rr", type=int, help="Risk Reduction")
    p_add.add_argument("--assignee", help="Assignee (person ID)")
    p_add.add_argument("--status", default="Funnel", help="Status (default: Funnel)")
    p_add.add_argument("--iteration", help="Iteration ID (e.g. PI-2026-1.3)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    root = find_repo_root()
    if root is None:
        print(color("Error: Cannot find .edpa/ directory. Run from the EDPA project directory.", C.ERR))
        sys.exit(1)

    backlog = load_backlog(root)
    # Stash .edpa/ path on args so cmd_* helpers (people index, etc.)
    # don't need an extra parameter through the call chain.
    args._root = root / ".edpa"

    if args.command == "tree":
        cmd_tree(backlog, args)
    elif args.command == "show":
        cmd_show(backlog, args, root=root)
    elif args.command == "status":
        cmd_status(backlog, args)
    elif args.command == "wsjf":
        cmd_wsjf(backlog, args)
    elif args.command == "validate":
        err_count = cmd_validate(backlog, args)
        sys.exit(1 if err_count else 0)
    elif args.command == "add":
        cmd_add(root, backlog, args)


if __name__ == "__main__":
    main()
