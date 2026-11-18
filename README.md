# Toronto café review study

Small Second Cup / Tim Hortons / Starbucks review study inside Toronto only. Builds evidence for a case competition: store sample, recent Google reviews, topic sentiment, charts, map, and a short findings report.

This does **not** prove demand for a bar or forecast financial returns.

## Defaults (`config.json`)

- City: Toronto (municipal envelope; no Mississauga / Vaughan / etc.)
- 10 stores per brand (30 total), matched comparison sets
- Up to 30 newest reviews per store in the past 12 calendar months
- Pilot: 3 stores × 5 reviews
- Usage cap: CAD 5 (conservative local accounting)
- Runtime: 6 hours, last 15 minutes reserved for export
- Gemini low-cost / free-tier model candidates; Outscraper API collection

## Setup

```bash
cd "/Users/christinahu/Projects/Data Scraping for UTCC Case Comp"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Put keys only in `.env` (never in chat):

```dotenv
OUTSCRAPER_API_KEY=...
GEMINI_API_KEY=...
```

Create accounts yourself:

1. [Outscraper](https://outscraper.com/) → dashboard → **API & Integrations → API Key** (or profile API token). Check [pricing](https://outscraper.com/pricing/). A deposit/card, if demanded for API access, is separate from the CAD 5 usage cap — do not auto-fund.
2. [Google AI Studio API key](https://aistudio.google.com/apikey). Prefer free tier; check [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

## Commands

```bash
python run.py preflight
python run.py preflight --probe   # small bounded auth/pricing probes
python run.py pilot
python run.py overnight --resume
python run.py export              # no paid calls
```

If the Outscraper API is blocked, collect via the website and import:

```bash
python run.py import-export /path/to/export.json
python run.py export
```

### Overnight (separate Terminal)

Keep the Mac plugged in, lid open, networking on:

```bash
chmod +x scripts/run_overnight.sh
nohup ./scripts/run_overnight.sh > logs/launcher.log 2>&1 &
```

Inspect: `tail -f logs/run.log` and morning file `exports/STATUS.md`.  
Stop: find the python/caffeinate process and terminate it (`pkill -f "run.py overnight"`).

## Outputs

```text
exports/STATUS.md
exports/findings.md
exports/findings.html
exports/map.html
exports/brand_ratings.png|.svg
exports/topic_comparison.png|.svg
exports/area_comparison.png|.svg
exports/stores.csv
exports/reviews.csv
exports/topics.csv
exports/summary_tables.csv
```

## Tests

```bash
python -m unittest tests/test_logic.py
```

## Limits

- Newest-first caps are not a full year of reviews.
- Selection is purposive; area/university groups can be thin and university overlaps geography.
- Topic labels are model-assisted and can be wrong; coverage is reported.
- Map tiles need internet; CSVs/charts work offline.
