"""
Sri Lanka Vehicle Service Centers Scraper v2
Fixes:
  - Replaced wait_until="networkidle" with "domcontentloaded" + explicit waits
  - Added retry logic for navigation
  - Handles consent/cookie banners more robustly
  - Better resilience against timeouts on individual place pages
"""

import asyncio
import csv
import re
import random
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── All 25 districts of Sri Lanka ──────────────────────────────────────────
DISTRICTS = [ #add districts comma separately here
    "Vavuniya"
]

OUTPUT_CSV   = "sri_lanka_vehicle_service_centers.csv"
HEADLESS     = False    # Set False to watch the browser
SCROLL_TIMES = 100     # Scrolls per district results panel
PAUSE_MIN    = 2.0
PAUSE_MAX    = 4.0

CSV_FIELDS = [ # CSV files columns
    "district", "name", "address", "phone",
    "website", "rating", "review_count",
    "category", "hours", "facilities", "maps_url",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def clean(text):
    if not text:
        return ""
    return " ".join(str(text).split()).strip()


async def safe_goto(page, url, retries=3):
    """Navigate with domcontentloaded (not networkidle) and retry on timeout."""
    for attempt in range(retries):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            # Give JS a moment to render
            await asyncio.sleep(2.5)
            return True
        except PWTimeout:
            print(f"    ⚠ Timeout on attempt {attempt+1} for {url[:80]}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return False


async def dismiss_consent(page):
    """Click through Google's consent/cookie banners."""
    selectors = [
        'button[aria-label*="Accept all"]',
        'button[aria-label*="Accept"]',
        'button[jsname="b3VHJd"]',
        '#L2AGLb',
        'form:has(button) button:first-child',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(1.5)
                return
        except Exception:
            pass


async def slow_scroll(page, selector, times=10):
    for _ in range(times):
        try:
            await page.evaluate(f"""
                const el = document.querySelector('{selector}');
                if (el) el.scrollBy(0, 700);
            """)
        except Exception:
            pass
        await asyncio.sleep(0.8)


async def safe_text(locator):
    try:
        return clean(await locator.first.inner_text(timeout=4000))
    except Exception:
        return ""


async def safe_attr(locator, attr):
    try:
        return (await locator.first.get_attribute(attr, timeout=4000)) or ""
    except Exception:
        return ""


async def extract_place_details(page):
    d = {}

    # Name
    d["name"] = await safe_text(page.locator('h1.DUwDvf, h1[class*="fontHeadlineLarge"]'))

    # Address
    d["address"] = await safe_text(page.locator('[data-item-id="address"] .fontBodyMedium'))

    # Phone — try multiple possible selectors
    for sel in [
        '[data-item-id^="phone:tel"] .fontBodyMedium',
        '[data-tooltip*="phone"] .fontBodyMedium',
        'button[data-item-id^="phone"] .fontBodyMedium',
        '[aria-label*="phone" i] .fontBodyMedium',
    ]:
        val = await safe_text(page.locator(sel))
        if val:
            d["phone"] = val
            break
    else:
        d["phone"] = ""

    # Website
    d["website"] = await safe_attr(page.locator('[data-item-id="authority"] a'), "href")

    # Rating
    d["rating"] = await safe_text(page.locator('div.F7nice span[aria-hidden="true"]'))

    # Review count
    try:
        raw = await safe_attr(
            page.locator('div.F7nice span[aria-label*="review"]'), "aria-label"
        )
        nums = re.findall(r"[\d,]+", raw)
        d["review_count"] = nums[0].replace(",", "") if nums else ""
    except Exception:
        d["review_count"] = ""

    # Category
    d["category"] = await safe_text(page.locator('button.DkEaL'))

    # Hours
    d["hours"] = await safe_text(page.locator('[data-item-id="oh"] .fontBodyMedium'))

    # Facilities — from About tab
    facilities = []
    try:
        about_btn = page.locator(
            'button[aria-label*="About"], [data-tab-index="1"] button, button:has-text("About")'
        ).first
        if await about_btn.count() and await about_btn.is_visible(timeout=3000):
            await about_btn.click()
            await asyncio.sleep(1.5)
            items = page.locator('li.hpLkke span, div.iP2t7d span, [class*="amenity"] span')
            cnt = await items.count()
            for i in range(min(cnt, 40)):
                txt = clean(await items.nth(i).inner_text())
                if txt and len(txt) < 100 and txt not in facilities:
                    facilities.append(txt)
    except Exception:
        pass

    d["facilities"] = " | ".join(facilities)
    d["maps_url"] = page.url
    return d


# ── District scraper ─────────────────────────────────────────────────────────

async def scrape_district(page, district, writer, seen, csvfile):
    query = f"vehicle+service+center+{district.replace(' ', '+')}+Sri+Lanka"  ## This is the query use to put on the google map search bar
    search_url = f"https://www.google.com/maps/search/{query}"

    print(f"\n[{district}] → {search_url}")

    ok = await safe_goto(page, search_url)
    if not ok:
        print(f"[{district}] ✗ Could not load search page — skipping")
        return

    await dismiss_consent(page)

    # Wait for results feed to appear
    try:
        await page.wait_for_selector('div[role="feed"]', timeout=15_000)
    except PWTimeout:
        print(f"[{district}] ✗ Results feed never appeared — skipping")
        return

    await slow_scroll(page, 'div[role="feed"]', SCROLL_TIMES)

    # Collect unique place URLs
    card_links = page.locator('a[href*="/maps/place/"]')
    count = await card_links.count()
    print(f"[{district}] Found {count} cards")

    hrefs = []
    for i in range(count):
        href = await safe_attr(card_links.nth(i), "href")
        if href and "/maps/place/" in href and href not in hrefs:
            hrefs.append(href)

    scraped = 0
    for href in hrefs:
        if href in seen:
            continue
        seen.add(href)

        ok = await safe_goto(page, href)
        if not ok:
            print(f"  ✗ Skipping (timeout): {href[:70]}")
            continue

        try:
            details = await extract_place_details(page)
            details["district"] = district

            if not details.get("name"):
                continue

            writer.writerow({f: details.get(f, "") for f in CSV_FIELDS})
            csvfile.flush()
            scraped += 1
            phone_display = details["phone"] or "—"
            addr_display  = details["address"][:55] or "—"
            print(f"  ✓ {details['name']} | {phone_display} | {addr_display}")

        except Exception as e:
            print(f"  ✗ Extract error: {e}")

        await asyncio.sleep(random.uniform(1.0, 2.2))

    print(f"[{district}] Done — {scraped} places saved")


# ── Entry point ──────────────────────────────────────────────────────────────

async def main():
    output_path = Path(OUTPUT_CSV)

    with output_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_FIELDS)
        writer.writeheader()
        seen_urls = set()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = await context.new_page()

            for district in DISTRICTS:
                await scrape_district(page, district, writer, seen_urls, csvfile)
                pause = random.uniform(PAUSE_MIN, PAUSE_MAX)
                print(f"  … pausing {pause:.1f}s")
                await asyncio.sleep(pause)

            await browser.close()

    total = sum(1 for _ in output_path.open(encoding="utf-8")) - 1
    print(f"\n✅ Complete! Saved {total} places → {output_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())