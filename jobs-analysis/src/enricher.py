"""
enricher.py — NLP enrichment pipeline for job postings.

Layer 1: Regex extraction (years_experience, salary_text, remote_policy).
Layer 2: LLM extraction via Anthropic API (required_skills, nice_to_have,
         fields, seniority_explicit).

Reads  : data/raw/jobs_raw.csv
Writes : data/enriched/jobs_enriched.csv
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import anthropic
import pandas as pd
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & env
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

RAW_PATH = PROJECT_ROOT / "data" / "raw" / "jobs_raw.csv"
ENRICHED_DIR = PROJECT_ROOT / "data" / "enriched"
ENRICHED_DIR.mkdir(parents=True, exist_ok=True)
ENRICHED_PATH = ENRICHED_DIR / "jobs_enriched.csv"

# ---------------------------------------------------------------------------
# LLM config
# ---------------------------------------------------------------------------
MODEL = "claude-haiku-4-5-20251001"
MAX_CONCURRENT = 2
BATCH_DELAY = 8.0  # seconds between batches
MAX_DESC_CHARS = 4_000  # truncate descriptions to limit token usage

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
_RE_YEARS = re.compile(r"(\d+)\\?\+?\s*years?[\u2019']?\s*(?:of\s+)?(?:\w+\s+)?experience", re.IGNORECASE)
_RE_SALARY = re.compile(r"£([\d,]+)\s*(?:[-–to]+\s*£([\d,]+))?", re.IGNORECASE)
_RE_REMOTE = re.compile(r"\b(remote|hybrid|on.?site|in.?office)\b", re.IGNORECASE)

_REMOTE_MAP = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "on-site": "On-site",
    "onsite": "On-site",
    "in-office": "On-site",
    "in office": "On-site",
}


# ---------------------------------------------------------------------------
# Layer 1 — Regex
# ---------------------------------------------------------------------------

def extract_years_experience(text: str) -> Optional[int]:
    """Return the minimum years of experience mentioned in text, or None."""
    if not isinstance(text, str):
        return None
    matches = _RE_YEARS.findall(text)
    if not matches:
        return None
    return min(int(m) for m in matches)


def extract_salary_text(text: str) -> Optional[str]:
    """Return raw salary string if present in text, or None."""
    if not isinstance(text, str):
        return None
    m = _RE_SALARY.search(text)
    if not m:
        return None
    lo = m.group(1)
    hi = m.group(2)
    return f"£{lo} - £{hi}" if hi else f"£{lo}"


def extract_remote_policy(text: str, is_remote_flag: Optional[bool], title: str = "") -> str:
    """
    Classify remote policy from description text and job title.

    Returns one of: "remote", "hybrid", "on-site".
    Falls back to is_remote_flag if no keyword found.
    """
    combined = f"{title} {text}" if isinstance(title, str) else text
    if isinstance(combined, str):
        # Scan for keywords in order of specificity
        found = set()
        for m in _RE_REMOTE.finditer(combined):
            kw = m.group(1).lower().replace(" ", "-").replace(".", "-")
            normalised = _REMOTE_MAP.get(kw, kw)
            found.add(normalised)
        if "Hybrid" in found:
            return "Hybrid"
        if "Remote" in found:
            return "Remote"
        if "On-site" in found:
            return "On-site"
    # Fallback to the boolean flag JobSpy provides (use == to handle numpy.bool_)
    if bool(is_remote_flag) is True:
        return "Remote"
    return "On-site"


# ---------------------------------------------------------------------------
# Layer 2 — LLM
# ---------------------------------------------------------------------------

_LLM_PROMPT = """\
You are a data extraction assistant. Extract the following fields from the \
job description below. Return a JSON object with exactly these keys:

{{
  "required_skills": [],   // technical tools & languages only (SQL, Python,
                           // Power BI, Tableau, dbt, Spark, Excel, etc.)
                           // No soft skills.
  "nice_to_have": [],      // same format, optional technical skills only
  "soft_skills": [],       // interpersonal & behavioural skills only
                           // (e.g. communication, stakeholder management,
                           //  attention to detail, problem-solving, etc.)
                           // No technical tools.
  "fields": [],            // one or more of: data, product, marketing,
                           //   finance, engineering, commercial,
                           //   operations, other
                           // Use multiple values for cross-functional roles
  "seniority_explicit": "" // seniority IF explicitly stated in the job spec
                           // (e.g. "Senior", "Junior", "Lead", "Principal")
                           // Return null if not explicitly mentioned
}}

Rules:
- required_skills: tools and platforms only, no soft skills
- soft_skills: behavioural/interpersonal only, no tools
- fields: list of 1 to 3 values
- seniority_explicit: extract only what is written, do not infer
- Return JSON only, no markdown, no explanation

Job description:
{description}
"""

_COMPANY_PROMPT = """\
Based on this job description, infer the following about the hiring company. \
Return JSON only:

