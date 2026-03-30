# Data Analyst Jobs Analysis

A portfolio project that scrapes, enriches, and analyses ~1,000 Data Analyst
job postings from LinkedIn UK, then visualises the results in Tableau Public.

## Project structure

```
jobs-analysis/
├── data/
│   ├── raw/              # CSV output from scraper (git-ignored)
│   └── enriched/         # CSV after NLP enrichment (git-ignored)
├── notebooks/
│   └── exploration.ipynb
├── src/
│   ├── scraper.py        # JobSpy collection script
│   ├── enricher.py       # NLP extraction pipeline (regex + LLM)
│   ├── db.py             # SQLite loader
│   └── classify.py       # Standalone field classifier
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Setup

### 1. Create and activate the virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
# Edit .env and paste your Anthropic API key
```

Get your key at [console.anthropic.com](https://console.anthropic.com).

## Pipeline

Run the steps in order:

```bash
# Step 1 — Scrape ~1,000 LinkedIn job postings
python src/scraper.py

# Step 2 — Enrich with regex + LLM (requires ANTHROPIC_API_KEY)
python src/enricher.py

# Step 3 — Load into SQLite
python src/db.py
```

## Output

| File | Description |
|------|-------------|
| `data/raw/jobs_raw.csv` | Raw scraped postings |
| `data/enriched/jobs_enriched.csv` | Enriched with skills, seniority, fields |
| `jobs.db` | Normalised SQLite database |

## Cost estimate

LLM enrichment uses `claude-haiku-4-5-20251001`. Estimated cost for 1,000
rows with two prompts each: **< £5**.

## Tech stack

- [JobSpy](https://github.com/Bunsly/JobSpy) — scraping
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) — LLM enrichment
- pandas — data wrangling
- SQLite — storage
- Tableau Public — visualisation
