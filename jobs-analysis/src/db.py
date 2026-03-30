"""
db.py — Load enriched job data into a normalised SQLite database.

Reads  : data/enriched/jobs_enriched.csv
Writes : jobs.db (SQLite, project root)
"""

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

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
ENRICHED_PATH = PROJECT_ROOT / "data" / "enriched" / "jobs_enriched.csv"
SOFT_SKILLS_MAPPING = PROJECT_ROOT / "data" / "soft_skills_mapping.csv"
TECH_SKILLS_MAPPING = PROJECT_ROOT / "data" / "tech_skills_mapping.csv"
DB_PATH = PROJECT_ROOT / "jobs.db"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT UNIQUE,
    linkedin_url        TEXT,
    industry            TEXT,
    size_estimate       TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT,
    company_id          INTEGER,
    location            TEXT,
    date_posted         DATE,
    job_type            TEXT,
    remote_policy       TEXT,
    max_salary          INTEGER,
    years_experience    INTEGER,
    seniority           TEXT,
    seniority_source    TEXT,
    job_url             TEXT UNIQUE,
    FOREIGN KEY (company_id) REFERENCES companies(id)
);

CREATE TABLE IF NOT EXISTS skills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER,
    skill       TEXT,
    skill_type  TEXT,
    canonical   TEXT,
    type        TEXT,
    category    TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_company  ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_skills_skill  ON skills(skill);
CREATE INDEX IF NOT EXISTS idx_skills_job    ON skills(job_id);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(val) -> Optional[int]:
    """Convert a value to int, returning None on failure."""
    try:
        return int(float(val)) if pd.notna(val) else None
    except (TypeError, ValueError):
        return None


def _safe_str(val) -> Optional[str]:
    """Convert a value to str, returning None for NaN/None."""
    if pd.isna(val) if not isinstance(val, (list, dict)) else False:
        return None
    s = str(val).strip()
    return s if s and s.lower() not in ("nan", "none") else None


_RE_SALARY_TEXT = re.compile(r"£([\d,]+)(?:\s*[-–to]+\s*£([\d,]+))?")

_SENIORITY_MAP = {
    "entry_level": "Entry level",
    "mid_level": "Mid level",
    "senior": "Senior",
    "lead_principal": "Lead principal",
    "not_specified": "Not specified",
}

_REMOTE_POLICY_MAP = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "on-site": "On-site",
    "on_site": "On-site",
}



def _parse_salary_text(val) -> tuple[Optional[int], Optional[int]]:
    """Parse a salary_text string like '£45,000 - £55,000' into (min, max) ints."""
    if not isinstance(val, str):
        return None, None
    m = _RE_SALARY_TEXT.search(val)
    if not m:
        return None, None
    lo = int(m.group(1).replace(",", ""))
    hi = int(m.group(2).replace(",", "")) if m.group(2) else lo
    return lo, hi


def _parse_json_list(val) -> list[str]:
    """Parse a JSON-encoded list string, returning [] on failure."""
    if not isinstance(val, str):
        return []
    try:
        result = json.loads(val)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []


def _upsert_company(
    cur: sqlite3.Cursor,
    name: Optional[str],
    linkedin_url: Optional[str],
    industry: Optional[str],
    size_estimate: Optional[str],
) -> Optional[int]:
    """Insert or update a company row; return its id."""
    if not name:
        return None
    # Normalise name for deduplication
    name_norm = " ".join(name.strip().lower().split())
    cur.execute(
        """
        INSERT INTO companies (name, linkedin_url, industry, size_estimate)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            linkedin_url  = excluded.linkedin_url,
            industry      = COALESCE(excluded.industry, industry),
            size_estimate = COALESCE(excluded.size_estimate, size_estimate)
        """,
        (name_norm, linkedin_url, industry, size_estimate),
    )
    cur.execute("SELECT id FROM companies WHERE name = ?", (name_norm,))
    row = cur.fetchone()
    return row[0] if row else None


def _insert_job(cur: sqlite3.Cursor, row: pd.Series, company_id: Optional[int]) -> Optional[int]:
    """Insert a job row; return its id (or None if duplicate/error)."""
    job_url = _safe_str(row.get("job_url"))
    if not job_url:
        return None
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO jobs
                (title, company_id, location, date_posted, job_type, remote_policy,
                 max_salary, years_experience, seniority, seniority_source, job_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _safe_str(row.get("title")),
                company_id,
                _safe_str(row.get("location")),
                _safe_str(row.get("date_posted")),
                _safe_str(row.get("job_type")),
                _REMOTE_POLICY_MAP.get(_safe_str(row.get("remote_policy")) or "", _safe_str(row.get("remote_policy"))),
                _safe_int(row.get("max_amount")) or _parse_salary_text(row.get("salary_text"))[1],
                _safe_int(row.get("years_experience")),
                _SENIORITY_MAP.get(_safe_str(row.get("seniority")) or "", _safe_str(row.get("seniority"))),
                _safe_str(row.get("seniority_source")),
                job_url,
            ),
        )
        if cur.lastrowid and cur.rowcount > 0:
            return cur.lastrowid
        cur.execute("SELECT id FROM jobs WHERE job_url = ?", (job_url,))
        found = cur.fetchone()
        return found[0] if found else None
    except sqlite3.Error:
        logger.exception("Failed to insert job: %s", job_url)
        return None