{{
  "industry": "",       // e.g. fintech, healthcare, retail, consulting,
                        //   tech, media, public_sector, other
  "size_estimate": ""   // one of: startup, scaleup, mid_market, enterprise
                        // infer from language, team size references, or
                        // company name if well known — null if unclear
}}

Job description:
{description}
"""


def _normalise_seniority(raw: Optional[str]) -> Optional[str]:
    """Map raw LLM seniority strings to canonical labels."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if any(k in s for k in ("junior", "graduate", "grad", "entry", "associate", "intern")):
        return "Entry level"
    if any(k in s for k in ("mid", "middle", "intermediate", "ii", "2")):
        return "Mid level"
    if any(k in s for k in ("senior", "sr.", "sr ", "principal", "staff", "iii", "3")):
        return "senior"
    if any(k in s for k in ("lead", "head", "director", "vp", "vice president", "manager")):
        return "Lead principal"
    return None


def _resolve_seniority(
    seniority_explicit: Optional[str],
    years_experience: Optional[int],
    title: str = "",
) -> tuple[str, str]:
    """Return (seniority, seniority_source).

    Resolution order:
    1. Explicit seniority from LLM
    2. Seniority keywords in job title
    3. Years of experience (regex)
    4. not_specified
    """
    if isinstance(seniority_explicit, str) and seniority_explicit.strip():
        normalised = _normalise_seniority(seniority_explicit)
        return (normalised or seniority_explicit.strip()), "Explicit"

    if isinstance(title, str) and title.strip():
        from_title = _normalise_seniority(title)
        if from_title:
            return from_title, "Title"

    if years_experience is None or (isinstance(years_experience, float) and years_experience != years_experience):
        return "Not specified", "Inferred"
    if years_experience <= 2:
        return "Entry level", "Inferred"
    if years_experience <= 5:
        return "Mid level", "Inferred"
    if years_experience <= 10:
        return "Senior", "Inferred"
    return "Lead principal", "Inferred"


