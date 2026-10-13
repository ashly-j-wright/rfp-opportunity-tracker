# RFP Opportunity Tracker

A Python tool that collects open public solicitations (RFPs, bids, and similar notices) from eight U.S. state procurement portals, scores each one against a keyword list you control, removes duplicates, and writes a clean Excel/CSV file for a person to review.

I built it to stop checking the same portals by hand when looking for AI, automation, and data-related state contracts. The tool narrows the list; a human makes the bid/no-bid decision.

> **What this is, and isn't:** the scoring is transparent keyword matching, not machine learning. It is a data-collection and triage tool. See [Roadmap](#roadmap) for where an LLM review step could fit.

---

## What it does

1. **Collects** current solicitations from each enabled portal (plain HTTP first, headless browser as a fallback for portals that need rendering).
2. **Parses** each listing into one consistent record (title, agency, solicitation ID, dates, description, link).
3. **Filters out** items that are closed, awarded, cancelled, or past their due date.
4. **Scores** each record against weighted keywords from `config.json`, with optional negative terms.
5. **De-duplicates** repeated records within each source.
6. **Writes** an Excel workbook (and a CSV) with qualified matches, everything pulled, a run summary, and an error log.

## Sources

| Connector | State | Portal | Method |
|---|---|---|---|
| `SC_SCBO` | SC | South Carolina Business Opportunities | HTTP, by category |
| `TX_ESBD` | TX | Texas SmartBuy ESBD | HTTP, paged |
| `PA_eMarketplace` | PA | PA eMarketplace | HTTP, detail pages |
| `FL_VBS` | FL | Florida Vendor Bid System | HTTP, browser fallback |
| `NJSTART` | NJ | NJSTART open bids | HTTP, browser fallback |
| `CO_VSS` | CO | Colorado VSS | Headless browser |
| `MD_eMMA` | MD | Maryland eMMA | Headless browser |
| `OH_OhioBuys` | OH | OhioBuys / procure.ohio.gov | Headless browser |
| `BROWSER_PROBE` | any | Optional keyword probe of pages you list | Headless browser (off by default) |

Each connector can be switched on or off in `config.json`.

## Design choices

- **Config-driven.** Keywords, weights, negative terms, score threshold, limits, enabled sources, and output paths all live in `config.json`. Retargeting the tool to a different topic requires no code changes.
- **Resilient by design.** One failing portal never stops the run. Each connector's errors are caught, recorded, and written to an `Errors` sheet so you can see what failed and why.
- **Fallbacks.** It tries lightweight HTTP requests first and only launches a headless browser (Playwright) when a page needs rendering.
- **Polite requests.** A configurable delay runs between requests, page and record counts are capped, and requests identify themselves with a User-Agent string.
- **Excel-safe output.** Portal text is cleaned before it is written, so control characters don't corrupt the workbook and text that begins with `=`, `+`, `-`, or `@` stays text instead of being read as a formula.
- **A single record type.** Every source is normalized into one `Opportunity` dataclass, which keeps scoring, de-duplication, and output uniform.

## Requirements

- Python 3.10 or newer
- Packages in `requirements.txt` (pandas, requests, beautifulsoup4, lxml, python-dateutil, openpyxl)
- Playwright plus a Chromium build, only for the connectors that render pages in a browser

## Setup

```bash
git clone <this-repo-url>
cd rfp-opportunity-tracker

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Only needed for browser-based connectors (CO, MD, OH, and some FL/NJ pages):
python3 -m playwright install chromium

cp config.example.json config.json  # then edit config.json
```

`config.json` and the `output/` folder are listed in `.gitignore`, so your settings and results stay on your machine.

## Run

```bash
python3 run_rfp_scraper.py
```

Results are written to the paths set in `config.json` (by default `output/rfp_results.xlsx` and `output/rfp_results.csv`).

## Configuration

