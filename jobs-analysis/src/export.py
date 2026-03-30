"""
export.py — Export SQLite tables to analysis-ready flat CSVs for Tableau Public.

Generates:
  data/exports/jobs_flat.csv      — one row per job with company fields joined
  data/exports/skills_flat.csv    — one row per skill with job + company fields joined
  data/exports/companies_flat.csv — one row per company with job count
"""

import logging
import sqlite3
from pathlib import Path

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
DB_PATH = PROJECT_ROOT / "jobs.db"
EXPORT_DIR = PROJECT_ROOT / "data" / "exports"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_JOBS_FLAT = """
SELECT
    j.id                AS job_id,
    j.title,
    j.company_id,
    c.name              AS company,
    c.linkedin_url      AS company_linkedin_url,
    c.industry,
    c.size_estimate,
    j.location,
    j.date_posted,
    j.job_type,
    j.remote_policy,
    j.max_salary,
    j.years_experience,
    j.seniority,
    j.seniority_source,
    j.job_url
FROM jobs j
LEFT JOIN companies c ON j.company_id = c.id
"""

_SKILLS_FLAT = """
SELECT
    j.id                AS job_id,
    j.title,
    c.name              AS company,
    c.industry,
    c.size_estimate,
    j.location,
    j.date_posted,
    j.remote_policy,
    j.seniority,
    s.skill,
    s.skill_type,
    s.canonical,
    s.type              AS skill_kind,
    s.category          AS skill_category
FROM skills s
JOIN jobs j   ON s.job_id = j.id
LEFT JOIN companies c ON j.company_id = c.id
"""

_COMPANIES_FLAT = """
SELECT
    c.id                AS company_id,
    c.name              AS company,
    c.linkedin_url      AS company_linkedin_url,
    c.industry,
    c.size_estimate,
    COUNT(j.id)         AS job_count
FROM companies c
LEFT JOIN jobs j ON j.company_id = c.id
GROUP BY c.id, c.name, c.linkedin_url, c.industry, c.size_estimate
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def export(db_path: Path = DB_PATH, export_dir: Path = EXPORT_DIR) -> None:
    """Run all export queries and write CSVs."""
    if not db_path.exists():
        logger.error("Database not found at %s — run db.py first", db_path)
        return

    export_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)

    exports = {
        "jobs_flat.csv": _JOBS_FLAT,
        "skills_flat.csv": _SKILLS_FLAT,
        "companies_flat.csv": _COMPANIES_FLAT,
    }

    for filename, query in exports.items():
        try:
            df = pd.read_sql_query(query, con)
            path = export_dir / filename
            df.to_csv(path, index=False)
            logger.info("Exported %d rows → %s", len(df), path)
        except Exception:
            logger.exception("Failed to export %s", filename)

    con.close()


if __name__ == "__main__":
    export()