def _parse_llm_json(raw: str) -> dict:
    """Parse JSON from LLM response, stripping any markdown fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = raw.rstrip("`").strip()
    return json.loads(raw)


async def _call_llm(client: anthropic.AsyncAnthropic, prompt: str) -> dict:
    """Make a single LLM call and return parsed JSON dict, with retry on 429."""
    for attempt in range(5):
        try:
            message = await client.messages.create(
                model=MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text
            return _parse_llm_json(raw)
        except anthropic.RateLimitError:
            wait = 2 ** attempt * 10  # 10s, 20s, 40s, 80s, 160s
            logger.warning("Rate limited — retrying in %ds (attempt %d/5)", wait, attempt + 1)
            await asyncio.sleep(wait)
    raise RuntimeError("Exceeded retry limit on rate limiting")


async def enrich_row_llm(
    client: anthropic.AsyncAnthropic,
    description: str,
) -> dict:
    """Run both LLM prompts for a single job row. Returns merged dict."""
    job_result: dict = {
        "required_skills": None,
        "nice_to_have": None,
        "soft_skills": None,
        "fields": None,
        "seniority_explicit": None,
        "industry": None,
        "size_estimate": None,
    }
    desc = description[:MAX_DESC_CHARS] if isinstance(description, str) else ""

    # Job-level extraction
    try:
        job_data = await _call_llm(client, _LLM_PROMPT.format(description=desc))
        job_result["required_skills"] = json.dumps(job_data.get("required_skills") or [])
        job_result["nice_to_have"] = json.dumps(job_data.get("nice_to_have") or [])
        job_result["soft_skills"] = json.dumps(job_data.get("soft_skills") or [])
        job_result["fields"] = json.dumps(job_data.get("fields") or [])
        job_result["seniority_explicit"] = job_data.get("seniority_explicit")
    except Exception:
        logger.exception("Job LLM extraction failed")

    # Company-level extraction
    try:
        company_data = await _call_llm(client, _COMPANY_PROMPT.format(description=desc))
        job_result["industry"] = company_data.get("industry")
        job_result["size_estimate"] = company_data.get("size_estimate")
    except Exception:
        logger.exception("Company LLM extraction failed")

    return job_result


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def process_batch(
    client: anthropic.AsyncAnthropic,
    rows: list[dict],
) -> list[dict]:
    """Process a batch of rows concurrently (up to MAX_CONCURRENT)."""
    tasks = [enrich_row_llm(client, row.get("description", "")) for row in rows]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    enriched = []
    for row, result in zip(rows, results):
        merged = dict(row)
        if isinstance(result, dict):
            merged.update(result)
        else:
            logger.error("Batch item failed: %s", result)
        enriched.append(merged)
    return enriched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the enrichment pipeline."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error(
            "ANTHROPIC_API_KEY is not set. "
            "Copy .env.example to .env and add your key before running."
        )
        sys.exit(1)

    if not RAW_PATH.exists():
        logger.error("Raw data not found at %s — run scraper.py first", RAW_PATH)
        sys.exit(1)

    df = pd.read_csv(RAW_PATH)
    logger.info("Loaded %d rows from %s", len(df), RAW_PATH)

    # Load already-enriched rows to enable incremental caching.
    # A row is considered done only if required_skills is non-null.
    done_urls: set[str] = set()
    cached_df: pd.DataFrame = pd.DataFrame()
    if ENRICHED_PATH.exists():
        cached_df = pd.read_csv(ENRICHED_PATH)
        if "job_url" in cached_df.columns and "required_skills" in cached_df.columns:
            has_skills = cached_df["required_skills"].notna() & (cached_df["required_skills"] != "[]")
            has_soft = "soft_skills" in cached_df.columns and cached_df["soft_skills"].notna()
            done_urls = set(
                cached_df.loc[has_skills & has_soft, "job_url"].dropna().tolist()
            )
        logger.info("Cache: %d rows already successfully enriched — skipping", len(done_urls))

    # Filter to rows that still need enrichment
    if "job_url" in df.columns:
        pending = df[~df["job_url"].isin(done_urls)].copy()
    else:
        pending = df.copy()

    logger.info("%d rows to enrich", len(pending))

    # -----------------------------------------------------------------------
    # Layer 1 — Regex (fast, no API cost)
    # -----------------------------------------------------------------------
    pending["years_experience"] = pending["description"].apply(extract_years_experience)
    pending["salary_text"] = pending.apply(
        lambda r: r.get("salary_text") or extract_salary_text(str(r.get("description", ""))),
        axis=1,
    )
    pending["remote_policy"] = pending.apply(
        lambda r: extract_remote_policy(
            str(r.get("description", "")),
            r.get("is_remote"),
            str(r.get("title", "")),
        ),
        axis=1,
    )

    # -----------------------------------------------------------------------
    # Layer 2 — LLM
    # -----------------------------------------------------------------------
    rows_dicts = pending.to_dict(orient="records")
    enriched_rows: list[dict] = []

    client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run_all() -> None:
        for i in range(0, len(rows_dicts), MAX_CONCURRENT):
            batch = rows_dicts[i : i + MAX_CONCURRENT]
            logger.info(
                "LLM batch %d–%d / %d",
                i + 1,
                min(i + MAX_CONCURRENT, len(rows_dicts)),
                len(rows_dicts),
            )
            results = await process_batch(client, batch)
            enriched_rows.extend(results)
            if i + MAX_CONCURRENT < len(rows_dicts):
                await asyncio.sleep(BATCH_DELAY)

    asyncio.run(run_all())

    # -----------------------------------------------------------------------
    # Seniority resolution for newly enriched rows
    # -----------------------------------------------------------------------
    enriched_df = pd.DataFrame(enriched_rows)
    if not enriched_df.empty:
        enriched_df["seniority"], enriched_df["seniority_source"] = zip(
            *enriched_df.apply(
                lambda r: _resolve_seniority(r.get("seniority_explicit"), r.get("years_experience"), str(r.get("title", ""))),
                axis=1,
            )
        )

    # -----------------------------------------------------------------------
    # Merge: drop stale versions of re-processed rows from cache, then concat
    # -----------------------------------------------------------------------
    if not cached_df.empty and not enriched_df.empty:
        re_processed_urls = set(enriched_df["job_url"].dropna().tolist())
        cached_df = cached_df[~cached_df["job_url"].isin(re_processed_urls)]
        final_df = pd.concat([cached_df, enriched_df], ignore_index=True)
    elif not cached_df.empty:
        final_df = cached_df
    else:
        final_df = enriched_df

    # -----------------------------------------------------------------------
    # Re-apply regex to ALL rows from raw CSV (cheap, ensures fields are fresh)
    # -----------------------------------------------------------------------
    raw_df = pd.read_csv(RAW_PATH)[["job_url", "description", "is_remote"]]
    url_to_raw = raw_df.set_index("job_url")

    def _refresh_regex(r: pd.Series) -> pd.Series:
        url = r.get("job_url")
        if url in url_to_raw.index:
            raw_row = url_to_raw.loc[url]
            desc = str(raw_row.get("description", ""))
            r["years_experience"] = extract_years_experience(desc)
            r["salary_text"] = extract_salary_text(desc) or r.get("salary_text")
            r["remote_policy"] = extract_remote_policy(desc, raw_row.get("is_remote"), str(raw_row.get("title", "")))
        return r

    final_df = final_df.apply(_refresh_regex, axis=1)

    # Re-apply seniority resolution across the entire final dataset
    final_df["seniority"], final_df["seniority_source"] = zip(
        *final_df.apply(
            lambda r: _resolve_seniority(r.get("seniority_explicit"), r.get("years_experience"), str(r.get("title", ""))),
            axis=1,
        )
    )

    final_df.to_csv(ENRICHED_PATH, index=False)
    logger.info("Saved %d enriched rows to %s", len(final_df), ENRICHED_PATH)


if __name__ == "__main__":
    main()
