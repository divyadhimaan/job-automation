#!/usr/bin/env python3
"""
Find LinkedIn company IDs for use in config.yaml target_companies.

Usage — single company:
  python find_company_id.py "Zepto"

Usage — multiple companies at once:
  python find_company_id.py "Zepto" "Porter" "District"

Logs in once, then searches all companies in sequence.
"""
import sys
import re
import time


ID_PATTERNS = [
    r'"companyId"\s*:\s*"?(\d+)"?',
    r'urn:li:fsd_company:(\d+)',
    r'urn:li:company:(\d+)',
    r'"objectUrn"\s*:\s*"urn:li:company:(\d+)"',
    r'company/(\d+)/',
    r'"id"\s*:\s*(\d{5,})',
]


def extract_id(html: str) -> str | None:
    for pat in ID_PATTERNS:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def search_company(page, query: str):
    """Reuses a single page (and its session) to look up a company."""
    print(f"\n── {query} ──")

    page.goto(
        f"https://www.linkedin.com/search/results/companies/?keywords={query}",
        wait_until="domcontentloaded"
    )
    time.sleep(3)

    # Collect all slugs BEFORE navigating away (avoids stale element errors)
    results = page.query_selector_all("a[href*='/company/']")
    slugs = []
    seen = set()
    for el in results:
        try:
            href = el.get_attribute("href") or ""
            m = re.search(r"/company/([^/?]+)", href)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                slugs.append(m.group(1))
        except Exception:
            pass

    for slug in slugs[:4]:
        try:

            # Navigate the same page to the company profile
            page.goto(f"https://www.linkedin.com/company/{slug}/", wait_until="domcontentloaded")
            time.sleep(1)
            comp_id = extract_id(page.content())

            # Fallback: jobs sub-page
            if not comp_id:
                page.goto(f"https://www.linkedin.com/company/{slug}/jobs/", wait_until="domcontentloaded")
                time.sleep(1)
                comp_id = extract_id(page.content())
                if not comp_id:
                    m2 = re.search(r'f_C=(\d+)', page.url)
                    comp_id = m2.group(1) if m2 else None

            name_el = page.query_selector("h1")
            name = name_el.inner_text().strip() if name_el else slug

            status = comp_id if comp_id else "? (not found)"
            print(f"  {name:45s}  linkedin_id: {status:<12}  slug: {slug}")

        except Exception as e:
            print(f"  [skip {slug}] {e}")


def main():
    if len(sys.argv) < 2:
        print('Usage: python find_company_id.py "Company1" "Company2" ...')
        sys.exit(1)

    queries = sys.argv[1:]

    from playwright.sync_api import sync_playwright
    print(f"\nLooking up {len(queries)} compan{'y' if len(queries)==1 else 'ies'}: {', '.join(queries)}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=200)
        # One context = one shared session — all pages reuse the same cookies
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        input("\nLog in to LinkedIn in the browser, then press Enter here: ")

        for q in queries:
            try:
                search_company(page, q)
            except Exception as e:
                print(f"  [error] {q}: {e}")

        context.close()
        browser.close()

    print("\nDone. Copy the linkedin_id values into config.yaml.\n")


if __name__ == "__main__":
    main()
