# 510k-browser — Project Plan

A static website, hosted on GitHub Pages, for browsing FDA 510(k) clearances, reading
their summary documents, and exploring the graph of devices and the predicates they
claimed substantial equivalence to. A scheduled GitHub Action keeps the data current.

Status: planning. No code yet. Items marked **[verify]** are assumptions to confirm
before building on them.

---

## 1. Goals and non-goals

### Goals
- Search every 510(k) since 1976 by K-number, device name, applicant, product code,
  and decision date.
- A device page that shows metadata, links to FDA's summary PDF, lists predicates and
  descendants, and shows related recalls.
- An interactive ancestry/descendant graph for any device.
- Every predicate edge is traceable: the page and text snippet it came from, how it was
  extracted, and a confidence score.
- Fully static hosting (GitHub Pages). No server and no database process.
- Automated updates without manual work, plus a lightweight path for humans to correct
  edges through pull requests.

### Non-goals (for now)
- Hosting or mirroring FDA PDFs. We link to FDA's copies.
- Full 510(k) submissions. Only summaries and statements are public; the rest requires FOIA.
- Regulatory advice. The site carries a clear "informational only, verify with FDA" disclaimer.
- Real-time updates. The data is at most about a week old.

---

## 2. Data sources

| Source | What it gives us | Cadence | Notes |
|---|---|---|---|
| openFDA `/device/510k` bulk download (download.open.fda.gov) | All 510(k) records: K-number, applicant, device name, product code, decision date/code, summary vs. statement flag, openFDA-harmonized fields | Monthly | Primary metadata source. **[verify]** current bulk file URL and schema via `https://api.fda.gov/download.json`. |
| FDA downloadable 510(k) files (`PMN96CUR.ZIP`, `PMNLSTMN.ZIP`, …) | Same records as fixed-width text | Monthly | Fallback and cross-check for openFDA. |
| FDA 510(k) database search (`accessdata.fda.gov/.../cfpmn/pmn.cfm`) | Per-device pages that link to the summary PDF; supports searching by decision-date range | Weekly **[verify]** | The only weekly source, so it's what makes a weekly cadence worthwhile (see §2.1). |
| 510(k) summary PDFs (`accessdata.fda.gov/cdrh_docs/pdfYY/KYYNNNN.pdf`) | Predicate references, intended use, device description | Per clearance | **[verify]** that the URL pattern holds across eras (older ones may live under other paths). |
| openFDA `/device/classification` | Product code → device name, class, panel, regulation number | Monthly | Join on product code. |
| openFDA `/device/recall` (and/or `/device/enforcement`) | Recalls linked to K-numbers | Weekly/monthly | **[verify]** which endpoint carries a usable K-number field. |
| FDA De Novo database download | De Novo (DEN) records | Monthly | Needed because predicates can be De Novos. |
| openFDA `/device/pma` | PMA records | Monthly | Predicates occasionally reference PMA devices. |
| Seed data: `wcedmisten/fda-510k-analysis` | ~152k extracted predicate edges (through early 2024) plus manual corrections | Static | Seeds the backfill. See §7 on licensing. |

### 2.1 Freshness caveat
openFDA and the bulk zip files update **monthly**, so a weekly job that relies only on
them would find new devices only once a month. Two options:

1. **Weekly job + monthly metadata (default).** Each week, query the FDA 510(k)
   database search for decisions in the past ~14 days to discover new K-numbers early.
   Fetch their PDFs, then reconcile with openFDA once it catches up.
2. **Monthly job.** Simpler, but up to ~5 weeks stale.

Start with option 1. If the search page scrape proves fragile, fall back to option 2.

---

## 3. Architecture overview

```
                ┌──────────────── GitHub Actions (cron) ────────────────┐
                │                                                        │
 openFDA bulk ──┤ 1. ingest metadata  ──► data/devices.parquet           │
 FDA search  ───┤ 2. discover new K#s ──► queue = with summary − processed│
 FDA PDFs   ────┤ 3. fetch (polite)   ──► PDF cache (actions/cache)      │
                │ 4. extract text     ──► text cache (actions/cache)     │
                │ 5. extract predicates ► state/edges.csv (committed)    │
                │ 6. build site data  ──► site/public/data/* (artifact)  │
                │ 7. build frontend + deploy to GitHub Pages             │
                └────────────────────────────────────────────────────────┘
                                 │
                                 ▼
               Static site: search · device page · graph view
             (browser loads JSON shards and walks the graph in JS)
```

