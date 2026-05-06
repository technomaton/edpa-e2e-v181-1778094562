#!/usr/bin/env python3
"""
EDPA GitHub Project Views — Automated setup via Playwright.

Creates 8 views: All Items, Stories Board, Features Board, Epics Board,
WSJF Ranking, Current Iteration, My Work, Roadmap. Each hierarchy level
gets its own Board to avoid mixing types.

Uses persistent browser profile — log in once, then it remembers.

Usage:
    python create_project_views.py --url https://github.com/orgs/ORG/projects/N
    python create_project_views.py  # reads URL from .edpa/config/edpa.yaml
"""
import asyncio
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Install: pip install playwright && playwright install chromium")
    sys.exit(1)

PROFILE = Path.home() / ".edpa" / "playwright-profile"

# Views to create after renaming the default view to "All Items".
# Format: (name, filter, layout). Layout: "table" (default) or "board" or "roadmap".
# Each hierarchy level gets its own Board to avoid mixing Epics with Stories.
VIEWS = [
    ("Stories Board", "type:Story", "board"),
    ("Features Board", "type:Feature", "board"),
    ("Epics Board", "type:Epic", "board"),
    ("WSJF Ranking", "", "table"),
    ("Current Iteration", "type:Story", "board"),
    ("My Work", "assignee:@me", "table"),
    ("Roadmap", "", "roadmap"),
]

COLUMNS = ["Job Size", "Business Value", "Time Criticality",
           "Risk Reduction", "WSJF Score", "Team"]


def get_project_url():
    """Build project URL from .edpa/config/edpa.yaml."""
    config_path = Path(".edpa/config/edpa.yaml")
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f)
            sync = config.get("sync", {})
            org = sync.get("github_org", "")
            num = sync.get("github_project_number", "")
            if org and num:
                return f"https://github.com/orgs/{org}/projects/{num}"
        except Exception:
            pass
    return None


async def close_dialogs(page):
    """Dismiss any modal dialog or backdrop overlay."""
    for _ in range(5):
        dialog = page.locator('[role="dialog"]')
        if await dialog.count() > 0 and await dialog.first.is_visible():
            for sel in ['button:has-text("Save")', 'button:has-text("Cancel")']:
                btn = dialog.locator(sel)
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(1000)
                    break
            continue
        backdrop = page.locator('[class*="Backdrop"]')
        if await backdrop.count() > 0 and await backdrop.first.is_visible():
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(800)
            continue
        break


async def wait_for_login(page, timeout=300):
    """Wait until user is logged in (max timeout seconds)."""
    for _ in range(timeout):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=2000)
            url = page.url
            if "github.com/login" not in url and "github.com/session" not in url:
                if await page.locator('img.avatar-user, [data-login]').count() > 0:
                    return True
                if "github.com" in url:
                    await page.wait_for_timeout(2000)
                    return True
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return False


async def rename_tab(page, index, new_name):
    """Double-click tab at index to rename it."""
    tabs = page.locator('[role="tab"]')
    if await tabs.count() <= index:
        return False
    tab = tabs.nth(index)
    await tab.click()
    await page.wait_for_timeout(500)
    await tab.dblclick()
    await page.wait_for_timeout(1500)
    inp = page.locator('input[aria-label="Change view name"]')
    if await inp.count() == 0:
        inp = page.locator('input[aria-label="View name"]')
    if await inp.count() > 0:
        await inp.first.fill(new_name)
        await inp.first.press("Enter")
        await page.wait_for_timeout(1000)
        return True
    return False


async def delete_view(page, tab_index):
    """Delete a view by clicking its ▾ dropdown → Delete view → Confirm."""
    tabs = page.locator('[role="tab"]')
    name = (await tabs.nth(tab_index).text_content()).strip()
    await tabs.nth(tab_index).click()
    await page.wait_for_timeout(1000)
    await tabs.nth(tab_index).hover()
    await page.wait_for_timeout(500)

    # Click the ▾ caret (hidden until hover, requires force click)
    caret = page.locator('.selected[class*="view-tab"] [class*="viewOptionsPlaceholder"]')
    if await caret.count() == 0:
        return False
    await caret.first.click(force=True)
    await page.wait_for_timeout(1500)

    # Click "Delete view" (it's a <li>, not role=menuitem)
    delete_li = page.locator('li:has-text("Delete view"):visible')
    if await delete_li.count() == 0:
        await page.keyboard.press("Escape")
        return False
    await delete_li.first.click()
    await page.wait_for_timeout(2000)

    # Confirm deletion dialog
    dialog = page.locator('[role="alertdialog"]')
    if await dialog.count() > 0:
        confirm = dialog.locator('button:has-text("Delete")')
        if await confirm.count() > 0:
            await confirm.first.click()
            await page.wait_for_timeout(2000)
            return True
    return False


