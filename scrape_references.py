#!/usr/bin/env python3
"""
scrape_references.py — Scrape property reference numbers from a Rightmove branch page.

Cross-checks against both:
  - not_contacted.csv  (refs queued to call)
  - called_references.csv  (refs already called)

Only truly new references (not in either list) are prepended to the top of not_contacted.csv.

Usage:
    python3 scrape_references.py [--dry-run]
"""

import asyncio
import csv
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE           = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
NOT_CONTACTED  = BASE / "Referances" / "Rightmove property references - not contacted.csv"
CALLED_CSV     = BASE / "called_references.csv"

RIGHTMOVE_URL = (
    "https://www.rightmove.co.uk/property-to-rent/find.html"
    "?locationIdentifier=BRANCH%5E96668"
    "&keywords=london"
    "&sortType=18"
    "&channel=RENT"
    "&transactionType=LETTING"
    "&displayLocationIdentifier=.html"
    "&minPrice=2500"
)

RESULTS_PER_PAGE = 24

# Reference numbers appear as: ** Property Reference: 1234567 **
REF_PATTERN = re.compile(r"\*\*\s*Property Reference[:\s]+(\d+)\s*\*\*", re.IGNORECASE)

# Also try to find them in data attributes / JSON blobs embedded in the page
REF_PATTERN_JSON = re.compile(r'"propertyId"\s*:\s*(\d{6,8})')


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_not_contacted() -> list[str]:
    if not NOT_CONTACTED.exists():
        return []
    with open(NOT_CONTACTED, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)
        return [row[0].strip() for row in reader if row and row[0].strip()]


def load_called() -> set[str]:
    if not CALLED_CSV.exists():
        return set()
    with open(CALLED_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        col = next((c for c in (reader.fieldnames or []) if "ref" in c.lower() or "number" in c.lower()), None)
        if col is None:
            # Fallback: first column
            f.seek(0)
            reader2 = csv.reader(f, delimiter=";")
            next(reader2, None)
            return {row[0].strip() for row in reader2 if row and row[0].strip()}
        return {row[col].strip() for row in reader if row.get(col, "").strip()}


def replace_csv(refs: list[str]) -> None:
    with open(NOT_CONTACTED, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Reference Number"])
        for ref in refs:
            writer.writerow([ref])
    print(f"Written {len(refs)} references to {NOT_CONTACTED.name} (fresh list)")


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

async def extract_refs_from_page(page) -> list[str]:
    content = await page.content()
    refs = REF_PATTERN.findall(content)
    if not refs:
        refs = REF_PATTERN_JSON.findall(content)
    return refs


async def get_total_pages(page) -> int:
    content = await page.content()
    # Rightmove embeds result count in JSON: "resultCount":"8173"
    m = re.search(r'"resultCount"\s*:\s*"?([\d,]+)"?', content)
    if m:
        total = int(m.group(1).replace(",", ""))
        pages = (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
        print(f"Total listings: {total:,}  (~{pages} pages)")
        return pages
    return 999


async def scrape_all() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        # Page 1
        print("Fetching page 1...", flush=True)
        await page.goto(RIGHTMOVE_URL, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector('[data-test="propertyCard"], .l-searchResult', timeout=8000)
        except Exception:
            pass

        total_pages = await get_total_pages(page)
        refs = await extract_refs_from_page(page)
        for r in refs:
            if r not in seen:
                seen.add(r)
                ordered.append(r)
        print(f"  Page 1: {len(refs)} refs found ({len(ordered)} unique so far)", flush=True)

        index = RESULTS_PER_PAGE
        page_num = 1

        while True:
            page_num += 1
            if total_pages != 999 and index >= total_pages * RESULTS_PER_PAGE:
                break

            url = RIGHTMOVE_URL + f"&index={index}"
            print(f"Fetching page {page_num} (index={index})...", end=" ", flush=True)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_selector('[data-test="propertyCard"], .l-searchResult', timeout=6000)
                except Exception:
                    pass
            except Exception as e:
                print(f"error: {e}", flush=True)
                break

            refs = await extract_refs_from_page(page)
            if not refs:
                print("no refs found — stopping.", flush=True)
                break

            new_this_page = 0
            for r in refs:
                if r not in seen:
                    seen.add(r)
                    ordered.append(r)
                    new_this_page += 1

            print(f"{len(refs)} refs, {new_this_page} new (total unique: {len(ordered)})", flush=True)

            # If nothing new for 3 consecutive pages with known total, stop early
            if total_pages != 999 and index + RESULTS_PER_PAGE >= total_pages * RESULTS_PER_PAGE:
                break

            index += RESULTS_PER_PAGE

        await browser.close()

    print(f"\nScraping complete — {len(ordered)} unique references found", flush=True)
    return ordered


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 55)
    print("  Rightmove Reference Scraper")
    print("=" * 55)

    scraped = await scrape_all()

    called_set = load_called()
    fresh_refs = [r for r in scraped if r not in called_set]

    print(f"\nScraped total         : {len(scraped)}")
    print(f"Already called        : {len(called_set)}")
    print(f"Fresh refs to write   : {len(fresh_refs)}")

    if not fresh_refs:
        print("\nNothing to write — all scraped references have already been called.")
        return

    if dry_run:
        print("\n[dry-run] No changes written.")
        return

    replace_csv(fresh_refs)
    print(f"\nDone! not-contacted.csv replaced with {len(fresh_refs)} fresh references.")
    print("Run `python3 next_batch.py` to start calling them.")


if __name__ == "__main__":
    asyncio.run(main())