What is stored where:
- **Committed to git (small, diffable, the source of truth):** `state/processed.csv`,
  `state/edges.csv`, `state/manual_overrides.csv`, `state/fetch_failures.csv`.
- **Rebuilt each run (not committed):** device metadata, site data shards, the frontend build.
- **Cached between runs (not committed):** PDFs and extracted text, in `actions/cache`.
  The cache is capped at 10 GB per repo and evicts entries unused for 7 days **[verify]**.
  Losing it is fine, because a PDF is never re-fetched once its K-number is in `processed.csv`.

---

## 4. Planned repository layout

```
510k-browser/
├── PLAN.md
├── README.md
├── LICENSE
├── pyproject.toml            # uv project; ruff + ty + pytest config
├── uv.lock
├── src/fda510k/              # Python pipeline package
│   ├── cli.py                # `uv run fda510k <stage>` entry points
│   ├── config.py             # rate limits, windows, paths, user agent
│   ├── sources/
│   │   ├── openfda.py        # bulk download + parse → polars
│   │   ├── fda_search.py     # weekly discovery via pmn.cfm search
│   │   ├── classification.py
│   │   ├── recalls.py
│   │   └── denovo_pma.py
│   ├── fetch.py              # polite PDF fetcher (rate limit, window, retries)
│   ├── extract_text.py       # pypdf → tesseract fallback
│   ├── predicates/
│   │   ├── candidates.py     # regex for K/DEN/P numbers + context windows
│   │   ├── classify.py       # predicate vs reference vs self vs noise
│   │   └── resolve.py        # validate IDs against known records
│   ├── graph.py              # merge edges + overrides, cycle checks, stats
│   └── build_site_data.py    # emit JSON shards for the frontend
├── tests/
│   ├── fixtures/             # small PDFs/text snippets with known predicates
│   └── ...
├── state/                    # committed pipeline state (CSV)
├── seed/                     # imported seed edges with provenance
├── site/                     # frontend (Vite + TypeScript)
└── .github/workflows/
    ├── ci.yml
    ├── update.yml
    └── backfill.yml
```

---

## 5. Pipeline stages in detail

All stages are idempotent CLI commands (`uv run fda510k <stage>`), so each can run
locally or in CI by itself.

### 5.1 `ingest` — device metadata
- Download the openFDA 510(k) bulk JSON. Parse it with polars into `devices.parquet`
  with typed columns: `k_number`, `decision_date`, `date_received`, `applicant`,
  `device_name`, `product_code`, `decision_code`, `clearance_type`,
  `statement_or_summary`, `advisory_committee`, `third_party`, `expedited`.
- Join classification data (device class, panel, regulation number).
- Ingest De Novo and PMA records into the same node table with a `node_type` column
  (`510k | denovo | pma | preamendment | unresolved`).
- Validate: K-number format, no duplicate keys, row-count sanity (alert on a >2% drop
  from the previous run).

### 5.2 `discover` — what needs fetching
- Queue = devices flagged `Summary` and not yet in `processed.csv`, plus any new
  K-numbers from the weekly FDA search scrape (§2.1).
- Order the queue newest first, so recent devices appear before backfill finishes.
- Skip items in `fetch_failures.csv` until they are due for retry (exponential backoff,
  up to N attempts, then marked `gave_up`).