async def create_view(page, name, filter_text="", layout="table"):
    """Create a new view with name, optional filter, and layout (table/board/roadmap)."""
    await close_dialogs(page)

    # Click "+ New view" tab — opens a popover menu
    new_view = page.locator('[role="tab"]:has-text("New view")')
    await new_view.first.click(timeout=10000)
    await page.wait_for_timeout(1500)

    # Click the desired layout in the popover menu
    layout_label = {"table": "Table", "board": "Board", "roadmap": "Roadmap"}.get(layout, "Table")
    menu_item = page.locator(f'[role="menuitem"]:has-text("{layout_label}")')
    if await menu_item.count() > 0:
        await menu_item.first.click()
        await page.wait_for_timeout(3000)
    else:
        print(f"    ✗ No {layout_label} option in popover")
        return False

    # Rename the new tab (second-to-last; last is always "+ New view")
    tabs = page.locator('[role="tab"]')
    new_idx = await tabs.count() - 2
    ok = await rename_tab(page, new_idx, name)
    if not ok:
        print(f"    ⚠ Could not rename to '{name}'")

    # Apply filter
    if filter_text:
        fi = page.locator('input[name="filter-bar-component-inputname"]')
        if await fi.count() == 0:
            fi = page.locator('input[placeholder*="Filter"]')
        if await fi.count() > 0:
            await fi.first.click()
            await fi.first.fill(filter_text)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(1500)

    # Save
    await page.keyboard.press("Meta+s")
    await page.wait_for_timeout(2000)
    await close_dialogs(page)
    return True


async def add_columns(page, tab_name):
    """Add EDPA custom field columns to a view."""
    await close_dialogs(page)
    tabs = page.locator('[role="tab"]')
    for i in range(await tabs.count()):
        if (await tabs.nth(i).text_content()).strip() == tab_name:
            await tabs.nth(i).click()
            await page.wait_for_timeout(2000)
            break

    for col in COLUMNS:
        # Click the + button in table header (add-column-header class)
        add_btn = page.locator('button[class*="add-column-header"]')
        if await add_btn.count() == 0:
            print(f"    ✗ No + button found")
            break
        await add_btn.first.click()
        await page.wait_for_timeout(1500)

        # Fields appear as <li> elements under "Hidden fields"
        item = page.locator(f'li:has-text("{col}"):visible')
        found = False
        for j in range(await item.count()):
            text = (await item.nth(j).text_content()).strip()
            if text == col:
                await item.nth(j).click()
                await page.wait_for_timeout(1000)
                found = True
                break
        if not found:
            # Already visible or not available
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)

    await page.keyboard.press("Meta+s")
    await page.wait_for_timeout(1500)
    await close_dialogs(page)


async def main(project_url: str):
    PROFILE.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                str(PROFILE), headless=False, slow_mo=300,
                viewport={"width": 1400, "height": 900},
            )
        except Exception as e:
            print(f"\n  ✗ Cannot launch browser: {e}")
            print("  Install: pip install playwright && playwright install chromium")
            print(f"\n  Alternative: open the project in browser and create views manually:")
            print(f"  {project_url}")
            return

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Login check
        await page.goto("https://github.com")
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1500)

        if await page.locator('img.avatar-user, [data-login]').count() == 0:
            print("\n  Not logged in. Please log in in the browser window.")
            print("  Waiting up to 5 minutes...\n")
            await page.goto("https://github.com/login")
            if not await wait_for_login(page, 300):
                print("  ✗ Login timeout.")
                await ctx.close()
                return

        print("  ✓ Logged in")
        print(f"  Loading: {project_url}")
        try:
            await page.goto(project_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            await page.wait_for_timeout(3000)
        await page.wait_for_timeout(3000)

        if "projects" not in page.url:
            print("  ✗ Could not load project")
            await ctx.close()
            return
        print("  ✓ Project loaded\n")

        # Step 1: Rename default view
        print("  [1] Renaming default view → 'All Items'")
        if await rename_tab(page, 0, "All Items"):
            await page.keyboard.press("Meta+s")
            await page.wait_for_timeout(1500)
            await close_dialogs(page)
            print("    ✓ Done")
        else:
            print("    ⚠ Skipped")

        # Step 2: Create views
        for i, (name, filt, layout) in enumerate(VIEWS, 2):
            parts = []
            if filt:
                parts.append(f"filter: {filt}")
            if layout != "table":
                parts.append(f"layout: {layout}")
            desc = f" ({', '.join(parts)})" if parts else ""
            print(f"  [{i}] Creating '{name}'{desc}")
            ok = await create_view(page, name, filt, layout)
            print(f"    {'✓ Done' if ok else '✗ Failed'}")

        # Step 3: Add columns to table views only (board/roadmap don't have custom columns)
        table_views = ["All Items"] + [v[0] for v in VIEWS if v[2] == "table"]
        all_views = table_views
        print(f"\n  Adding columns to {len(all_views)} views...")
        for view in all_views:
            print(f"  [{view}]")
            await add_columns(page, view)
            print(f"    ✓ Columns added")

        print(f"\n  {'═' * 50}")
        print(f"  ✓ Views setup complete!")
        tabs = page.locator('[role="tab"]')
        for i in range(await tabs.count()):
            t = (await tabs.nth(i).text_content()).strip()
            if t != "New view":
                print(f"    {t}")
        print(f"  {'═' * 50}\n")

        try:
            await page.wait_for_timeout(60000)
        except Exception:
            pass
        await ctx.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EDPA Project Views — Playwright automation")
    parser.add_argument("--url", default=None, help="Project URL (default: from .edpa/config/edpa.yaml)")
    args = parser.parse_args()

    url = args.url or get_project_url()
    if not url:
        print("  Error: No project URL. Pass --url or configure .edpa/config/edpa.yaml")
        sys.exit(1)

    asyncio.run(main(url))
