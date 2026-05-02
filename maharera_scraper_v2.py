"""
MahaRERA Mumbai Project Scraper — v2
Inspired by ramSeraph/opendata patterns:
  - Incremental: only scrapes new/changed records
  - Resumable: saves state after every page, safe to Ctrl+C and restart
  - GitHub Actions ready: exits with code 0 if nothing new, 1 if new data found

Usage:
    pip install requests beautifulsoup4 pandas
    python maharera_scraper_v2.py

Re-run anytime — it will skip already-scraped records automatically.
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import json
import time
import random
import logging
import os
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL      = "https://maharera.maharashtra.gov.in"
SEARCH_URL    = f"{BASE_URL}/projects-search-result"
DATA_FILE     = "maharera_mumbai.csv"          # main output — appended incrementally
STATE_FILE    = "scraper_state.json"           # tracks last scrape per district
RAW_HTML_FILE = "last_response.html"           # debug dump if parsing fails

DISTRICTS = {
    "Mumbai City":     "519",
    "Mumbai Suburban": "518",
}

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         BASE_URL,
}

DELAY = 2  # seconds between requests — do not lower

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── State management (ramSeraph pattern: track what you have) ─────────────────

def load_state() -> dict:
    """Load previous scrape state — which pages we already have per district."""
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_existing_rera_numbers() -> set:
    """Load RERA numbers we already have so we can skip duplicates."""
    if Path(DATA_FILE).exists():
        df = pd.read_csv(DATA_FILE, usecols=["rera_number"], dtype=str)
        return set(df["rera_number"].dropna().tolist())
    return set()

def append_to_csv(records: list[dict]):
    """Append new records to CSV — creates file with header if it doesn't exist."""
    if not records:
        return
    df = pd.DataFrame(records)
    write_header = not Path(DATA_FILE).exists()
    df.to_csv(DATA_FILE, mode="a", header=write_header, index=False, encoding="utf-8-sig")
    log.info(f"  → Appended {len(records)} records to {DATA_FILE}")

# ── Session ───────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    log.info("Initialising session...")
    resp = session.get(BASE_URL, timeout=30)
    resp.raise_for_status()
    log.info(f"Session ready. Cookies: {list(session.cookies.keys())}")
    return session