### 5.3 `fetch` — polite PDF downloads
- **FDA's crawl rules (accessdata.fda.gov robots.txt, checked 2026-11-13):**
  - The paths we need are **allowed**: `/cdrh_docs/` (PDFs) and
    `/scripts/cdrh/cfdocs/cfpmn/` (510(k) database pages and search).
  - **Disallowed paths we must never touch:** `/scripts/cdrh/cfdocs/cftplc/` (TPLC),
    `/scripts/cdrh/devicesatfda/`, `/scripts/cdrh/*excel*.cfm` (no spreadsheet exports
    of search results), `/scripts/search/`, and the CLIA, RL and `cfres/textresults.cfm`
    paths. Recalls and lifecycle data come from openFDA, not from scraping.
  - `Hit-rate: 30`: wait 30 s between the starts of consecutive requests.
  - `Visiting-hours: 23:00EDT-05:00EDT`. Taken literally (EDT = UTC−4), this is
    **03:00–09:00 UTC all year**. We use that fixed UTC window rather than shifting
    it for EST.
  - `Concurrent-hits: 2`. We stay at **1 concurrent request**, because with a 30 s hit
    rate a second stream gains nothing and only adds risk.
  - These directives are non-standard. Python's `urllib.robotparser` understands
    `Disallow` but ignores `Hit-rate` and `Visiting-hours`, so `fetch.py` needs a small
    custom parser for them. Every run fetches robots.txt again and applies whatever it
    says. If it can't be parsed or the values get stricter, the run stops instead of
    falling back to defaults. The `Disallow` check runs on every URL before the request.
- Try the predictable PDF URL first. If it 404s, load the `pmn.cfm?ID=` page and follow
  the "Summary" link. That is one request in the common case instead of two.
- Send a descriptive User-Agent with a project URL and contact email.
- **Time budget:** stop starting new downloads when the window closes or the job's
  remaining time drops below a safety margin. Commit progress either way.
- **Throughput at a 30 s delay:** about 120/hour, or about 700 per 6-hour window
  (in practice about 650 after startup and a safety margin).
  - Steady state: about 60 new PDFs/week, roughly 30 minutes per run.
  - Backfill: about 9,000 PDFs (late 2023 through now), about 13 nightly runs.