def _insert_skills(
    cur: sqlite3.Cursor,
    job_id: int,
    required: list[str],
    nice_to_have: list[str],
    soft: list[str],
) -> None:
    """Insert skill rows for a job."""
    for skill in required:
        if skill:
            cur.execute(
                "INSERT INTO skills (job_id, skill, skill_type) VALUES (?, ?, ?)",
                (job_id, skill.strip(), "Required"),
            )
    for skill in nice_to_have:
        if skill:
            cur.execute(
                "INSERT INTO skills (job_id, skill, skill_type) VALUES (?, ?, ?)",
                (job_id, skill.strip(), "Nice to have"),
            )
    for skill in soft:
        if skill:
            cur.execute(
                "INSERT INTO skills (job_id, skill, skill_type) VALUES (?, ?, ?)",
                (job_id, skill.strip(), "Soft"),
            )


# ---------------------------------------------------------------------------
# Soft skills post-processing
# ---------------------------------------------------------------------------

def _apply_soft_skills_mapping(con: sqlite3.Connection) -> None:
    """
    Using soft_skills_mapping.csv:
    1. Move misclassified technical skills (REMOVE rows) from soft → required.
    2. Populate canonical and category for all remaining soft skills.
    """
    if not SOFT_SKILLS_MAPPING.exists():
        logger.warning("Soft skills mapping not found at %s — skipping", SOFT_SKILLS_MAPPING)
        return

    mapping = pd.read_csv(SOFT_SKILLS_MAPPING)
    cur = con.cursor()

    # Step 1 — reclassify technical skills to required
    to_remove = mapping.loc[
        mapping["canonical"].str.startswith("REMOVE", na=False), "variant"
    ].tolist()
    if to_remove:
        placeholders = ",".join("?" * len(to_remove))
        cur.execute(
            f"UPDATE skills SET skill_type='Required', canonical=NULL, category=NULL "
            f"WHERE skill_type='Soft' AND skill IN ({placeholders})",
            to_remove,
        )
        logger.info("Reclassified %d technical skill rows from soft → required", cur.rowcount)

    # Step 2 — populate canonical, type and category for soft skills
    keep = mapping[~mapping["canonical"].str.startswith("REMOVE", na=False)]
    for _, row in keep.iterrows():
        cur.execute(
            "UPDATE skills SET canonical=?, type='Concept', category=? WHERE skill_type='Soft' AND skill=?",
            (row["canonical"], row["category"], row["variant"]),
        )

    con.commit()
    logger.info("Soft skills canonical/type/category populated")


def _apply_tech_skills_mapping(con: sqlite3.Connection) -> None:
    """
    Using tech_skills_mapping.csv, populate canonical, type, and category
    for all required and nice_to_have skill rows.
    """
    if not TECH_SKILLS_MAPPING.exists():
        logger.warning("Tech skills mapping not found at %s — skipping", TECH_SKILLS_MAPPING)
        return

    mapping = pd.read_csv(TECH_SKILLS_MAPPING)
    cur = con.cursor()

    for _, row in mapping.iterrows():
        cur.execute(
            """UPDATE skills SET canonical=?, type=?, category=?
               WHERE skill_type IN ('Required', 'Nice to have') AND skill=?""",
            (row["canonical"], row["type"], row["category"], row["variant"]),
        )

    con.commit()
    logger.info("Tech skills canonical/type/category populated")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_to_db(csv_path: Path = ENRICHED_PATH, db_path: Path = DB_PATH) -> None:
    """Load enriched CSV data into the SQLite database."""
    if not csv_path.exists():
        logger.error("Enriched CSV not found at %s — run enricher.py first", csv_path)
        return

    df = pd.read_csv(csv_path)
    logger.info("Loaded %d rows from %s", len(df), csv_path)

    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # Create schema
    for statement in _DDL.strip().split(";"):
        statement = statement.strip()
        if statement:
            cur.execute(statement)
    con.commit()

    inserted_jobs = 0
    skipped_jobs = 0

    for _, row in df.iterrows():
        try:
            company_id = _upsert_company(
                cur,
                name=_safe_str(row.get("company")),
                linkedin_url=_safe_str(row.get("company_url")),
                industry=_safe_str(row.get("industry")),
                size_estimate=_safe_str(row.get("size_estimate")),
            )

            job_id = _insert_job(cur, row, company_id)
            if job_id is None:
                skipped_jobs += 1
                continue

            required_skills = _parse_json_list(row.get("required_skills"))
            nice_to_have = _parse_json_list(row.get("nice_to_have"))
            soft_skills = _parse_json_list(row.get("soft_skills"))

            _insert_skills(cur, job_id, required_skills, nice_to_have, soft_skills)

            inserted_jobs += 1
        except Exception:
            logger.exception("Failed to process row: %s", row.get("job_url"))

    con.commit()

    _apply_soft_skills_mapping(con)
    _apply_tech_skills_mapping(con)

    con.close()

    logger.info(
        "Database loaded — jobs inserted: %d | skipped (duplicates/errors): %d | DB: %s",
        inserted_jobs,
        skipped_jobs,
        db_path,
    )


if __name__ == "__main__":
    load_to_db()
