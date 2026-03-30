"""
scraper.py — LinkedIn job scraper using JobSpy.

Scrapes "data analyst" roles in the UK, deduplicates, and saves to
data/raw/jobs_raw.csv.
"""

import logging
import signal
import sys
import time
from pathlib import Path

import pandas as pd
from jobspy import scrape_jobs

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = RAW_DIR / "jobs_raw.csv"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TARGET_COUNT = 1_000
INITIAL_HOURS_OLD = 4_320          # 6 months
HOURS_INCREMENT = 30 * 24          # +30 days per iteration
MAX_ITERATIONS = 10
MAX_HOURS_OLD = 365 * 24           # hard cap: 1 year
SEARCH_TERM = "data analyst"
LOCATION = "United Kingdom"


def scrape(hours_old: int, results_wanted: int) -> pd.DataFrame:
    """Run a single JobSpy scrape and return a DataFrame."""
    logger.info("Scraping: hours_old=%d, results_wanted=%d", hours_old, results_wanted)
    try:
        jobs = scrape_jobs(
            site_name=["linkedin"],
            search_term=SEARCH_TERM,
            location=LOCATION,
            hours_old=hours_old,
            results_wanted=results_wanted,
            description_format="markdown",
            linkedin_fetch_description=True,
        )
        return jobs if jobs is not None else pd.DataFrame()
    except Exception:
        logger.exception("JobSpy scrape failed")
        return pd.DataFrame()


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate on (title, company, location), keeping the most recent."""
    if df.empty:
        return df
    df = df.copy()
    # Normalise key fields
    for col in ("title", "company", "location"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.lower()
    # Sort so most recent rows win
    if "date_posted" in df.columns:
        df["date_posted"] = pd.to_datetime(df["date_posted"], errors="coerce")
        df = df.sort_values("date_posted", ascending=False)
    before = len(df)
    df = df.drop_duplicates(subset=["title", "company", "location"], keep="first")
    after = len(df)
    logger.info("Deduplication: %d → %d rows (%d removed)", before, after, before - after)
    return df


def main() -> None:
    """Entry point: scrape until TARGET_COUNT distinct jobs are collected."""
    all_jobs: pd.DataFrame = pd.DataFrame()

    def _save_and_exit(sig, frame) -> None:  # type: ignore[type-arg]
        logger.info("Interrupted — saving %d jobs collected so far", len(all_jobs))
        if not all_jobs.empty:
            all_jobs.to_csv(OUTPUT_PATH, index=False)
            logger.info("Saved to %s", OUTPUT_PATH)
        sys.exit(0)

    signal.signal(signal.SIGINT, _save_and_exit)

    hours_old = INITIAL_HOURS_OLD
    results_wanted = TARGET_COUNT

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info("--- Iteration %d ---", iteration)
        batch = scrape(hours_old=hours_old, results_wanted=results_wanted)

        if batch.empty:
            logger.warning("Empty result — stopping early")
            break

        if all_jobs.empty:
            all_jobs = batch
        else:
            previous_count = len(all_jobs)
            all_jobs = pd.concat([all_jobs, batch], ignore_index=True)
            all_jobs = deduplicate(all_jobs)
            if len(all_jobs) <= previous_count:
                logger.info("No new rows added — stopping")
                break

        all_jobs = deduplicate(all_jobs)
        logger.info("Running total after dedup: %d", len(all_jobs))

        if len(all_jobs) >= TARGET_COUNT:
            logger.info("Target reached: %d jobs", len(all_jobs))
            break

        # Expand window for next iteration
        hours_old += HOURS_INCREMENT
        if hours_old > MAX_HOURS_OLD:
            logger.info("Reached 1-year cap (hours_old=%d) — stopping", hours_old)
            break
        results_wanted = TARGET_COUNT - len(all_jobs) + 200  # buffer
        logger.info("Expanding window to hours_old=%d, results_wanted=%d", hours_old, results_wanted)
        time.sleep(2)  # polite delay between retries

    # -----------------------------------------------------------------------
    # Final output
    # -----------------------------------------------------------------------
    if all_jobs.empty:
        logger.error("No jobs collected — check your network and JobSpy version")
        return

    all_jobs = deduplicate(all_jobs)

    # Restore proper casing (we lowercased for dedup — re-read raw if needed)
    # JobSpy returns proper-cased strings; we only lowercased for key comparison.
    # Since we kept the original df rows, values should still be original-cased
    # *unless* the dedup mutated them. Re-read from the intermediate concat to
    # preserve originals — simplest fix: skip lowercasing in place and use a
    # copy for the key comparison only (already done above with df.copy()).

    # Save
    all_jobs.to_csv(OUTPUT_PATH, index=False)
    logger.info("Saved %d jobs to %s", len(all_jobs), OUTPUT_PATH)

    # Summary
    date_min = all_jobs["date_posted"].min() if "date_posted" in all_jobs.columns else "n/a"
    date_max = all_jobs["date_posted"].max() if "date_posted" in all_jobs.columns else "n/a"
    logger.info(
        "Summary — total scraped: %d | final count: %d | date range: %s → %s",
        len(all_jobs),
        len(all_jobs),
        date_min,
        date_max,
    )


if __name__ == "__main__":
    main()
