"""
classify.py — LLM-based field classifier (reserved for future extension).

Currently, field classification is handled inline in enricher.py.
This module provides a standalone interface if a re-classification pass
is needed independently of the main enrichment pipeline.
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic
import pandas as pd
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

ENRICHED_PATH = PROJECT_ROOT / "data" / "enriched" / "jobs_enriched.csv"

MODEL = "claude-haiku-4-5-20251001"

_CLASSIFY_PROMPT = """\
You are a data extraction assistant. Given the job description below, \
classify the business domain this analyst role serves.

Return JSON only with this exact structure:
{{
  "fields": []  // 1 to 3 values from: data, product, marketing,
                //   finance, engineering, commercial, operations, other
}}

Job description:
{description}
"""


def _parse_llm_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = raw.rstrip("`").strip()
    return json.loads(raw)


def classify_fields(description: str, client: anthropic.Anthropic) -> Optional[list[str]]:
    """Return a list of field labels for a single job description."""
    if not isinstance(description, str) or not description.strip():
        return None
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(description=description)}],
        )
        data = _parse_llm_json(message.content[0].text)
        return data.get("fields")
    except Exception:
        logger.exception("Field classification failed")
        return None


def main() -> None:
    """Re-run field classification on the enriched dataset."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not set.")
        sys.exit(1)
    if not ENRICHED_PATH.exists():
        logger.error("Enriched data not found at %s", ENRICHED_PATH)
        sys.exit(1)

    df = pd.read_csv(ENRICHED_PATH)
    client = anthropic.Anthropic(api_key=api_key)

    df["fields"] = df["description"].apply(
        lambda d: json.dumps(classify_fields(d, client))
    )
    df.to_csv(ENRICHED_PATH, index=False)
    logger.info("Re-classified fields for %d rows. Saved to %s", len(df), ENRICHED_PATH)


if __name__ == "__main__":
    main()