def get_form_token(session: requests.Session) -> str:
    """Extract Drupal form_build_id from the search page."""
    resp = session.get(f"{BASE_URL}/project-search", timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    for name in ["form_build_id", "__RequestVerificationToken", "_token"]:
        el = soup.find("input", {"name": name})
        if el:
            val = el.get("value", "")
            log.info(f"Token ({name}): {val[:40]}...")
            return val
    log.warning("No form token found — proceeding without")
    return ""

# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_projects(html: str, district_name: str) -> list[dict]:
    """
    Parse project listing HTML. 
    MahaRERA renders results as repeated blocks — we try multiple selectors.
    If all fail, the raw HTML is dumped to last_response.html for debugging.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try each selector pattern — government sites change layout without notice
    rows = (
        soup.select("div.views-row") or
        soup.select("div.view-content > div") or
        soup.select("table.views-table tbody tr") or
        soup.select("div.search-result-item") or
        soup.select("article")
    )

    if not rows:
        log.warning("No rows matched — dumping HTML to last_response.html")
        Path(RAW_HTML_FILE).write_text(html, encoding="utf-8")
        log.warning("Open last_response.html, inspect the structure, then report back")
        return []

    results = []
    for row in rows:
        def text(selectors: list[str]) -> str:
            for sel in selectors:
                el = row.select_one(sel)
                if el:
                    return el.get_text(strip=True)
            return ""

        # Multiple selector fallbacks per field — handles layout variations
        project = {
            "rera_number":   text([".rera-number", ".field--name-field-rera-no",
                                   "td:nth-child(1)", ".views-field-field-rera-no"]),
            "project_name":  text([".project-name", ".field--name-title",
                                   "td:nth-child(2)", ".views-field-title", "h3", "h2"]),
            "promoter":      text([".promoter", ".field--name-field-promoter-name",
                                   "td:nth-child(3)", ".views-field-field-promoter-name"]),
            "district":      text([".district", ".field--name-field-district",
                                   "td:nth-child(4)"]) or district_name,
            "taluka":        text([".taluka", ".field--name-field-taluka", "td:nth-child(5)"]),
            "pincode":       text([".pincode", ".field--name-field-pincode", "td:nth-child(6)"]),
            "last_modified": text([".last-modified", ".views-field-changed", "td:nth-child(7)"]),
            "scraped_at":    datetime.today().strftime("%Y-%m-%d"),
            "detail_url":    "",
        }

        # Grab detail page URL
        link = row.select_one("a[href*='project'], a[href*='view-details'], a[href*='details']")
        if link:
            href = link.get("href", "")
            project["detail_url"] = href if href.startswith("http") else BASE_URL + href

        if project["rera_number"]:
            results.append(project)

    return results


def get_total_pages(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")

    # Try to read result count from banner text first
    for tag in soup.find_all(string=True):
        if "Result" in tag and any(c.isdigit() for c in tag):
            log.info(f"Result banner: {tag.strip()}")
            break

    # Drupal pager — last page link
    last = soup.select_one("li.pager__item--last a, li.pager-last a")
    if last:
        href = last.get("href", "")
        if "page=" in href:
            try:
                return int(href.split("page=")[-1]) + 1
            except ValueError:
                pass

    # Count numbered pager items
    page_nums = []
    for a in soup.select("li.pager__item a, ul.pager li a"):
        try:
            page_nums.append(int(a.get_text(strip=True)))
        except ValueError:
            pass
    if page_nums:
        return max(page_nums)

    return 1

# ── Core scrape loop ──────────────────────────────────────────────────────────

def scrape_district(
    session: requests.Session,
    district_name: str,
    district_code: str,
    existing_rera: set,
    state: dict,
) -> tuple[int, int]:
    """
    Scrape one district. Returns (new_count, skipped_count).
    Resumes from last completed page if interrupted previously.
    """
    log.info(f"\n{'='*55}")
    log.info(f"District: {district_name} (code={district_code})")
    log.info(f"{'='*55}")

    token = get_form_token(session)
    payload = {
        "project_registration_type": "registered",
        "state_code":                "27",
        "district_code":             district_code,
        "project_name":              "",
        "rera_no":                   "",
        "pincode":                   "",
        "completion_date":           "",
        "form_id":                   "custom_search_form",
        "op":                        "Search",
    }
    if token:
        payload["form_build_id"] = token

    # Resume from where we left off
    start_page = state.get(district_code, {}).get("last_page", 0)
    if start_page > 0:
        log.info(f"Resuming from page {start_page + 1} (previous run got to page {start_page})")

    # Fetch page 0 to get total pages (always needed even on resume)
    log.info("Fetching page 1 to detect total pages...")
    resp = session.post(SEARCH_URL, data=payload, timeout=30)
    resp.raise_for_status()

    total_pages = get_total_pages(resp.text)
    log.info(f"Total pages: {total_pages}")

    new_count = 0
    skipped_count = 0

    # Process page 0 only if not resumed past it
    if start_page == 0:
        projects = parse_projects(resp.text, district_name)
        new, skipped = _process_page(projects, existing_rera)
        new_count += new
        skipped_count += skipped
        state[district_code] = {"last_page": 0, "total_pages": total_pages}
        save_state(state)

    # Remaining pages
    for page_num in range(max(1, start_page), total_pages):
        time.sleep(DELAY + random.uniform(0, 0.8))

        url = f"{SEARCH_URL}?page={page_num}"
        log.info(f"Page {page_num + 1}/{total_pages}")

        try:
            resp = session.post(url, data=payload, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error(f"Request failed: {e} — will retry next run from this page")
            state[district_code]["last_page"] = page_num - 1
            save_state(state)
            break

        projects = parse_projects(resp.text, district_name)

        if not projects:
            log.warning(f"Page {page_num + 1} returned 0 projects — stopping")
            break

        new, skipped = _process_page(projects, existing_rera)
        new_count += new
        skipped_count += skipped

        # Save progress after every page — safe to interrupt anytime
        state[district_code]["last_page"] = page_num
        save_state(state)

    # Mark district complete
    state[district_code]["completed"] = True
    state[district_code]["completed_at"] = datetime.today().isoformat()
    save_state(state)

    log.info(f"District done: {new_count} new, {skipped_count} already had")
    return new_count, skipped_count


def _process_page(projects: list[dict], existing_rera: set) -> tuple[int, int]:
    """Filter out duplicates, append new records. Returns (new, skipped)."""
    new = [p for p in projects if p["rera_number"] not in existing_rera]
    skipped = len(projects) - len(new)

    if new:
        append_to_csv(new)
        for p in new:
            existing_rera.add(p["rera_number"])  # update in-memory set

    return len(new), skipped

# ── Entry point ───────────────────────────────────────────────────────────────

def run():
    state = load_state()
    existing_rera = load_existing_rera_numbers()
    log.info(f"Already have {len(existing_rera)} RERA numbers on disk")

    session = make_session()
    total_new = 0

    for district_name, district_code in DISTRICTS.items():
        # Skip if already completed in a previous full run
        if state.get(district_code, {}).get("completed"):
            log.info(f"Skipping {district_name} — already completed. Delete {STATE_FILE} to re-scrape.")
            continue

        try:
            new, _ = scrape_district(session, district_name, district_code, existing_rera, state)
            total_new += new
        except Exception as e:
            log.error(f"District {district_name} crashed: {e}")
            continue

    log.info(f"\n✓ Done. {total_new} new records added to {DATA_FILE}")

    if total_new == 0:
        log.info("Nothing new found.")
        exit(0)   # GitHub Actions: exit 0 = no new data
    else:
        exit(1)   # GitHub Actions: exit 1 = new data found, trigger next step


if __name__ == "__main__":
    run()
