"""
BambooHR UI Capture Script - Full Coverage

Covers ALL workflow steps from the BambooHR Workflows PDF (5 pages)
and captures screenshots for all 35 MCP tools.

Usage:
    BAMBOO_URL=https://yourco.bamboohr.com BAMBOO_EMAIL=you@example.com BAMBOO_PASSWORD=secret \
        uv run python scripts/capture_bamboohr.py

Output goes to: captures/bamboohr/
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page, async_playwright

# -- Config --
BASE_URL = os.environ.get("BAMBOO_URL", "https://mercor.bamboohr.com")
EMAIL = os.environ.get("BAMBOO_EMAIL", "")
PASSWORD = os.environ.get("BAMBOO_PASSWORD", "")

OUTPUT_DIR = Path("captures/bamboohr")
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"
ELEMENTS_DIR = OUTPUT_DIR / "elements"
WORKFLOWS_DIR = OUTPUT_DIR / "workflows"
HAR_DIR = OUTPUT_DIR / "har"

VIEWPORT = {"width": 1440, "height": 900}
SLOW_MO = 200


# -- Helpers --


def ensure_dirs():
    for d in [SCREENSHOTS_DIR, ELEMENTS_DIR, WORKFLOWS_DIR, HAR_DIR]:
        d.mkdir(parents=True, exist_ok=True)


async def safe_click(page: Page, selector: str, timeout: int = 5000):
    """Click an element if it exists, return True/False."""
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=timeout)
        await el.click()
        await page.wait_for_load_state("networkidle", timeout=10000)
        return True
    except Exception:
        return False


async def safe_click_no_nav(page: Page, selector: str, timeout: int = 5000):
    """Click without waiting for navigation (for modals, dropdowns)."""
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=timeout)
        await el.click()
        await asyncio.sleep(1)
        return True
    except Exception:
        return False


async def dismiss_popups(page: Page):
    """Dismiss cookie banners, modals, overlays."""
    for sel in [
        'button:has-text("Accept")',
        'button:has-text("Got it")',
        'button:has-text("Close")',
        'button:has-text("Dismiss")',
        '[data-dismiss="modal"]',
        'button[aria-label="Close"]',
    ]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                await el.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass


async def extract_elements(page: Page) -> dict:
    """Extract all interactive elements from the current page."""
    return await page.evaluate("""() => {
        function getClassStr(el) {
            // el.className is an SVGAnimatedString on SVG elements, not a string
            if (typeof el.className === 'string') return el.className.trim();
            if (el.classList && el.classList.length > 0) return Array.from(el.classList).join(' ');
            return el.getAttribute('class') || '';
        }
        function getSelector(el) {
            const elId = el.getAttribute('id');
            if (elId && typeof elId === 'string' && elId !== 'undefined') return '#' + elId;
            if (el.getAttribute('data-testid')) return '[data-testid="' + el.getAttribute('data-testid') + '"]';
            if (el.getAttribute('data-bi-id')) return '[data-bi-id="' + el.getAttribute('data-bi-id') + '"]';
            const cls = getClassStr(el);
            if (cls) {
                const first3 = cls.split(/\\s+/).slice(0, 3).join('.');
                if (first3) return el.tagName.toLowerCase() + '.' + first3;
            }
            return el.tagName.toLowerCase();
        }
        function getInfo(el) {
            const elId = el.getAttribute('id') || null;
            return {
                tag: el.tagName.toLowerCase(),
                text: (el.textContent || '').trim().slice(0, 200),
                id: elId,
                name: el.getAttribute('name') || null,
                type: el.getAttribute('type') || null,
                href: (el.getAttribute('href') && el.getAttribute('href') !== 'undefined') ? el.getAttribute('href') : null,
                placeholder: el.getAttribute('placeholder') || null,
                ariaLabel: el.getAttribute('aria-label') || null,
                classes: getClassStr(el),
                selector: getSelector(el),
                required: el.required || false,
                disabled: el.disabled || false,
                rect: el.getBoundingClientRect().toJSON(),
                visible: el.offsetParent !== null || el.tagName === 'BODY',
            };
        }
        const results = { buttons: [], links: [], inputs: [], selects: [], forms: [], modals: [], tabs: [], tables: [] };
        document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"], a.btn, .fab-Button')
            .forEach(el => { if (el.offsetParent !== null) results.buttons.push(getInfo(el)); });
        document.querySelectorAll('a[href], nav a, [role="link"], [role="tab"], .nav-link, .fab-NavItem')
            .forEach(el => { if (el.offsetParent !== null) results.links.push(getInfo(el)); });
        document.querySelectorAll('input, textarea, [contenteditable="true"]')
            .forEach(el => { if (el.offsetParent !== null) results.inputs.push(getInfo(el)); });
        document.querySelectorAll('select, [role="listbox"], [role="combobox"], .fab-Select, .fab-Dropdown')
            .forEach(el => { if (el.offsetParent !== null) results.selects.push(getInfo(el)); });
        document.querySelectorAll('form')
            .forEach(el => results.forms.push({
                ...getInfo(el), action: el.getAttribute('action') || null, method: el.getAttribute('method') || null,
                fieldCount: el.querySelectorAll('input, select, textarea').length,
            }));
        document.querySelectorAll('[role="dialog"], .modal, .fab-Modal, .fab-Dialog, [class*="modal"]')
            .forEach(el => results.modals.push(getInfo(el)));
        document.querySelectorAll('[role="tablist"] [role="tab"], .fab-Tabs [role="tab"], .tab-item')
            .forEach(el => results.tabs.push(getInfo(el)));
        document.querySelectorAll('table, [role="grid"], .fab-Table')
            .forEach(el => {
                const headers = Array.from(el.querySelectorAll('th, [role="columnheader"]')).map(th => (th.textContent || '').trim());
                const rowCount = el.querySelectorAll('tr, [role="row"]').length;
                results.tables.push({ ...getInfo(el), headers, rowCount });
            });
        results.meta = {
            title: document.title, url: window.location.href,
            h1: Array.from(document.querySelectorAll('h1')).map(h => h.textContent.trim()),
            h2: Array.from(document.querySelectorAll('h2')).map(h => h.textContent.trim()),
            breadcrumbs: Array.from(document.querySelectorAll('[class*="breadcrumb"] a, nav[aria-label="breadcrumb"] a'))
                .map(a => ({ text: a.textContent.trim(), href: a.href })),
        };
        return results;
    }""")


async def screenshot(page: Page, name: str, full_page: bool = True):
    """Take a screenshot and save extracted elements."""
    safe_name = re.sub(r"[^\w\-]", "_", name)
    await page.screenshot(path=str(SCREENSHOTS_DIR / f"{safe_name}.png"), full_page=full_page)
    elements = await extract_elements(page)
    with open(ELEMENTS_DIR / f"{safe_name}.json", "w") as f:
        json.dump(elements, f, indent=2, default=str)
    print(f"  [screenshot] {name} ({elements['meta']['url']})")
    return elements


SCROLL_JS = """() => {
    // BambooHR uses an inner scrollable container, not the window.
    // Try common content area selectors, fall back to window scroll.
    const candidates = [
        document.querySelector('#js-main-content'),
        document.querySelector('[class*="MainContent"]'),
        document.querySelector('main'),
        document.querySelector('[role="main"]'),
        ...Array.from(document.querySelectorAll('div')).filter(el => {
            const s = getComputedStyle(el);
            return (s.overflowY === 'auto' || s.overflowY === 'scroll')
                && el.scrollHeight > el.clientHeight + 50
                && el.clientHeight > 200;
        }),
    ].filter(Boolean);
    return candidates[0] || null;
}"""


async def scroll_to(page: Page, position: str = "bottom"):
    """Scroll the main content area (or window fallback) to a position."""
    pos_map = {"top": "0", "middle": "el.scrollHeight / 2", "bottom": "el.scrollHeight"}
    pos_val = pos_map.get(position, "el.scrollHeight")
    scrolled = await page.evaluate(f"""() => {{
        const el = ({SCROLL_JS})();
        if (el) {{
            el.scrollTop = {pos_val};
            return true;
        }}
        window.scrollTo(0, {pos_val.replace("el.", "document.body.")});
        return false;
    }}""")
    await asyncio.sleep(0.5)
    return scrolled


async def scroll_and_screenshot(page: Page, name: str):
    """Screenshot top, middle, bottom of a page."""
    await scroll_to(page, "top")
    await screenshot(page, f"{name}_top")
    await scroll_to(page, "middle")
    await screenshot(page, f"{name}_mid")
    await scroll_to(page, "bottom")
    await screenshot(page, f"{name}_bottom")


# -- Trusted Browser Handling --


async def handle_trusted_browser(page: Page):
    """Click 'Yes, Trust this Browser' if the interstitial appears."""
    if "trusted_browser" in page.url or "auth/trusted" in page.url:
        try:
            btn = page.locator(
                'button:has-text("Yes, Trust this Browser"), button:has-text("Trust this Browser")'
            )
            await btn.wait_for(state="visible", timeout=5000)
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(2)
            print("  [OK] Trusted browser confirmed")
        except Exception:
            # Try "No Thanks" as fallback to just proceed
            try:
                no_btn = page.locator('button:has-text("No Thanks")')
                await no_btn.wait_for(state="visible", timeout=2000)
                await no_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                await asyncio.sleep(2)
                print("  [OK] Trusted browser dismissed (No Thanks)")
            except Exception:
                print("  [WARN] Trusted browser page detected but no button found")


# -- Login --


async def login(page: Page):
    print("\n[LOGIN] Logging in...")
    await page.goto(f"{BASE_URL}/login.php", wait_until="networkidle")
    await screenshot(page, "00_login_page")

    await page.fill("#lemail", EMAIL)
    await page.fill("#password", PASSWORD)
    await screenshot(page, "00_login_filled")

    await page.click('button[type="submit"]:has-text("Log In")')
    try:
        await page.wait_for_url("**/home**", timeout=15000)
    except Exception:
        await page.wait_for_load_state("networkidle", timeout=15000)

    await asyncio.sleep(3)

    # Handle "Trust this Browser" interstitial
    await handle_trusted_browser(page)

    await dismiss_popups(page)
    await screenshot(page, "00_logged_in")
    print(f"  [OK] Logged in at: {page.url}")


# =====================================================================
# PAGE CAPTURES - covers all MCP tools
# =====================================================================


async def capture_home(page: Page):
    """Home dashboard. Tools: get_balances, get_whos_out"""
    print("\n[PAGE] Home Dashboard...")
    await page.goto(f"{BASE_URL}/home", wait_until="networkidle")
    await asyncio.sleep(2)
    await dismiss_popups(page)
    await scroll_and_screenshot(page, "01_home")

    # Who's Out calendar (get_whos_out)
    if await safe_click(
        page, 'a:has-text("full calendar"), a:has-text("Full Calendar")', timeout=3000
    ):
        await asyncio.sleep(2)
        await screenshot(page, "01_home_whos_out_calendar")
        await page.go_back()
        await page.wait_for_load_state("networkidle")


async def capture_my_info(page: Page):
    """My Info profile. Tools: get (self), get_balances, get_employee_policies"""
    print("\n[PAGE] My Info...")
    # Self employee id is 8 - navigate directly to profile
    self_id = 8
    await page.goto(f"{BASE_URL}/employees/employee.php?id={self_id}", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "02_myinfo_default")

    # Tabs via direct URLs (more reliable than clicking tab links)
    tab_urls = {
        "personal": f"/employees/employee.php?id={self_id}&page=2097",
        "job": f"/employees/employee.php?id={self_id}&page=2098",
        "time_off": f"/employees/pto/?id={self_id}",
        "documents": f"/employees/files/employeeFilesPage?id={self_id}",
        "benefits": f"/employees/benefits/?id={self_id}",
        "emergency": f"/employees/contacts.php?id={self_id}&e={self_id}",
    }
    for tab_name, tab_path in tab_urls.items():
        try:
            await page.goto(f"{BASE_URL}{tab_path}", wait_until="networkidle")
            await asyncio.sleep(1.5)
            await screenshot(page, f"02_myinfo_{tab_name}")
            await scroll_to(page, "bottom")
            await screenshot(page, f"02_myinfo_{tab_name}_bottom")
        except Exception as e:
            print(f"    [WARN] My Info tab {tab_name} failed: {e}")


async def capture_people_list(page: Page):
    """People list view. Tools: get_directory, search.employees"""
    print("\n[PAGE] People List View...")
    await page.goto(f"{BASE_URL}/employees/directory.php", wait_until="networkidle")
    await asyncio.sleep(2)

    # Directory card view first
    await screenshot(page, "03_people_directory_cards")

    # Switch to List view
    if await safe_click(page, 'button:has-text("List"), a:has-text("List")', timeout=3000):
        await asyncio.sleep(2)
        await screenshot(page, "03_people_list_view")
        await scroll_to(page, "bottom")
        await screenshot(page, "03_people_list_view_bottom")


async def capture_employee_profile(page: Page):
    """Employee profile from People page. Tools: get, get_employee_policies, get_balances"""
    print("\n[PAGE] Employee Profile (jane doe)...")

    # Navigate directly to jane doe (employee list shows her)
    # First go to directory and find clickable employee name
    await page.goto(f"{BASE_URL}/employees/directory.php", wait_until="networkidle")
    await asyncio.sleep(2)

    # Click an employee in directory card view
    clicked = await safe_click(page, 'a:has-text("jane doe"), a:has-text("Jane Doe")', timeout=5000)
    if not clicked:
        # Navigate directly to a known employee (114 worked in previous run)
        await page.goto(f"{BASE_URL}/employees/employee.php?id=114", wait_until="networkidle")

    await asyncio.sleep(2)
    await screenshot(page, "03_employee_profile_default")

    # Capture all profile tabs
    for tab_name in ["Personal", "Job", "Time Off", "Emergency", "Documents", "Notes", "Training"]:
        if await safe_click(
            page,
            f'a:has-text("{tab_name}"):not([data-bi-id*="main-nav"]):not([data-bi-id*="settings"])',
            timeout=3000,
        ):
            await asyncio.sleep(1.5)
            await screenshot(page, f"03_employee_profile_{tab_name.lower().replace(' ', '_')}")
            await scroll_to(page, "bottom")
            await screenshot(
                page, f"03_employee_profile_{tab_name.lower().replace(' ', '_')}_bottom"
            )
        else:
            print(f"    [WARN] Employee tab not found: {tab_name}")


async def capture_new_employee(page: Page):
    """New Employee form. Tools: bamboo_employees_create"""
    print("\n[PAGE] New Employee Form...")
    await page.goto(f"{BASE_URL}/employees/new.php", wait_until="networkidle")
    await asyncio.sleep(2)
    await scroll_and_screenshot(page, "04_new_employee")

    # Try to find and click "Allow Access to BambooHR"
    allow_selectors = [
        'label:has-text("Allow Access")',
        'text="Allow Access to BambooHR"',
        'input[name*="access"]',
        'label:has-text("self service")',
        'input[id*="selfService"]',
    ]
    for sel in allow_selectors:
        if await safe_click_no_nav(page, sel, timeout=2000):
            await asyncio.sleep(1)
            await screenshot(page, "04_new_employee_allow_access")
            # After clicking, work email may become required
            await scroll_and_screenshot(page, "04_new_employee_access_enabled")
            break


async def capture_settings_main(page: Page):
    """Settings overview. Tools: get_company_info, get_users, get_fields, get_list_fields"""
    print("\n[PAGE] Settings Main...")
    await page.goto(f"{BASE_URL}/settings/", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "05_settings_main")
    await scroll_to(page, "bottom")
    await screenshot(page, "05_settings_main_bottom")

    # Capture Employee Fields page (get_fields, get_list_fields, get_field_options, update_field_options)
    if await safe_click(page, '[data-bi-id="settings-employee-fields-link"]', timeout=3000):
        await asyncio.sleep(2)
        await screenshot(page, "05_settings_employee_fields")
        await scroll_to(page, "bottom")
        await screenshot(page, "05_settings_employee_fields_bottom")


async def capture_settings_timeoff(page: Page):
    """Settings > Time Off. Tools: get_policies, get_types, create_type, create_policy, assign_policy, update_balance"""
    print("\n[PAGE] Settings > Time Off (deep)...")

    # Navigate directly to Time Off settings
    await page.goto(f"{BASE_URL}/settings/pto/", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "06_timeoff_overview")
    await scroll_to(page, "bottom")
    await screenshot(page, "06_timeoff_overview_bottom")

    # Click each policy link (they use fabric-x8rxmk-root-root class and href="#id")
    policy_selectors = [
        'a:has-text("Freebie")',
        'a:has-text("Maternity leave")',
        'a:has-text("Salary Employees")',
    ]
    for idx, sel in enumerate(policy_selectors):
        try:
            if await safe_click_no_nav(page, sel, timeout=3000):
                await asyncio.sleep(1.5)
                # Policy detail appears inline or as panel
                await screenshot(page, f"06_timeoff_policy_{idx}")
                await scroll_to(page, "bottom")
                await screenshot(page, f"06_timeoff_policy_{idx}_bottom")

                # Look for Assign/Add Employees button (assign_policy)
                assign_clicked = await safe_click_no_nav(
                    page,
                    'button:has-text("Assign"), button:has-text("Add Employees to Policy"), '
                    'button:has-text("Manage Employees")',
                    timeout=3000,
                )
                if assign_clicked:
                    await asyncio.sleep(1.5)
                    await screenshot(page, f"06_timeoff_policy_{idx}_assign_modal")
                    # Scroll to see dual-list picker and extra inputs
                    await page.evaluate("""
                        const dialog = document.querySelector('[role="dialog"]');
                        if (dialog) dialog.scrollTop = dialog.scrollHeight;
                    """)
                    await asyncio.sleep(0.5)
                    await screenshot(page, f"06_timeoff_policy_{idx}_assign_modal_bottom")
                    # Close modal
                    await safe_click_no_nav(
                        page,
                        'button:has-text("Cancel"), button[aria-label="Close"], [class*="close"]',
                        timeout=2000,
                    )

                # Go back to overview for next policy
                await page.goto(f"{BASE_URL}/settings/pto/", wait_until="networkidle")
                await asyncio.sleep(1)
        except Exception as e:
            print(f"    [WARN] Policy {idx}: {e}")

    # "New Policy" wizard (create_policy)
    print("    Capturing New Policy wizard...")
    await page.goto(f"{BASE_URL}/settings/pto/", wait_until="networkidle")
    await asyncio.sleep(1)

    # Click "New Policy" button
    if await safe_click_no_nav(
        page,
        '[data-bi-id="settings-TimeOff-new-policy-button"], button:has-text("New Policy")',
        timeout=3000,
    ):
        await asyncio.sleep(1.5)
        await screenshot(page, "06_timeoff_new_policy_wizard_step1")

        # Click "It accrues time (traditional)" option
        if await safe_click_no_nav(page, 'text="It accrues time"', timeout=3000):
            await asyncio.sleep(0.5)
            await screenshot(page, "06_timeoff_new_policy_traditional_selected")

        # Click "Create Policy" to proceed to next step
        if await safe_click(page, 'button:has-text("Create Policy")', timeout=3000):
            await asyncio.sleep(2)
            await screenshot(page, "06_timeoff_new_policy_step2_name")
            await scroll_to(page, "bottom")
            await screenshot(page, "06_timeoff_new_policy_step2_bottom")
            # Go back without saving
            await page.go_back()
            await page.wait_for_load_state("networkidle")

        # Close wizard if still open
        await safe_click_no_nav(
            page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
        )

    # Click "It's flexible (unlimited)" option
    await page.goto(f"{BASE_URL}/settings/pto/", wait_until="networkidle")
    await asyncio.sleep(1)
    if await safe_click_no_nav(
        page,
        '[data-bi-id="settings-TimeOff-new-policy-button"], button:has-text("New Policy")',
        timeout=3000,
    ):
        await asyncio.sleep(1.5)
        if await safe_click_no_nav(page, 'text="flexible"', timeout=3000):
            await asyncio.sleep(0.5)
            await screenshot(page, "06_timeoff_new_policy_flexible_selected")

        if await safe_click(page, 'button:has-text("Create Policy")', timeout=3000):
            await asyncio.sleep(2)
            await screenshot(page, "06_timeoff_new_policy_flexible_step2")
            await page.go_back()
            await page.wait_for_load_state("networkidle")

        await safe_click_no_nav(
            page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
        )

    # "New Category" button (create_type)
    print("    Capturing New Category...")
    await page.goto(f"{BASE_URL}/settings/pto/", wait_until="networkidle")
    await asyncio.sleep(1)
    await scroll_to(page, "bottom")

    if await safe_click_no_nav(page, 'button:has-text("New Category")', timeout=3000):
        await asyncio.sleep(1.5)
        await screenshot(page, "06_timeoff_new_category_modal")
        await safe_click_no_nav(
            page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
        )


async def capture_employee_timeoff_actions(page: Page):
    """Employee Time Off tab interactions. Tools: get_balances, estimate_future_balances, update_balance"""
    print("\n[PAGE] Employee Time Off Actions...")

    # Go to My Info > Time Off (we know id=8 is Joyce Lu / ourselves)
    await page.goto(f"{BASE_URL}/employees/pto/?id=8", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "07_emp_timeoff_tab")
    await scroll_to(page, "bottom")
    await screenshot(page, "07_emp_timeoff_tab_bottom")

    # Balance card action buttons - they have small icon buttons below each card
    # Try to find the gear/settings dropdown on the first balance card
    gear_btns = page.locator(
        '[aria-label*="settings"], [aria-label*="Settings"], [data-bi-id*="settings"]'
    )
    gear_count = await gear_btns.count()
    print(f"    Found {gear_count} gear/settings buttons")

    # Try the settings dropdown on the balance card area
    if await safe_click_no_nav(page, '[aria-label="Time Off Overview Dropdown"]', timeout=2000):
        await screenshot(page, "07_emp_timeoff_settings_dropdown")
        await safe_click_no_nav(page, "body", timeout=500)  # dismiss

    # Try to click each small action icon on the balance card
    # From the screenshot: 4 icons per card: request, calendar, adjust(+), gear
    # The "+" button is for adjust balance (update_balance)
    # Try finding it by position or aria-label
    action_icons = page.locator(".fabric-1i5heln-IconButton-root")
    icon_count = await action_icons.count()
    print(f"    Found {icon_count} icon buttons on Time Off page")

    # Try clicking an icon that opens the "Adjust Potential Balance" modal
    for i in range(min(icon_count, 15)):
        try:
            icon = action_icons.nth(i)
            label = await icon.get_attribute("aria-label") or ""
            title = await icon.get_attribute("title") or ""
            text = await icon.text_content() or ""
            if any(kw in f"{label} {title} {text}".lower() for kw in ["adjust", "edit", "balance"]):
                await icon.click()
                await asyncio.sleep(1.5)
                await screenshot(page, "07_emp_timeoff_adjust_balance_modal")
                # Capture the full modal
                await page.evaluate("""
                    const dialog = document.querySelector('[role="dialog"]');
                    if (dialog) dialog.scrollTop = dialog.scrollHeight;
                """)
                await asyncio.sleep(0.5)
                await screenshot(page, "07_emp_timeoff_adjust_balance_modal_bottom")
                await safe_click_no_nav(
                    page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
                )
                break
        except Exception:
            continue

    # Try to find and click calculator (estimate_future_balances)
    for i in range(min(icon_count, 15)):
        try:
            icon = action_icons.nth(i)
            label = await icon.get_attribute("aria-label") or ""
            title = await icon.get_attribute("title") or ""
            if any(kw in f"{label} {title}".lower() for kw in ["calculator", "estimate", "future"]):
                await icon.click()
                await asyncio.sleep(1.5)
                await screenshot(page, "07_emp_timeoff_calculator_modal")
                await safe_click_no_nav(
                    page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
                )
                break
        except Exception:
            continue

    # Also visit another employee's time off tab to show populated data
    print("    Capturing another employee's Time Off...")
    for eid in [114, 110, 105, 100, 50, 20, 10]:
        try:
            resp = await page.goto(
                f"{BASE_URL}/employees/pto/?id={eid}", wait_until="domcontentloaded", timeout=15000
            )
            await asyncio.sleep(2)
            if resp and resp.status != 404 and "404" not in (await page.title()):
                await screenshot(page, f"07_emp_timeoff_employee_{eid}")
                break
        except Exception:
            continue


async def capture_request_timeoff(page: Page):
    """Request Time Off form. Tools: create_request, get_types, get_balances"""
    print("\n[PAGE] Request Time Off...")
    await page.goto(f"{BASE_URL}/app/time_off/requests/create", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "08_request_timeoff_form")
    await scroll_to(page, "bottom")
    await screenshot(page, "08_request_timeoff_form_bottom")


async def capture_reports(page: Page):
    """Reports. Tools: run_company_report, run_custom_report, get_custom_reports, get_custom_report, datasets.*"""
    print("\n[PAGE] Reports...")
    await page.goto(f"{BASE_URL}/app/reports/", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "09_reports_main")
    await scroll_to(page, "bottom")
    await screenshot(page, "09_reports_bottom")

    # Click first report
    first_report = page.locator('table a, .fab-List a, [class*="report"] a').first
    try:
        await first_report.wait_for(state="visible", timeout=5000)
        await first_report.click()
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(2)
        await screenshot(page, "09_reports_detail")
        await scroll_to(page, "bottom")
        await screenshot(page, "09_reports_detail_bottom")
        await page.go_back()
    except Exception:
        print("    [WARN] No report links found")


async def capture_files(page: Page):
    """Files page."""
    print("\n[PAGE] Files...")
    await page.goto(f"{BASE_URL}/files/", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "10_files_main")


async def capture_hiring(page: Page):
    """Hiring page."""
    print("\n[PAGE] Hiring...")
    await page.goto(f"{BASE_URL}/hiring/jobs", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "11_hiring_main")


async def capture_settings_other(page: Page):
    """Other settings pages for field/metadata tools. Tools: get_countries, get_states, get_users, get_field_options, update_field_options"""
    print("\n[PAGE] Settings - Additional pages...")

    # Access Levels (get_users)
    await page.goto(f"{BASE_URL}/app/settings/access_levels/all", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "12_settings_access_levels")

    # Custom Fields & Tables (get_field_options, update_field_options)
    await page.goto(f"{BASE_URL}/app/settings/custom_fields", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "12_settings_custom_fields")

    # Employee Fields - Department list (get_list_fields example)
    await page.goto(f"{BASE_URL}/settings/list.php?id=3517", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "12_settings_field_list_department")

    # Employee Fields - Job Title list
    await page.goto(f"{BASE_URL}/settings/list.php?id=3515", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "12_settings_field_list_jobtitle")

    # Holidays (related to time off)
    await page.goto(f"{BASE_URL}/app/settings/holidays", wait_until="networkidle")
    await asyncio.sleep(2)
    await screenshot(page, "12_settings_holidays")


# =====================================================================
# WORKFLOW CAPTURES - all PDF steps
# =====================================================================


async def workflow_create_employee(page: Page):
    """
    PDF Page 1: Create Employee and Update.
    Tools: bamboo_employees_create, bamboo_employees_update
    """
    print("\n[WORKFLOW] Create & Update Employee (PDF p1)...")
    steps = []

    # Step 1: Go to New Employee form
    await page.goto(f"{BASE_URL}/employees/new.php", wait_until="networkidle")
    await asyncio.sleep(2)
    steps.append({"step": 1, "action": "Navigate to New Employee form"})
    await screenshot(page, "wf1_01_new_employee_form")
    await scroll_to(page, "bottom")
    await screenshot(page, "wf1_01_new_employee_form_bottom")

    # Step 2: Try clicking Allow Access to BambooHR
    for sel in [
        'label:has-text("Allow Access")',
        'text="Allow Access"',
        'input[id*="selfService"]',
    ]:
        if await safe_click_no_nav(page, sel, timeout=2000):
            await asyncio.sleep(1)
            steps.append({"step": 2, "action": "Click Allow Access to BambooHR"})
            await screenshot(page, "wf1_02_allow_access_enabled")
            await scroll_to(page, "bottom")
            await screenshot(page, "wf1_02_allow_access_bottom")
            break

    # Step 3: Show Save button (bamboo_employees_create) - don't click it
    await scroll_to(page, "bottom")
    await asyncio.sleep(0.3)
    await screenshot(page, "wf1_03_save_button_visible")
    steps.append({"step": 3, "action": "Save button = bamboo_employees_create"})

    # Step 4: Go to an existing employee profile to show edit workflow
    await page.goto(f"{BASE_URL}/employees/employee.php?id=8&page=2097", wait_until="networkidle")
    await asyncio.sleep(2)
    steps.append({"step": 4, "action": "Navigate to employee profile Personal tab"})
    await screenshot(page, "wf1_04_employee_profile_personal")
    await scroll_to(page, "bottom")
    await screenshot(page, "wf1_04_employee_profile_personal_bottom")

    # Step 5: Try to click an editable field to show edit mode
    # BambooHR uses inline editing - click on a field value to edit
    edit_triggers = page.locator('[class*="editable"], [class*="Editable"], [data-bi-id*="edit"]')
    edit_count = await edit_triggers.count()
    print(f"    Found {edit_count} editable elements")
    if edit_count > 0:
        try:
            await edit_triggers.first.click()
            await asyncio.sleep(1)
            steps.append({"step": 5, "action": "Click editable field"})
            await screenshot(page, "wf1_05_field_edit_mode")
        except Exception:
            pass

    # Save changes button = bamboo_employees_update
    steps.append({"step": 6, "action": "Save changes button = bamboo_employees_update"})

    with open(WORKFLOWS_DIR / "wf1_create_update_employee.json", "w") as f:
        json.dump({"name": "Create & Update Employee", "pdf_page": 1, "steps": steps}, f, indent=2)


async def workflow_timeoff_settings(page: Page):
    """
    PDF Page 2: Time Off Settings - Create Type & Policy.
    Tools: get_policies, get_types, create_type, create_policy
    """
    print("\n[WORKFLOW] Time Off Settings (PDF p2)...")
    steps = []

    # Step 1: Settings page
    await page.goto(f"{BASE_URL}/settings/", wait_until="networkidle")
    await asyncio.sleep(2)
    steps.append({"step": 1, "action": "Go to Settings page"})
    await screenshot(page, "wf2_01_settings")

    # Step 2: Click Time Off tab
    await safe_click(page, '[data-bi-id="settings-TimeOff-link"]', timeout=5000)
    await asyncio.sleep(2)
    steps.append({"step": 2, "action": "Click Time Off tab (get_policies, get_types)"})
    await screenshot(page, "wf2_02_timeoff_tab")
    await scroll_to(page, "bottom")
    await screenshot(page, "wf2_02_timeoff_tab_bottom")

    # Step 3: Click New Category (create_type)
    await scroll_to(page, "bottom")
    if await safe_click_no_nav(page, 'button:has-text("New Category")', timeout=3000):
        await asyncio.sleep(1.5)
        steps.append({"step": 3, "action": "Click New Category (create_type)"})
        await screenshot(page, "wf2_03_new_category_modal")
        await safe_click_no_nav(
            page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
        )
        await asyncio.sleep(0.5)

    # Step 4: Click Add Policy / New Policy (create_policy wizard)
    await scroll_to(page, "top")
    if await safe_click_no_nav(
        page,
        '[data-bi-id="settings-TimeOff-new-policy-button"], button:has-text("New Policy")',
        timeout=3000,
    ):
        await asyncio.sleep(1.5)
        steps.append({"step": 4, "action": "Click New Policy (create_policy wizard)"})
        await screenshot(page, "wf2_04_new_policy_wizard")

        # Step 5: Select traditional
        if await safe_click_no_nav(page, 'text="It accrues time"', timeout=3000):
            await asyncio.sleep(0.5)
            steps.append({"step": 5, "action": "Select traditional policy type"})
            await screenshot(page, "wf2_05_traditional_selected")

        # Step 6: Click Create Policy
        if await safe_click(page, 'button:has-text("Create Policy")', timeout=3000):
            await asyncio.sleep(2)
            steps.append({"step": 6, "action": "Create Policy - name and typeId inputs"})
            await screenshot(page, "wf2_06_policy_name_form")

            # Scroll to see all fields
            await scroll_to(page, "bottom")
            steps.append({"step": 7, "action": "All remaining optional inputs"})
            await screenshot(page, "wf2_07_policy_all_inputs")

            # Navigate to Previous Step or away
            await page.go_back()
            await page.wait_for_load_state("networkidle")
        else:
            await safe_click_no_nav(
                page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
            )

    # Step 8: Click a policy on left bar to view it
    await page.goto(f"{BASE_URL}/settings/pto/", wait_until="networkidle")
    await asyncio.sleep(1)
    if await safe_click_no_nav(page, 'a:has-text("Salary Employees")', timeout=3000):
        await asyncio.sleep(1.5)
        steps.append({"step": 8, "action": "Click policy on left bar to view"})
        await screenshot(page, "wf2_08_policy_detail_view")
        await scroll_to(page, "bottom")
        await screenshot(page, "wf2_08_policy_detail_bottom")

    with open(WORKFLOWS_DIR / "wf2_timeoff_settings.json", "w") as f:
        json.dump({"name": "Time Off Settings", "pdf_page": 2, "steps": steps}, f, indent=2)


async def workflow_assign_policy(page: Page):
    """
    PDF Page 3: Assign Policy & View Balances.
    Tools: assign_policy, get_employee_policies, estimate_future_balances, get_balances
    """
    print("\n[WORKFLOW] Assign Policy & Balances (PDF p3)...")
    steps = []

    # Step 1: Settings > Time Off, click a policy
    await page.goto(f"{BASE_URL}/settings/pto/", wait_until="networkidle")
    await asyncio.sleep(2)
    steps.append({"step": 1, "action": "Navigate to Settings > Time Off"})

    # Click on Salary Employees policy (it has employees assigned)
    if await safe_click_no_nav(page, 'a:has-text("Salary Employees")', timeout=3000):
        await asyncio.sleep(1.5)
        steps.append({"step": 2, "action": "Click Salary Employees policy"})
        await screenshot(page, "wf3_01_policy_selected")

        # Look for the assign/manage employees area
        # The "Add Employees to Policy" link or employees list
        manage_selectors = [
            'button:has-text("Add Employees")',
            'a:has-text("Add Employees")',
            'button:has-text("Manage")',
            'button:has-text("Assign")',
            'a:has-text("Assign")',
        ]
        for sel in manage_selectors:
            if await safe_click_no_nav(page, sel, timeout=2000):
                await asyncio.sleep(1.5)
                steps.append({"step": 3, "action": "Open assign employees modal (assign_policy)"})
                await screenshot(page, "wf3_02_assign_modal")
                # Scroll modal to see all inputs
                await page.evaluate("""
                    const dialog = document.querySelector('[role="dialog"], [class*="modal"]');
                    if (dialog) dialog.scrollTop = dialog.scrollHeight;
                """)
                await asyncio.sleep(0.5)
                await screenshot(page, "wf3_02_assign_modal_scrolled")
                # Close
                await safe_click_no_nav(
                    page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
                )
                break

    # Step 4: Employee Time Off tab (get_employee_policies)
    await page.goto(f"{BASE_URL}/employees/pto/?id=8", wait_until="networkidle")
    await asyncio.sleep(2)
    steps.append({"step": 4, "action": "Employee Time Off tab (get_employee_policies)"})
    await screenshot(page, "wf3_03_employee_timeoff")
    await scroll_to(page, "bottom")
    await screenshot(page, "wf3_03_employee_timeoff_bottom")

    # Step 5: Click calculator icon (estimate_future_balances)
    # Scan icon buttons for calculator
    icon_btns = page.locator('button.fabric-1i5heln-IconButton-root, button[class*="IconButton"]')
    count = await icon_btns.count()
    for i in range(count):
        try:
            btn = icon_btns.nth(i)
            label = await btn.get_attribute("aria-label") or ""
            title = await btn.get_attribute("title") or ""
            if any(
                kw in f"{label} {title}".lower()
                for kw in ["calculator", "estimate", "future", "project"]
            ):
                await btn.click()
                await asyncio.sleep(1.5)
                steps.append({"step": 5, "action": "Click calculator (estimate_future_balances)"})
                await screenshot(page, "wf3_04_calculator_modal")
                await safe_click_no_nav(
                    page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
                )
                break
        except Exception:
            continue

    # Step 6: Click settings/gear icon (get_balances detail)
    # There's a gear dropdown on the Time Off header
    if await safe_click_no_nav(
        page,
        '[data-bi-id*="settings-TimeOff-dropdown"], [aria-label="Time Off Overview Dropdown"]',
        timeout=3000,
    ):
        await asyncio.sleep(1)
        steps.append({"step": 6, "action": "Click settings gear (get_balances)"})
        await screenshot(page, "wf3_05_timeoff_settings_dropdown")
        await safe_click_no_nav(page, "body", timeout=500)

    with open(WORKFLOWS_DIR / "wf3_assign_policy_balances.json", "w") as f:
        json.dump(
            {"name": "Assign Policy & View Balances", "pdf_page": 3, "steps": steps}, f, indent=2
        )


async def workflow_adjust_balance(page: Page):
    """
    PDF Page 4: Adjust Balance.
    Tools: update_balance, get_balances
    """
    print("\n[WORKFLOW] Adjust Balance (PDF p4)...")
    steps = []

    # Go to employee Time Off tab
    await page.goto(f"{BASE_URL}/employees/pto/?id=8", wait_until="networkidle")
    await asyncio.sleep(2)
    steps.append({"step": 1, "action": "Navigate to employee Time Off tab"})
    await screenshot(page, "wf4_01_timeoff_tab")

    # Find the edit/adjust button on a balance card
    # These are small icon buttons in the balance card area
    # Try clicking each icon button to find the one that opens "Adjust Potential Balance"
    icon_btns = page.locator('button.fabric-1i5heln-IconButton-root, button[class*="IconButton"]')
    count = await icon_btns.count()
    found_adjust = False

    for i in range(count):
        try:
            btn = icon_btns.nth(i)
            # Check if button is in the balance card area (y between 400-700)
            box = await btn.bounding_box()
            if not box or box["y"] < 350 or box["y"] > 800:
                continue

            await btn.click()
            await asyncio.sleep(1)

            # Check if "Adjust" dialog appeared
            dialog = page.locator(
                '[role="dialog"]:has-text("Adjust"), [role="dialog"]:has-text("Balance")'
            )
            if await dialog.count() > 0:
                steps.append({"step": 2, "action": "Click edit button on balance card"})
                await screenshot(page, "wf4_02_adjust_balance_modal")
                # Scroll modal
                await page.evaluate("""
                    const dialog = document.querySelector('[role="dialog"]');
                    if (dialog) dialog.scrollTop = dialog.scrollHeight;
                """)
                await asyncio.sleep(0.5)
                await screenshot(page, "wf4_02_adjust_balance_modal_bottom")
                steps.append({"step": 3, "action": "Adjust Potential Balance form with summary"})
                # Close
                await safe_click_no_nav(
                    page, 'button:has-text("Cancel"), button[aria-label="Close"]', timeout=2000
                )
                found_adjust = True
                break
            else:
                # Check for any dropdown/popover that appeared
                popover = page.locator('[role="menu"], [class*="popover"], [class*="dropdown"]')
                if await popover.count() > 0:
                    await screenshot(page, f"wf4_balance_menu_{i}")
                # Dismiss whatever opened
                await safe_click_no_nav(page, "body", timeout=300)
        except Exception:
            continue

    if not found_adjust:
        print("    [WARN] Could not find Adjust Balance modal")

    with open(WORKFLOWS_DIR / "wf4_adjust_balance.json", "w") as f:
        json.dump({"name": "Adjust Balance", "pdf_page": 4, "steps": steps}, f, indent=2)


async def workflow_request_timeoff(page: Page):
    """
    PDF Page 5: Homepage & Request Time Off.
    Tools: get_balances, get_whos_out, get_types, create_request
    """
    print("\n[WORKFLOW] Request Time Off (PDF p5)...")
    steps = []

    # Step 1: Homepage with updated time off
    await page.goto(f"{BASE_URL}/home", wait_until="networkidle")
    await asyncio.sleep(2)
    steps.append({"step": 1, "action": "Homepage with Time Off card (get_balances)"})
    await screenshot(page, "wf5_01_homepage")

    # Step 2: Who's Out section
    await scroll_to(page, "middle")
    await asyncio.sleep(1)
    steps.append({"step": 2, "action": "Who's Out section (get_whos_out)"})
    await screenshot(page, "wf5_02_whos_out_section")

    # Step 3: Full calendar
    if await safe_click(
        page, 'a:has-text("full calendar"), a:has-text("Full Calendar")', timeout=3000
    ):
        await asyncio.sleep(2)
        steps.append({"step": 3, "action": "Full calendar view (get_whos_out)"})
        await screenshot(page, "wf5_03_full_calendar")
        await page.go_back()
        await page.wait_for_load_state("networkidle")

    # Step 4: Click Request Time Off
    await page.goto(f"{BASE_URL}/home", wait_until="networkidle")
    await asyncio.sleep(1)
    if await safe_click(
        page, 'button:has-text("Request Time Off"), a:has-text("Request Time Off")', timeout=5000
    ):
        await asyncio.sleep(2)
        steps.append({"step": 4, "action": "Request Time Off form (get_types, get_balances)"})
        await screenshot(page, "wf5_04_request_form")
        await scroll_to(page, "bottom")
        await screenshot(page, "wf5_04_request_form_bottom")

        # Show the Send Request button (create_request)
        steps.append({"step": 5, "action": "Send Request button = bamboo_time_off_create_request"})

    with open(WORKFLOWS_DIR / "wf5_request_timeoff.json", "w") as f:
        json.dump({"name": "Request Time Off", "pdf_page": 5, "steps": steps}, f, indent=2)


# =====================================================================
# MAIN
# =====================================================================


async def main():
    if not EMAIL or not PASSWORD:
        print("ERROR: Set BAMBOO_EMAIL and BAMBOO_PASSWORD environment variables.")
        print(
            "  Example: BAMBOO_EMAIL=you@example.com BAMBOO_PASSWORD=secret uv run python scripts/capture_bamboohr.py"
        )
        return

    ensure_dirs()
    start_time = time.time()

    print("=" * 60)
    print("  BambooHR UI Capture Script - Full Coverage")
    print(f"  Target: {BASE_URL}")
    print(f"  Output: {OUTPUT_DIR.resolve()}")
    print(f"  Started: {datetime.now().isoformat()}")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=SLOW_MO)
        context = await browser.new_context(
            viewport=VIEWPORT,
            record_har_path=str(HAR_DIR / "bamboohr.har"),
            record_har_url_filter="**/bamboohr.com/**",
        )
        page = await context.new_page()
        page.set_default_navigation_timeout(60000)  # 60s for page navigations
        page.set_default_timeout(15000)  # 15s for element operations

        try:
            await login(page)

            # -- Page Captures (all tools) --
            capture_fns = [
                ("Home Dashboard", capture_home),
                ("My Info", capture_my_info),
                ("People List", capture_people_list),
                ("Employee Profile", capture_employee_profile),
                ("New Employee Form", capture_new_employee),
                ("Settings Main", capture_settings_main),
                ("Settings Time Off", capture_settings_timeoff),
                ("Employee Time Off Actions", capture_employee_timeoff_actions),
                ("Request Time Off", capture_request_timeoff),
                ("Reports", capture_reports),
                ("Files", capture_files),
                ("Hiring", capture_hiring),
                ("Settings Other", capture_settings_other),
            ]
            for name, fn in capture_fns:
                try:
                    await fn(page)
                except Exception as e:
                    print(f"\n[ERROR] {name} failed: {e}")
                    await screenshot(page, f"ERROR_{name.lower().replace(' ', '_')}")

            # -- Workflow Captures (all PDF pages) --
            workflow_fns = [
                ("Workflow: Create Employee (PDF p1)", workflow_create_employee),
                ("Workflow: Time Off Settings (PDF p2)", workflow_timeoff_settings),
                ("Workflow: Assign Policy (PDF p3)", workflow_assign_policy),
                ("Workflow: Adjust Balance (PDF p4)", workflow_adjust_balance),
                ("Workflow: Request Time Off (PDF p5)", workflow_request_timeoff),
            ]
            for name, fn in workflow_fns:
                try:
                    await fn(page)
                except Exception as e:
                    print(f"\n[ERROR] {name} failed: {e}")
                    await screenshot(page, f"ERROR_{name.split(':')[0].lower().replace(' ', '_')}")

        except Exception as e:
            print(f"\n[ERROR] Fatal: {e}")
            await screenshot(page, "ERROR_final_state")
            raise
        finally:
            await context.close()
            await browser.close()

    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print("  [OK] Capture complete!")
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"  Screenshots: {SCREENSHOTS_DIR.resolve()}")
    print(f"  Elements: {ELEMENTS_DIR.resolve()}")
    print(f"  Workflows: {WORKFLOWS_DIR.resolve()}")
    print(f"  HAR: {HAR_DIR.resolve()}")
    print("=" * 60)

    screenshots = list(SCREENSHOTS_DIR.glob("*.png"))
    elements = list(ELEMENTS_DIR.glob("*.json"))
    print("\n  [SUMMARY]")
    print(f"     Screenshots: {len(screenshots)}")
    print(f"     Element files: {len(elements)}")

    all_elements = {"pages": []}
    for ef in sorted(elements):
        with open(ef) as f:
            data = json.load(f)
            all_elements["pages"].append(
                {
                    "name": ef.stem,
                    "url": data.get("meta", {}).get("url", ""),
                    "buttons": len(data.get("buttons", [])),
                    "links": len(data.get("links", [])),
                    "inputs": len(data.get("inputs", [])),
                    "selects": len(data.get("selects", [])),
                    "forms": len(data.get("forms", [])),
                    "tables": len(data.get("tables", [])),
                    "tabs": len(data.get("tabs", [])),
                }
            )

    with open(OUTPUT_DIR / "capture_summary.json", "w") as f:
        json.dump(all_elements, f, indent=2)
    print(f"     Summary: {OUTPUT_DIR / 'capture_summary.json'}")


if __name__ == "__main__":
    asyncio.run(main())
