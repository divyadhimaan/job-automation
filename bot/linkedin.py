"""
LinkedIn automation bot using Playwright.

Flow:
  1. Login to LinkedIn
  2. Build search URL from config filters
  3. Paginate through job listings
  4. For each job:
     - If Easy Apply → attempt to auto-fill and submit
     - If external link → save as needs_manual with the career URL
  5. Record everything in SQLite
"""

import re
import time
import logging
from pathlib import Path
from urllib.parse import urlencode, quote_plus
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

from .database import (
    init_db, upsert_job, update_job_status, job_exists,
    start_run, finish_run,
)
from .utils import (
    load_config, setup_logger, human_delay,
    EXPERIENCE_MAP, JOB_TYPE_MAP, DATE_POSTED_MAP,
)

log = setup_logger("linkedin_bot")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

SESSION_FILE = Path(__file__).parent.parent / ".linkedin_session.json"


def _notify(title: str, message: str):
    """Send a macOS desktop notification (silent fail on other platforms)."""
    try:
        import subprocess
        subprocess.run([
            "osascript", "-e",
            f'display notification "{message}" with title "{title}" sound name "Glass"'
        ], check=False, capture_output=True)
    except Exception:
        pass


def run(config_path: str = None, config: dict = None, debug: bool = False):
    cfg = config if config is not None else load_config(config_path)
    log.setLevel(getattr(logging, cfg["settings"].get("log_level", "INFO")))
    init_db()

    run_id = start_run()
    counters = {"discovered": 0, "applied": 0, "external": 0, "failed": 0}
    errors = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=cfg["settings"].get("headless", False),
            slow_mo=cfg["settings"].get("slow_mo", 500),
        )

        # Load saved session if available (avoids repeated manual logins)
        ctx_kwargs = dict(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        if SESSION_FILE.exists():
            ctx_kwargs["storage_state"] = str(SESSION_FILE)
            log.info(f"Loaded saved session from {SESSION_FILE}")

        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        try:
            _login(page, cfg)
            # Save session after successful login so next run can skip it
            context.storage_state(path=str(SESSION_FILE))
            log.info(f"Session saved → {SESSION_FILE}")

            max_jobs   = cfg["search"].get("max_jobs", 100)
            company_ids = _get_company_ids(cfg)

            # Decide which passes to run
            passes = []
            if company_ids:
                passes.append(("target",  max_jobs // 2))   # half budget for target companies
            passes.append(("general", max_jobs - (max_jobs // 2 if company_ids else 0)))

            for pass_mode, pass_limit in passes:
                search_url = _build_search_url(cfg, mode=pass_mode)
                log.info(f"[{pass_mode.upper()} PASS] URL: {search_url}")

                job_count = 0
                page_num  = 0

                while job_count < pass_limit:
                    paginated_url = search_url + f"&start={page_num * 25}"
                    page.goto(paginated_url, wait_until="load", timeout=30000)
                    human_delay(3, 5)

                    if debug:
                        shot = f"debug_{pass_mode}_page{page_num+1}.png"
                        page.screenshot(path=shot, full_page=False)
                        log.info(f"[debug] Screenshot → {shot}")
                        _debug_dump_selectors(page)

                    job_cards = _get_job_cards(page)
                    if not job_cards:
                        log.info(f"[{pass_mode}] No more cards on page {page_num+1} — done.")
                        if debug:
                            with open(f"debug_{pass_mode}_page.html", "w") as f:
                                f.write(page.content())
                        break

                    log.info(f"[{pass_mode}] Page {page_num+1}: {len(job_cards)} cards")

                    for card in job_cards:
                        if job_count >= pass_limit:
                            break
                        try:
                            result = _process_job_card(page, card, cfg)
                            if result:
                                counters["discovered"] += 1
                                if result["status"] == "applied":
                                    counters["applied"] += 1
                                elif result["status"] == "needs_manual":
                                    counters["external"] += 1
                                elif result["status"] == "failed":
                                    counters["failed"] += 1
                            job_count += 1
                        except Exception as e:
                            log.warning(f"Error processing card: {e}")
                            errors.append(str(e))
                            counters["failed"] += 1

                        time.sleep(cfg["settings"].get("pause_between_jobs", 3))

                    page_num += 1

        except Exception as e:
            log.error(f"Bot crashed: {e}", exc_info=True)
            errors.append(str(e))
        finally:
            browser.close()

    finish_run(
        run_id,
        discovered=counters["discovered"],
        applied=counters["applied"],
        external=counters["external"],
        failed=counters["failed"],
        error_log="\n".join(errors) if errors else None,
    )
    summary = (
        f"discovered={counters['discovered']} "
        f"applied={counters['applied']} "
        f"needs_manual={counters['external']} "
        f"failed={counters['failed']}"
    )
    log.info(f"Run complete — {summary}")

    _notify(
        "JobBot run complete ✓",
        f"Applied: {counters['applied']}  |  Needs manual: {counters['external']}  |  Found: {counters['discovered']}"
    )
    return counters


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _login(page: Page, cfg: dict):
    """
    Navigate to LinkedIn and wait for the user to log in manually in the
    browser window. This is the most reliable approach — it bypasses all
    bot-detection, CAPTCHAs, and 2FA without any selector fragility.
    """
    log.info("Opening LinkedIn login page...")
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
    human_delay(1, 2)

    # If already logged in, we land on /feed directly — skip the prompt
    if re.search(r"linkedin\.com/(feed|jobs|mynetwork|messaging)", page.url):
        log.info("Already logged in — continuing.")
        return

    # Try to auto-fill if the form is present (best-effort, not required)
    _try_autofill_login(page, cfg)

    # Wait for the user to finish logging in (handles 2FA, CAPTCHA, etc.)
    print("\n" + "─" * 60)
    print("  LinkedIn is open in the browser window.")
    print("  Log in normally (email + password + any 2FA/CAPTCHA).")
    print("  Once you see your LinkedIn feed, come back here and")
    print("  press  Enter  to start the job bot.")
    print("─" * 60)
    input("\n  → Press Enter when you are logged in: ")

    # Confirm we're actually on a logged-in page
    if not re.search(r"linkedin\.com/(feed|jobs|mynetwork|messaging|search)", page.url):
        # Navigate to feed as a sanity check
        page.goto("https://www.linkedin.com/feed", wait_until="domcontentloaded", timeout=20000)
        human_delay(1, 2)

    log.info(f"Logged in — current URL: {page.url}")


def _try_autofill_login(page: Page, cfg: dict):
    """Best-effort auto-fill of email+password. Silently skips if fields not found."""
    try:
        for sel in ("#username", "input[name='session_key']", "input[autocomplete='username']"):
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(cfg["linkedin"]["email"])
                human_delay(0.3, 0.6)
                break

        for sel in ("#password", "input[name='session_password']", "input[type='password']"):
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(cfg["linkedin"]["password"])
                human_delay(0.3, 0.6)
                break

        submit = page.query_selector('button[type="submit"]')
        if submit and submit.is_visible():
            submit.click()
            log.info("Auto-filled login form — waiting for redirect...")
            human_delay(3, 4)
    except Exception as e:
        log.debug(f"Auto-fill skipped: {e}")


# ---------------------------------------------------------------------------
# Search URL builder
# ---------------------------------------------------------------------------

def _build_search_url(cfg: dict, mode: str = "general") -> str:
    """
    Build a LinkedIn jobs search URL.

    mode="target"  — uses f_C company filter, wider date window, no remote restriction
    mode="general" — no company filter, uses all search filters from config
    """
    s = cfg["search"]
    params = {}

    role_keywords = s.get("role_keywords", s.get("keywords", []))
    params["keywords"] = " OR ".join(role_keywords) if isinstance(role_keywords, list) else role_keywords

    # Location — geoId is more precise than a string
    geo_id = s.get("geo_id")
    if geo_id:
        params["geoId"] = geo_id
        if s.get("distance_km"):
            params["distance"] = s["distance_km"]
    elif s.get("location"):
        params["location"] = s["location"]

    if mode == "target":
        company_ids = _get_company_ids(cfg)
        if company_ids:
            params["f_C"] = ",".join(company_ids)
        # Target company pass: wider date window, no remote restriction
        # (these companies post infrequently and many roles are hybrid/on-site)
        params["f_TPR"] = DATE_POSTED_MAP.get("month", "r2592000")
    else:
        # General pass: apply all filters from config
        if s.get("remote"):
            params["f_WT"] = "2"
        date_posted = s.get("date_posted", "")
        if date_posted and date_posted in DATE_POSTED_MAP:
            params["f_TPR"] = DATE_POSTED_MAP[date_posted]

    exp_levels = s.get("experience_levels", [])
    if exp_levels:
        params["f_E"] = ",".join(EXPERIENCE_MAP[e] for e in exp_levels if e in EXPERIENCE_MAP)

    job_types = s.get("job_types", [])
    if job_types:
        params["f_JT"] = ",".join(JOB_TYPE_MAP[t] for t in job_types if t in JOB_TYPE_MAP)

    params["sortBy"] = "DD"
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


def _get_company_ids(cfg: dict) -> list[str]:
    """Return valid (non-zero) LinkedIn company ID strings from target_companies."""
    ids = []
    for entry in cfg.get("target_companies", []):
        cid = entry.get("linkedin_id") if isinstance(entry, dict) else None
        if cid and int(cid) != 0:
            ids.append(str(cid))
    return ids


# ---------------------------------------------------------------------------
# Debug helper
# ---------------------------------------------------------------------------

def _debug_dump_selectors(page: Page):
    """Log how many elements each candidate selector finds."""
    candidates = [
        "li[data-occludable-job-id]",
        "li[data-job-id]",
        "div[data-job-id]",
        "div.job-card-container",
        "div.jobs-search-results__list-item",
        "li.jobs-search-results__list-item",
        "ul.scaffold-layout__list-container li",
        ".jobs-search-results-list li",
        "a.job-card-container__link",
    ]
    for sel in candidates:
        count = len(page.query_selector_all(sel))
        if count:
            log.info(f"[debug] {sel!r:60s} → {count} elements  ✓")
        else:
            log.debug(f"[debug] {sel!r:60s} → 0")


# ---------------------------------------------------------------------------
# Job card extraction
# ---------------------------------------------------------------------------

# Ordered from most stable (data-attr) to most fragile (class-name)
_CARD_SELECTORS = [
    "li[data-occludable-job-id]",          # stable data attribute (2023-2025+)
    "li[data-job-id]",                      # older variant
    "div[data-job-id]",
    "li.jobs-search-results__list-item",    # class-based fallback
    "div.job-card-container",               # very old fallback
]

def _get_job_cards(page: Page) -> list:
    for sel in _CARD_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=6000)
            cards = page.query_selector_all(sel)
            if cards:
                log.info(f"Job cards matched by: {sel!r}")
                return cards
        except PWTimeout:
            continue
    log.warning("No job card selector matched. Try running with --debug to inspect the page.")
    return []


def _is_excluded_title(title: str, cfg: dict) -> bool:
    """Return True if the job title contains any excluded keyword."""
    excluded = cfg["search"].get("exclude_title_keywords", [])
    title_lower = title.lower()
    for kw in excluded:
        if kw.lower() in title_lower:
            log.debug(f"  Skipping (excluded keyword '{kw}'): {title}")
            return True
    return False


def _is_target_company(company: str, cfg: dict) -> bool:
    """
    Return True if company filter is off, or if the company matches the target list.
    Handles both plain strings and {name, linkedin_id} dicts.
    """
    targets = cfg.get("target_companies", [])
    if not targets or not cfg["settings"].get("target_companies_only", False):
        return True
    company_lower = company.lower()
    for t in targets:
        name = (t["name"] if isinstance(t, dict) else t).lower()
        if name in company_lower:
            return True
    return False


def _process_job_card(page: Page, card, cfg: dict) -> dict | None:
    """Click a job card, extract details, filter, and attempt to apply."""
    try:
        card.click()
        human_delay(1.5, 2.5)
    except Exception:
        return None

    # Wait for detail panel + apply button to fully render
    for detail_sel in (
        "h1.t-24",
        ".job-details-jobs-unified-top-card__job-title",
        ".jobs-unified-top-card__job-title",
    ):
        try:
            page.wait_for_selector(detail_sel, timeout=5000)
            break
        except PWTimeout:
            pass
    human_delay(0.8, 1.2)  # extra wait for apply button to render

    job_data = _extract_job_details(page)
    if not job_data:
        return None

    # ── Filters ────────────────────────────────────────────────────────
    if _is_excluded_title(job_data["title"], cfg):
        return None

    if not _is_target_company(job_data["company"], cfg):
        log.debug(f"  Skipping (not a target company): {job_data['company']}")
        return None

    linkedin_id = job_data["linkedin_id"]
    if cfg["settings"].get("skip_if_already_applied", True) and job_exists(linkedin_id):
        log.debug(f"Already seen: {job_data['title']} @ {job_data['company']}")
        return None

    log.info(f"✓ {job_data['title']} @ {job_data['company']} [{job_data['apply_method']}]")

    if job_data["apply_method"] == "easy_apply":
        success = _do_easy_apply(page, cfg, job_data)
        job_data["status"] = "applied" if success else "failed"
        if success:
            job_data["applied_at"] = _now_iso()
    else:
        job_data["status"] = "needs_manual"

    upsert_job(job_data)
    return job_data


def _detect_apply_method(page: Page) -> tuple[str, str | None]:
    """
    Return (apply_method, external_url).
    Casts a wide net across all buttons/links in the detail panel and checks
    whether any of them say "Easy Apply" in text or aria-label.
    Falls back to "external" with a best-effort URL if nothing matches.
    """
    # Everything that could be an apply button — broad on purpose
    candidates = page.query_selector_all(
        "button, a[role='link']"
    )
    for el in candidates:
        try:
            label = (el.get_attribute("aria-label") or "").lower()
            text  = el.inner_text().strip().lower()
            if "easy apply" in label or "easy apply" in text:
                log.debug(f"Easy Apply detected via: label={label!r} text={text!r}")
                return "easy_apply", None
        except Exception:
            pass

    # Nothing said "Easy Apply" — it's an external application
    return "external", _get_external_apply_url(page)


def _first_text(page: Page, *selectors) -> str:
    """Return inner text of the first selector that matches."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                return el.inner_text().strip()
        except Exception:
            pass
    return ""


def _extract_job_details(page: Page) -> dict | None:
    """Pull job metadata from the detail panel."""
    try:
        current_url = page.url

        # Wait briefly for the detail panel to populate
        for title_sel in (
            "h1.t-24",
            ".job-details-jobs-unified-top-card__job-title h1",
            ".jobs-unified-top-card__job-title h1",
            "h2.t-24",
            ".job-details-jobs-unified-top-card__job-title",
        ):
            try:
                page.wait_for_selector(title_sel, timeout=4000)
                break
            except PWTimeout:
                pass

        title = _first_text(page,
            "h1.t-24",
            ".job-details-jobs-unified-top-card__job-title h1",
            ".jobs-unified-top-card__job-title h1",
            "h2.t-24",
            ".job-details-jobs-unified-top-card__job-title",
            ".jobs-unified-top-card__job-title",
        )

        company = _first_text(page,
            ".job-details-jobs-unified-top-card__company-name a",
            ".job-details-jobs-unified-top-card__company-name",
            ".jobs-unified-top-card__company-name a",
            ".jobs-unified-top-card__company-name",
            "a[data-tracking-control-name='public_jobs_topcard-org-name']",
        )

        location = _first_text(page,
            ".job-details-jobs-unified-top-card__bullet",
            ".jobs-unified-top-card__bullet",
            ".job-details-jobs-unified-top-card__primary-description-container span",
            "span.tvm__text--positive",
        )

        # LinkedIn job ID from the URL or data attribute
        match = re.search(r"/jobs/view/(\d+)", current_url)
        if not match:
            # Try the job ID from the active card's data attribute
            active = page.query_selector("[data-occludable-job-id][class*='active'], [data-job-id][class*='active']")
            jid = active.get_attribute("data-occludable-job-id") or active.get_attribute("data-job-id") if active else None
            linkedin_id = jid or f"unknown_{int(time.time())}"
        else:
            linkedin_id = match.group(1)

        # Determine apply method
        # Cast a wide net: any visible button or link whose text/label says "Easy Apply"
        apply_method, external_url = _detect_apply_method(page)

        # Salary / workplace type from insight items
        salary, workplace_type = "", ""
        insight_sels = [
            ".job-details-jobs-unified-top-card__job-insight span",
            ".jobs-unified-top-card__job-insight span",
            "li.job-details-jobs-unified-top-card__job-insight",
        ]
        for isel in insight_sels:
            for item in page.query_selector_all(isel):
                try:
                    text = item.inner_text().strip().lower()
                    if any(k in text for k in ["₹", "$", "£", "€", "lpa", "salary", "/yr", "/mo"]):
                        salary = item.inner_text().strip()
                    if any(k in text for k in ["remote", "hybrid", "on-site", "on site", "in-office"]):
                        workplace_type = item.inner_text().strip()
                except Exception:
                    pass

        if not title:
            log.warning(f"Could not extract title from {current_url} — skipping")
            return None

        return {
            "linkedin_id":    linkedin_id,
            "title":          title,
            "company":        company,
            "location":       location,
            "workplace_type": workplace_type,
            "salary":         salary,
            "linkedin_url":   current_url,
            "external_url":   external_url,
            "apply_method":   apply_method,
            "status":         "discovered",
            "applied_at":     None,
            "notes":          None,
        }
    except Exception as e:
        log.warning(f"Failed to extract job details: {e}")
        return None


def _get_external_apply_url(page: Page) -> str | None:
    """Try to capture the external apply URL without navigating away."""
    try:
        # Some external buttons have an href on a wrapping anchor
        anchor = page.query_selector("a.jobs-apply-button")
        if anchor:
            href = anchor.get_attribute("href")
            if href and href.startswith("http"):
                return href
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Easy Apply flow
# ---------------------------------------------------------------------------

def _do_easy_apply(page: Page, cfg: dict, job_data: dict) -> bool:
    """Click Easy Apply and navigate the multi-step modal."""
    log.info(f"  → Starting Easy Apply for {job_data['title']}")
    try:
        # Find button whose label or text contains "Easy Apply"
        btn = None
        for candidate in page.query_selector_all("button.jobs-apply-button, .jobs-apply-button"):
            try:
                label = (candidate.get_attribute("aria-label") or "").lower()
                text  = candidate.inner_text().strip().lower()
                if "easy apply" in label or "easy apply" in text:
                    btn = candidate
                    break
            except Exception:
                pass
        if not btn:
            log.warning("  → Easy Apply button not found")
            return False

        btn.click()
        human_delay(1.5, 2.5)

        # Iterate through modal steps (LinkedIn can have 1-5 steps)
        max_steps = 8
        for step in range(max_steps):
            if not page.query_selector(".jobs-easy-apply-modal"):
                log.info("  → Modal closed (likely submitted or dismissed)")
                break

            # Fill form fields on this step
            _fill_easy_apply_step(page, cfg)
            human_delay(0.8, 1.5)

            # Check for Submit button
            submit_btn = page.query_selector(
                "button[aria-label='Submit application'], "
                "footer button.artdeco-button--primary[aria-label*='Submit']"
            )
            if submit_btn:
                submit_btn.click()
                human_delay(1.5, 2.5)
                # Confirm success dialog
                if page.query_selector("div.jobs-easy-apply-modal h3"):
                    page.keyboard.press("Escape")
                log.info("  → Application submitted!")
                return True

            # Next / Review button
            next_btn = page.query_selector(
                "button[aria-label='Continue to next step'], "
                "footer button.artdeco-button--primary[aria-label*='Next'], "
                "footer button.artdeco-button--primary[aria-label*='Review']"
            )
            if next_btn:
                next_btn.click()
                human_delay(1, 2)
            else:
                # No recognizable button — bail out
                log.warning("  → Could not find Next/Submit button, dismissing")
                _dismiss_modal(page)
                return False

        log.warning("  → Exceeded max steps, dismissing modal")
        _dismiss_modal(page)
        return False

    except Exception as e:
        log.error(f"  → Easy Apply error: {e}")
        _dismiss_modal(page)
        return False


def _fill_easy_apply_step(page: Page, cfg: dict):
    """Fill all visible inputs in the current Easy Apply step."""
    ea_cfg = cfg.get("easy_apply", {})
    profile = cfg.get("profile", {})

    # Phone number
    phone_input = page.query_selector("input[id*='phoneNumber'], input[name*='phone']")
    if phone_input and not phone_input.input_value():
        phone_input.fill(ea_cfg.get("phone", profile.get("phone", "")))
        human_delay(0.2, 0.5)

    # Text inputs — match label text to known fields
    text_inputs = page.query_selector_all("input[type='text'], input[type='number']")
    for inp in text_inputs:
        if inp.input_value():
            continue
        label_text = _get_input_label(page, inp).lower()
        value = _guess_text_answer(label_text, cfg)
        if value:
            inp.fill(str(value))
            human_delay(0.1, 0.3)

    # Dropdowns (select elements)
    selects = page.query_selector_all("select")
    for sel in selects:
        try:
            current = sel.evaluate("el => el.value")
            if current:
                continue
            label_text = _get_input_label(page, sel).lower()
            # Try "Yes" or "No" based on boolean answers config
            options = sel.evaluate("el => Array.from(el.options).map(o => o.text)")
            answer = _guess_boolean_answer(label_text, options, ea_cfg)
            if answer:
                sel.select_option(label=answer)
                human_delay(0.1, 0.3)
        except Exception:
            pass

    # Radio buttons (yes/no questions)
    radio_groups = page.query_selector_all("fieldset")
    for fieldset in radio_groups:
        try:
            legend = fieldset.query_selector("legend")
            if not legend:
                continue
            question = legend.inner_text().strip().lower()
            radios = fieldset.query_selector_all("input[type='radio']")
            if not radios:
                continue

            # Only click if nothing selected
            checked = fieldset.query_selector("input[type='radio']:checked")
            if checked:
                continue

            target_value = _guess_boolean_radio(question, ea_cfg)
            for radio in radios:
                label = _get_input_label(page, radio).lower()
                if target_value is True and label in ("yes", "true"):
                    radio.click()
                    break
                elif target_value is False and label in ("no", "false"):
                    radio.click()
                    break
        except Exception:
            pass


def _get_input_label(page: Page, element) -> str:
    """Find the label text associated with an input element."""
    try:
        el_id = element.get_attribute("id")
        if el_id:
            label = page.query_selector(f"label[for='{el_id}']")
            if label:
                return label.inner_text().strip()
        # Traverse up to find a label
        return element.evaluate("""el => {
            let node = el.closest('div[class*="form-component"]');
            if (!node) node = el.parentElement;
            if (!node) return '';
            let lbl = node.querySelector('label');
            return lbl ? lbl.innerText.trim() : '';
        }""")
    except Exception:
        return ""


def _guess_text_answer(label: str, cfg: dict) -> str:
    """Return a sensible answer for a text input based on its label."""
    profile = cfg.get("profile", {})
    ea_cfg = cfg.get("easy_apply", {})

    mappings = {
        "first name":    profile.get("first_name", ""),
        "last name":     profile.get("last_name", ""),
        "email":         profile.get("email", ""),
        "phone":         profile.get("phone", ea_cfg.get("phone", "")),
        "city":          profile.get("location", ""),
        "location":      profile.get("location", ""),
        "linkedin":      profile.get("linkedin_url", ""),
        "github":        profile.get("github_url", ""),
        "website":       profile.get("portfolio_url", ""),
        "portfolio":     profile.get("portfolio_url", ""),
        "salary":        profile.get("salary_expectation", ""),
        "expected":      profile.get("salary_expectation", ""),
        "compensation":  profile.get("salary_expectation", ""),
    }

    for keyword, value in mappings.items():
        if keyword in label and value:
            return value

    # Years of experience
    if "year" in label and "experience" in label:
        yoe = ea_cfg.get("years_of_experience", {})
        for tech, years in yoe.items():
            if tech != "default" and tech in label:
                return str(years)
        return str(yoe.get("default", ""))

    return ""


def _guess_boolean_answer(label: str, options: list, ea_cfg: dict) -> str | None:
    """Return Yes/No option text for a select based on boolean_answers config."""
    bool_answers = ea_cfg.get("boolean_answers", {})
    for key, value in bool_answers.items():
        keyword = key.replace("_", " ")
        if keyword in label:
            target = "Yes" if value else "No"
            for opt in options:
                if opt.strip().lower() == target.lower():
                    return opt
    return None


def _guess_boolean_radio(question: str, ea_cfg: dict) -> bool | None:
    bool_answers = ea_cfg.get("boolean_answers", {})
    for key, value in bool_answers.items():
        keyword = key.replace("_", " ")
        if keyword in question:
            return value
    return None


def _dismiss_modal(page: Page):
    try:
        page.keyboard.press("Escape")
        human_delay(0.5, 1)
        discard_btn = page.query_selector("button[data-control-name='discard_application_confirm_btn']")
        if discard_btn:
            discard_btn.click()
    except Exception:
        pass


def _now_iso() -> str:
    from datetime import datetime
    return datetime.utcnow().isoformat()