| Setting | What it does |
|---|---|
| `keywords` | Terms to look for. Matching is case-insensitive and by substring. |
| `keyword_weights` | Points per matched keyword. Any keyword without a weight is worth 2. |
| `negative_terms` | Each match subtracts 1 point. |
| `min_fit_score` | Minimum score for a record to appear in **Qualified Matches**. |
| `include_all_current_records` | If `true`, every current record is treated as qualified. |
| `request_delay_seconds` | Pause between requests. |
| `max_pages_per_source` | Page limit for paged sources (TX). |
| `max_records_per_state` | Detail-page limit per source. |
| `browser_wait_seconds` | How long to wait for browser-rendered pages. |
| `scbo_categories` | South Carolina category IDs (`{"<id>": "<name>"}`). SC pulls nothing until this is filled in, which is why `SC_SCBO` is disabled in the example config. |
| `enabled_connectors` | List of `{name, enabled}` entries. |
| `enable_browser_probe_sources` / `browser_probe_sources` | Optional probe of pages you specify. |
| `output_excel`, `output_csv` | Output locations. |

**Tip on keywords:** because matching is by substring, avoid very short terms such as `ai`, which matches inside words like "maintain" or "email". Use whole phrases like `artificial intelligence` or `machine learning`.

## How scoring works

For each record, the title, agency, solicitation ID, status, description, and notes are combined into one lowercase string.

- Each configured keyword found adds its weight (default 2).
- Each negative term found subtracts 1.
- The score never goes below 0.

| Score | Recommendation |
|---|---|
| 8 or more | High fit - review immediately |
| 4 to 7 | Medium fit - review |
| 1 to 3 | Possible fit - quick screen |
| 0 | No keyword match |

Only records at or above `min_fit_score` reach the **Qualified Matches** sheet.

## Output

The workbook has four sheets:

| Sheet | Contents |
|---|---|
| **Run Summary** | Run time, counts of qualified and total records, number of errors, the score threshold, and which connectors ran |
| **Qualified Matches** | Records meeting the minimum score |
| **All Current Pulled** | Every current record collected, after de-duplication |
| **Errors** | Source, state, portal, and error message for anything that failed |

Columns in the record sheets: `scraped_at`, `source`, `state`, `title`, `agency`, `solicitation_id`, `description`, `detail_url`, `portal`. A CSV of the qualified matches is written alongside the workbook.

## Project structure

```
rfp-opportunity-tracker/
├── run_rfp_scraper.py      # connectors, parsing, scoring, de-duplication, output
├── config.example.json     # copy to config.json and edit
├── requirements.txt
├── .gitignore
└── README.md
```

## Validation status

Offline checks use synthetic records and the example configuration to verify scoring, de-duplication, and four-sheet Excel output. Live portal access has not been validated for this release. Connectors may need maintenance before use.

## Limitations

- **Portals change.** Each connector depends on a portal's current page structure, so connectors need occasional maintenance, and some will fail or return partial results without warning. The `Errors` sheet exists to surface this.
- **Keyword scoring is simple.** It can't judge context, so it will produce false positives and miss relevant listings that use different wording. Review the results.
- **Field extraction is heuristic.** Labels like "Due Date" or "Agency" are located with text patterns, and fields can occasionally come back long, blank, or misaligned on some portals. The label-extraction helper applies only the first stop pattern, which is a known rough edge I plan to fix.
- **De-duplication is source-specific.** The same opportunity listed by different portals is retained because the duplicate key includes source and state.
- **No automated tests yet.**
- **Not legal or bidding advice.** Always verify a solicitation on the official portal before acting on it.

## Responsible use

This tool reads publicly posted solicitations. If you run it:

- Review each portal's terms of use and `robots.txt` first, and keep the request delay and record limits conservative.
- The browser fallback tries to click common continuation prompts (for example "Continue" or "Public Access", but not "I Agree") so pages can render. Read what a portal's prompts say before relying on that behavior, and remove labels from the list in `get_playwright_html` if you don't want them clicked.
- Update the `USER_AGENT` string at the top of the script so it identifies your project. If you have a public project contact, you may include it; a personal email address is not required.
- Don't commit collected results. They can contain agency and contact details.

## Roadmap

- Fix the label-extraction stop-pattern handling and add unit tests with saved sample pages
- Add an optional LLM step that summarizes high-scoring listings, classifies fit, and flags risks, with a human reviewing the output and a comparison against the keyword score to measure how well each performs
- A per-source summary and trend view in the workbook
- Configurable output columns

## Built with

Python, pandas, requests, Beautiful Soup, Playwright, and openpyxl.

## Author

Ashly Wright · [LinkedIn](https://www.linkedin.com/in/ashly-j-wright/)