- Handle errors: 404 means record `no_summary_pdf`; 403/429 means stop the whole run
  immediately, record it, and fail the workflow loudly (don't hammer); 5xx means retry
  with backoff.

### 5.4 `extract-text`
- Use `pypdf` for embedded text. Use `pytesseract` (Tesseract) for pages with
  little or no text.
- Store text per page with the extraction method and a quality signal (character count,
  dictionary-word ratio).
- Avoid PyMuPDF. It is AGPL-licensed, which would constrain this project's license (see §7).

### 5.5 `extract-predicates`
This is where we can do better than the existing dataset.

1. **Candidates.** Regex over the text for `K\d{6}` (allowing `K 123456`, `K#123456`,
   and common OCR confusions such as `O→0` and `l→1`), `DEN\d{6}`, and `P\d{6}`
   (with optional supplement `S\d{3}`). Capture about 300 characters of context and the page number.
2. **Drop obvious non-predicates.** Remove the device's own K-number, numbers not found
   in the node table (flag possible OCR errors for fuzzy matching instead), and numbers
   with a decision date after the subject device's receipt date.
3. **Classify** each remaining candidate as `predicate | reference | other`.
   - v1 rules: keywords near the match ("predicate", "substantially equivalent to",
     "primary predicate", "reference device", "previously cleared"), plus where the
     match sits in the document (an SE comparison table vs. the indications section).
   - v2, optional: a small LLM pass, only for ambiguous candidates. It runs in Actions
     with an API key secret, has a strict per-run budget, and caches results by
     (K-number, text hash) so nothing is classified twice.
4. **Name-only predicates.** When no number is present ("predicate: Acme Model X by Acme
   Corp"), record an `unresolved` edge with the name text. Optionally fuzzy-match against
   device name and applicant within the same product code, kept as low confidence.
5. **Output rows** for `state/edges.csv`: `device, predicate, predicate_type, role,
   confidence, method, page, snippet_hash, extracted_at, pipeline_version`.

### 5.6 `graph`
- Merge in this order: seed edges, then extracted edges, then `manual_overrides.csv`
  (which can add or remove edges), last write wins.
- Checks: self-loops, cycles (a device citing a later device suggests a parsing error,
  so flag it), and dangling references.
- Compute per-node stats: in/out degree, ancestry depth, number of recalled ancestors,
  and the earliest ancestor (a "root" that is preamendment or a De Novo).

### 5.7 `build-site-data`
See §8.2 for the format. Fail the build if any shard exceeds the size budget.

---

## 6. Data model (committed state)

`state/processed.csv`
| column | notes |
|---|---|
| k_number | primary key |
| status | `ok | no_summary_pdf | scanned_low_quality | gave_up` |
| source_url | the URL that was actually fetched |
| fetched_at | UTC timestamp |
| sha256 | of the PDF, so changed documents can be detected |
| text_method | `pypdf | ocr | mixed` |

`state/edges.csv` (see §5.5). One row per (device, predicate, method). The graph stage
dedupes.

`state/manual_overrides.csv`
| column | notes |
|---|---|
| device, predicate | |
| action | `add | remove` |
| reason | free text, required |
| author | GitHub handle |

`state/fetch_failures.csv`: `k_number, url, http_status, attempts, last_attempt, next_retry`.

---

## 7. Seeding and licensing

- The upstream repo `wcedmisten/fda-510k-analysis` is **AGPL-3.0**. Plan: **don't copy
  its code**; write the pipeline from scratch.
- Import `predicates.csv` and the manual-link CSVs into `seed/` with `method=seed_wcedmisten`
  and credit the source in the README and on the site's About page. Extracted facts are
  probably not copyrightable, but credit is the right thing to do either way.
  **[decide]** whether to ask the author first, which would be courteous and might lead
  to collaboration.
- Seed edges get re-validated by our own extractor whenever we have the PDF. Where they
  disagree, both are kept and the disagreement is surfaced for review.
- Project license **[decide]**: MIT or Apache-2.0 for code. Data is mostly US-government
  public domain, plus the derived edges (CC-BY-4.0 or CC0).

---

## 8. Frontend

### 8.1 Stack
- Vite + TypeScript. Plain TS or a light framework (Preact/Svelte) **[decide]**. The app
  is just three views, so a heavy framework isn't needed.
- Graph rendering: **Cytoscape.js with a layered (dagre/ELK) layout**, with nodes ordered
  by decision date. Ancestry is a DAG with a time direction, so a layered layout reads
  much better than a force-directed one. Offer a force layout as an alternate view.
- Hash routing (`#/device/K123456`) so deep links work on GitHub Pages without 404 hacks.

### 8.2 Static data format and size budget
About 175k nodes and ~150k+ edges.

| File | Contents | Est. size (gzip) |
|---|---|---|
| `data/meta.json` | build date, counts, pipeline version, data source dates | tiny |
| `data/ids.json` | ordered list of node IDs; array index = integer ID | ~1 MB |
| `data/graph.json` | CSR adjacency (offsets + targets) for both edge directions, plus per-edge role/confidence | ~1–2 MB |
| `data/search.json` | per node: id, name, applicant, product code, year (for the client-side search index) | ~4–6 MB, lazy-loaded |
| `data/nodes/K23.json` etc. | full metadata + per-edge provenance snippets, sharded by prefix and year | ~100–500 KB each |
| `data/recalls/*.json` | recalls sharded the same way | small |

- The browser loads `ids` and `graph` on first visit (about 3 MB), loads search data when
  the user starts typing, and loads node shards on demand.
- Traversal runs in JS: BFS with depth and node caps. Some descendant sets are huge
  (thousands of nodes), so the UI shows "+N more" and lets the user expand further.
- **Escape hatch:** if the JSON approach gets unwieldy, switch to a single SQLite/Parquet
  file queried with sql.js-httpvfs or DuckDB-WASM over HTTP range requests. GitHub Pages
  supports range requests **[verify]**.
- Stay well under Pages limits: 1 GB published site and 100 MB per file. Pages also has a
  soft 100 GB/month bandwidth limit, so a CDN (Cloudflare) may be needed if traffic grows.

### 8.3 Views
1. **Home and search.** Search box with filters (product code, panel, applicant, year
   range, has recalls), plus headline stats.
2. **Device page.** Metadata, a "View summary PDF on FDA" link, a predicates list (each
   with role, confidence and a snippet: "found on p.4: '…substantially equivalent to
   K201234…'"), a descendants list, recalls, and a "Report an incorrect link" button that
   opens a prefilled GitHub issue.
3. **Graph view.** Ancestors and descendants of the focal device, with a depth slider.
   Nodes are colored by recall status and node type. Edges are styled by confidence
   (dashed for low or unresolved).
4. **About.** Method, data sources, last-updated date, limitations, disclaimer, credits.

### 8.4 Linking to PDFs
Link out to FDA in a new tab. Don't embed in an iframe, because FDA may send
frame-blocking headers **[verify]** and linking out avoids mirroring issues.

---

## 9. GitHub Actions workflows

### `ci.yml` (every push and PR)
- `uv sync`, `ruff format --check`, `ruff check`, `ty check`, `pytest`.
- Frontend: typecheck, lint, build.
- Schema check: validate `state/*.csv` columns and run graph sanity checks, so a bad
  manual override fails the PR.

### `update.yml` (weekly cron, plus manual `workflow_dispatch`)
- FDA's crawl window is 03:00–09:00 UTC (see §5.3). The non-fetch stages
  (ingest, discover) run first and don't touch accessdata.fda.gov, so the job can
  start a little early. Cron: `45 2 * * 6` (Saturday 02:45 UTC). GitHub's scheduled
  start times are often delayed, so the fetcher itself waits until 03:00 UTC and
  stops starting requests at 08:55 UTC, whenever the job actually began.
- `timeout-minutes: 380`. That covers the window plus extraction, build and deploy
  after fetching ends.
- Steps: ingest, discover, fetch, extract-text, extract-predicates, graph,
  build-site-data, then commit `state/` changes as a bot (one commit per run with a
  summary: "+57 devices, +112 edges"), then build the site and deploy with
  `actions/upload-pages-artifact` + `actions/deploy-pages`.
- `concurrency: group: update, cancel-in-progress: false`, so runs never overlap.
- Use `actions/cache` for PDFs and text, keyed by run with restore-keys.
- On a 403/429 from FDA: fail, open or update a GitHub issue automatically, and skip
  fetching on later runs until a maintainer clears a flag.
- Note: GitHub disables scheduled workflows in repos with no activity for 60 days
  **[verify]**. The weekly state commit should keep it active.

### `backfill.yml` (nightly cron, temporary)
- The same job in "backfill mode", running nightly (`45 2 * * *`, same window rules) until the queue is empty, then
  disabled. It could be the same workflow with a schedule and an input flag.

### Deployment
- Pages source: "GitHub Actions" (not a branch), so site data never bloats git history.
- **Pages on a private repo requires a paid GitHub plan.** On a free account, the repo
  must be public to use Pages.

---

## 10. Key risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| FDA blocks GitHub-hosted runner IPs (Akamai/WAF) | Fetching impossible from Actions | **Test in milestone 1 before anything else.** Fallback: a self-hosted runner or a cheap VPS that runs only the fetch stage and pushes to `state/`. |
| FDA tightens its crawl rules (they were checked 2026-11-13) | Slower backfill | Read robots.txt on every run and stop if it can't be parsed or gets stricter; fetch newest first; the seed data covers history. |
| Accidentally requesting a disallowed path | Violates FDA's rules and risks a block | Check every URL against `Disallow` before the request; a unit test asserts that the URL builders produce only allowed paths. |
| PDF URL pattern doesn't hold for some eras | Missed PDFs | Fall back to the `pmn.cfm` page; track the hit rate of each pattern. |
| Weekly search scrape breaks (HTML changes) | Freshness drops to monthly | Rely on monthly openFDA as the baseline; alert on scrape failure. |
| Extractor false positives (reference devices counted as predicates) | Misleading graph | Role classification, confidence scores, visible snippets, and the issue-based correction flow. |
| OCR quality on old scans | Missed or wrong edges | OCR digit-confusion handling, validation against known IDs, low-confidence styling. |
| Actions cache eviction | Re-OCR needed for re-extraction | Keep extracted text compactly; optionally store text as a release asset per year. |
| Users treat the site as authoritative | Regulatory/legal exposure | Prominent disclaimer; every edge links to its source PDF. |

---

## 11. Testing strategy

- **Unit tests (pytest):** regex candidates (including OCR-mangled forms), classification
  rules, CSV schema round-trips, graph merge and override semantics, cycle detection,
  shard building.
- **Golden set:** about 100 hand-labeled summaries across eras (1990s scans, 2000s, modern
  eSTAR-era) with true predicate lists. Track precision and recall in CI. A regression
  beyond a threshold fails the build.
- **Network-free CI:** fixtures for openFDA JSON and FDA HTML pages; no live FDA calls in CI.
- **Live smoke test:** a manual `workflow_dispatch` job that fetches 3 known PDFs, to
  check reachability.
- **Frontend:** a Playwright smoke test against the built site with a tiny synthetic
  dataset (search, then device page, then graph renders).

---

## 12. Milestones

### M0 — Repo scaffolding
uv project, ruff/ty/pytest configs, CI workflow, README, LICENSE.
*Done when:* CI passes on an empty package.

### M1 — Reachability spike (do first; it decides the architecture)
The robots.txt parser (`robots.py`) and the paced client (`polite.py`) are built to
keep, since M4's fetcher reuses them. Only `fda510k probe` and
`.github/workflows/probe-fda.yml` are specific to M1. The probe sends 7 requests,
30+ s apart, only inside the 03:00–09:00 UTC window:
- robots.txt (checked against the last-known window before it is requested);
- five summary PDFs at their conventional URLs, one per URL era
  (`pdf23/`, `pdf15/`, `pdf5/`, and flat `pdf/` for 2001 and 1999);
- one `pmn.cfm` record page, whose "Summary" link is compared with the convention.

It writes a report (status, redirects, selected headers, `%PDF-` check, page titles)
and keeps the PDFs as a 7-day artifact. It stops at the first 403, 429 or block page.
Exit codes: 0 reachable, 1 error, 2 blocked, 3 refused (outside the window).

*Done when:* we know whether Actions can fetch from FDA directly or needs the
self-hosted fallback, and whether the PDF URL convention holds for each era.

### M2 — Metadata + seed graph
`ingest`, seed import, `graph`, and `build-site-data`, with no PDF fetching yet.
*Done when:* a complete node table and seed graph (through early 2024) exist as site data.

### M3 — Minimal frontend on Pages
Search, device page and graph view on the seed data, deployed by Actions.
*Done when:* a public URL lets you search a device and see its ancestry.

### M4 — Fetch + extract pipeline
`discover`, `fetch`, `extract-text` and `extract-predicates` (v1 rules), plus the
golden-set evaluation.
*Done when:* precision and recall on the golden set are at or above the agreed threshold
**[decide]**, and a local run on 50 recent devices produces edges.

### M5 — Automated backfill
Turn on `backfill.yml` nightly until the 2023–2026 gap closes, then disable it.
*Done when:* coverage of devices with summaries is at or above ~75% (the original
project's rate) for every year.

### M6 — Weekly updates + correction flow
`update.yml` weekly, plus the weekly discovery scrape, manual overrides, the "report
incorrect link" issue template, and failure alerting.
*Done when:* two consecutive unattended weekly runs succeed and deploy.

### M7 — Enrichment and polish
Recalls overlay, De Novo/PMA nodes, the optional LLM classifier, product-code views,
graph stats ("most-cited predicates", "longest chains"), and the About/method page.

---

## 13. Open questions / decisions

1. Public vs. private repo. Free-plan Pages requires public.
2. Project license for code and derived data.
3. Whether to contact the upstream author before using the seed data.
4. Frontend framework: plain TS vs. Preact vs. Svelte.
5. Whether to use an LLM classifier at all (cost, reproducibility, API key in Actions).
6. Custom domain or `<user>.github.io/510k-browser`.
7. Minimum confidence to draw an edge by default (show all, but style low confidence?).

---

## 14. Conventions

- Python ≥ 3.12, managed with **uv** (`uv run`, `uv add`); no pip.
- **ruff** for formatting and lint, **ty** for type checking, **pytest** for tests.
- **polars** for all tabular work (no pandas). Check current polars docs, since its API
  evolves.
- Type annotations everywhere with modern syntax (`X | None`, `list[str]`).
- Comments only where they explain a decision or non-obvious behavior.
- Pipeline outputs are deterministic: sorted CSVs, stable JSON key order, so that state
  diffs are reviewable.
