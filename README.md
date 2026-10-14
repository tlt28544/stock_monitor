# Weekly Equity Research Pipeline

Python 3.11+ research pipeline for TSLA, AMZN, META, and configurable larger watchlists. Deterministic Python manages collection, event screening, bounded worker concurrency (default 4), checkpoints, valuation arithmetic, rendering, and validation. An OpenAI-compatible structured JSON API supplies analytical interpretations only. EODHD supplies news; Tavily search is an explicitly incomplete fallback. SQLite persists jobs, articles, events, ingestion watermarks, execution history, API accounting, and analyst-owned valuation references.

## Install and validate

Use the existing checkout in `/workspace/stock_monitor`; cloud tasks already have isolated environments, so no additional worktree is needed.

```bash
cd /workspace/stock_monitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
```

`requirements-lock.txt` pins all installed runtime and test dependencies. The smaller `requirements.txt` and `requirements-dev.txt` describe direct dependencies. The application uses Linux/POSIX file locking; local Windows execution requires a POSIX environment such as WSL. SQLite should reside on local disk, not a network filesystem. No database service or web server is required.

## Offline three-stock demonstration

```bash
.venv/bin/python orchestrator.py --all --date 2026-10-10 --demo
.venv/bin/python orchestrator.py --status --date 2026-10-10 --demo
.venv/bin/python orchestrator.py --resume --date 2026-10-10 --demo
```

Demo mode makes **zero API calls**. Its fabricated fixtures and interpretations are conspicuously labeled **SYNTHETIC DEMO - NOT INVESTMENT RESEARCH**. It uses `state/demo.sqlite3` and `reports/demo/`, separate from live research. Every demo report concludes with a valuation table containing N/A until analyst inputs exist in that database. A partial outcome and exit code 2 are expected even when all demo PDFs validate; synthetic coverage and missing valuations are disclosed, never described as real research.

## Live configuration and credentials

Edit nonsecret settings in `config/settings.json`. Expand `watchlist` to 36 or more symbols without changing the pipeline. Configure `concurrency`, `full_backfill_days` (28), overlap days (3), retries (3 with exponential backoff), timeouts (30 seconds), news-request limits, run-wide token/cost ceilings, and actual contracted provider/model prices. The model price defaults are **conservative configuration examples, not a verified price quotation**. Set them at least as high as your actual rates before paid use. `news_request_cost_usd` defaults to zero for subscription-covered EODHD requests; configure your marginal cost if applicable. Tavily's configured request cost is an example ceiling and must be verified against the selected search plan.

Securely bind these environment variables; never put their values in config files, source, commands saved to history, or chat:

| Variable | Purpose | HTTPS destination |
| --- | --- | --- |
| `EODHD_API_KEY` | EODHD financial news API | `eodhd.com` |
| `TAVILY_API_KEY` | Optional web-search fallback when EODHD fails or has no usable data | `api.tavily.com` |
| `LLM_API_KEY` | Structured JSON synthesis | `api.openai.com` by default |

The environment currently has none of these bindings. `llm_base_url`, `llm_model`, and variable **names** are configurable. The endpoint must implement `/chat/completions`, strict `response_format=json_schema`, `max_completion_tokens`, and token usage reporting. A different model/endpoint needs its own pricing and tokenizer-envelope validation. OpenAI's byte-based tokenizers are the supported budget assumption. HTTP redirects are disabled for authenticated API calls. TLS verification is enabled. Requests and raw errors containing keys are not logged.

Allow the exact HTTPS destinations above in cloud environment settings. Cloud proxy secret bindings must support the adapter's credential placement (EODHD query parameter, Tavily JSON body, LLM Authorization header); validate the bindings with a live run. Do not extract injected credentials. No paid live API request is part of the offline demo or test suite.

Provider references: [EODHD Financial News API](https://eodhd.com/financial-apis/stock-market-financial-news-api), [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search). Live compatibility and subscription entitlements remain to be verified with your account.

An alternative `news_provider: "file"` with `news_file` supports analyst-provided news arrays using the `Article` fields in `equity/models.py`: provider, article_id, ticker, title, summary, URL, timezone-aware publication/retrieval timestamps, and optional event_id. File completeness is explicitly unverified. Provider implementations conform to `NewsProvider.fetch(ticker, start, end)` and return validated articles plus coverage warnings. Custom providers can include company announcements, earnings, regulatory, competition, products, and sector news from additional sources without changing orchestration.

## CLI and outputs

Activate the venv (`source .venv/bin/activate`) to use the requested CLI directly:

```bash
python orchestrator.py --tickers TSLA AMZN META --date 2026-10-10
python orchestrator.py --all --date 2026-10-10
python orchestrator.py --resume --date 2026-10-10
python orchestrator.py --status --date 2026-10-10
```

The report date is inclusive. The exact window is midnight UTC on `date - 89 days` through midnight UTC following the report date, giving 90 calendar days. Future report dates are rejected by the CLI. A first ingest and each periodic backfill retrieves the full window; weekly incremental ingestion overlaps the last watermark. Incomplete backfills do not advance the successful-full-backfill date, so the next report retries full coverage. EODHD pages use limit/offset; malformed records are rejected with an incomplete-coverage disclosure, and an empty full window fails. Tavily results need real publication dates; undated items are rejected, not assigned invented dates. Persisted articles preserve provider IDs, URLs, publication/retrieval timestamps, and source excerpts. No fabricated article text or financial forecasts are used in live mode.

For default version `v1`:

```text
reports/YYYY-MM-DD/TICKER.pdf
reports/YYYY-MM-DD/TICKER.md
reports/YYYY-MM-DD/TICKER.json
reports/YYYY-MM-DD/status.json
reports/YYYY-MM-DD/failed_tickers.json
reports/YYYY-MM-DD/completion_summary.md
state/research.sqlite3
```

Companion JSON contains research, event/source provenance, valuation calculations, analyst rationale, coverage notes, and review flags. Full source article content (up to the configured schema limit) is retained in SQLite; event/report content uses bounded excerpts. PDF facts are labeled source excerpts; analytical implications are labeled model interpretation. Source URLs are clickable. A bounded readable layout creates exactly two pages; oversized content fails rather than silently spilling, dropping valuation, or shrinking excessively. Valuation is the concluding table on page two. Markdown/JSON provide additional detail.

Exit codes: **0** complete (or successful `--status` query), **2** partial/failed/interrupted research run, **1** configuration/runtime/lock failure. Always inspect status JSON rather than interpreting exit code 2 as total failure. A job is `complete` only after its two-page PDF, required sections, link annotations, and PDF/Markdown/JSON hashes validate. The overall run is `partial` when any job failed/is unfinished, news coverage is incomplete, or valuation inputs are missing/stale. A `failed` run has no validated jobs and all jobs failed. Every persisted ticker for the date/version is included in the final summary, including stocks added in earlier invocations. The completion console output and summary are produced only after all statuses and artifacts have been checked. No email/chat notification destination is configured or contacted.

## Event screening and factual validation

Events are grouped conservatively by shared URLs, provider event IDs, identical/near-identical headlines within three days, or highly similar syndicated excerpts. Different headlines about the same event can still escape heuristic deduplication; genuinely distinct events can also need analyst review. There is no claim of perfect semantic deduplication. Analyst-provided `event_id` can resolve ambiguous clusters. Deterministic category/materiality screening prioritizes earnings, regulatory, competitive, execution and product/industry developments. It is a transparent screening heuristic, not a fundamental impact model.

The model sees only selected events and read-only valuation scope/rationale. Its structured schema requires known event citations for every finding. Automated validation rejects malformed JSON, unknown/missing citations, and new numeric assertions unsupported by cited excerpts. It does **not** prove semantic entailment or eliminate qualitative hallucinations. Check source facts and interpretations before investment use. Ticker-tagged news may miss untagged competitor/industry stories; EODHD reports disclose that limitation. Tavily search cannot establish exhaustive quarter coverage and always produces an incomplete-coverage warning. These limits are visible in report and run status rather than hidden by a successful PDF generation.

## Analyst-owned valuation engine

Valuation inputs are independent of summaries in the SQLite `valuation_reference` table. Only the explicit analyst importer writes revisions. Research never overwrites them. Incomplete templates in `config/valuation_templates.json` are **not approved**, contain null financial values, and are not imported automatically. Copy a template outside the tracked config, fill inputs with cited sources and dates, name the approving analyst, and approve/review it only after confirming economic coverage and adjustments:

```bash
.venv/bin/python -m equity.valuation --import-file /path/to/analyst-reference.json
# For demo-only testing, specify --db state/demo.sqlite3 instead.
```

Imports append immutable historical revisions. Each report version checkpoints the reference revision it used. A changed analyst reference requires a **new report version** to change an already generated report; it cannot silently rewrite history.

All monetary drivers, adjustments, and diluted shares use **millions in one declared currency**. Prices use currency per share. Numbers are decimal strings and deterministic Python `Decimal` performs arithmetic:

- P/E: forward net income × selected multiple. If net income is absent, derive it from revenue × net margin; inconsistent supplied drivers suppress valuation. Nonpositive net income cannot be valued with P/E.
- P/S: forecast revenue × multiple. Specify equity or enterprise basis. Enterprise values require explicit allocated net debt (including a sourced zero) to convert to segment equity.
- Peak sales: estimated peak revenue × multiple × probability of success × present-value discount factor. Neither probability nor discount defaults to 1. Specify equity/enterprise basis and net-debt treatment.
- `direct_equity`: analyst-supplied equity value for a documented company-specific exception; Python still adds adjustments and calculates prices/upside.
- Total equity: sum every available segment **equity** value and signed unallocated equity adjustments. If any necessary segment/adjustment input is missing, total equity is N/A. Missing diluted shares suppress implied price; missing dated current price suppress upside/downside.
- Implied price: total equity / diluted shares. Upside/downside: (implied price / current price − 1) × 100.

For multiple economically distinct businesses with reliable estimates, use `sotp`. Otherwise choose an explicitly justified consolidated or custom method. AMZN's template separates AWS, Advertising, Retail, and named emerging assets; Advertising must be carved out of Retail, and shared costs must be allocated or capitalized exactly once. MSFT initially uses consolidated forward P/E. META separates Core Business from the requested Muse AI opportunity, but the template **does not assert that Muse is a verified business**: establish the opportunity's existence and boundary with sources, exclude its economics from Core, and leave it N/A until supported.

P/E produces equity value: do not subtract the same financing debt again. If adding separately valued net cash, remove associated interest income from segment earnings to avoid counting both cash and its capitalized earnings. P/S/peak-sales enterprise values subtract allocated net debt once; global net cash/debt adjustments cover **unallocated amounts only**. `corporate_cost` adjustments are negative **capitalized values**, not annual expenses, and must be excluded from segment earnings if separately deducted. The importer enforces signs, unique segments/adjustments, and method/basis rules. Analysts must explicitly set `overlap_reviewed` and `adjustments_reviewed`, documenting reconciliation in coverage/adjustment notes. Software cannot independently prove an allocation is economically nonoverlapping; incomplete approval/review gates result in N/A.

Every reference/segment/adjustment records source and last-updated date; common reference provenance covers share count and quote inputs, with a separate timezone-aware quote timestamp. References older than 45 days or prices older than 7 days are highlighted (configurable). Future-dated inputs relative to report date are rejected from calculated outputs to avoid look-ahead. Material earnings/regulatory/competition events produce review flags, and the model may recommend review; neither changes approved inputs. Reports conclude with Fundamental Driver, Valuation Driver, Implied Segment Value, Total Equity Value, Implied Price per Share, Upside/Downside, and Valuation As-of Date.

## Recovery, idempotency, and budgets

Identity is `(ticker, report_date, report_version)`. Collection, classification, synthesis, valuation, and rendering have persistent checkpoints. API spend is reserved atomically **before** each paid attempt. Known LLM usage reconciles the reservation; uncertain timeouts retain the full reservation because the provider may have billed them. Reservations and budgets survive restarts. Budget ceilings are per date/version across all tickers, retries, and resumed invocations; a new version intentionally has a new budget. News subscription costs are counted using your configured per-request rate. Actual provider billing must still be reconciled externally. Concurrent reservations cannot overspend the configured envelope; an endpoint exceeding its reserved token envelope is recorded and fails validation.

Only temporary transport, timeout, HTTP 408/429/5xx failures retry automatically, with exponential backoff. Missing bindings, entitlement failures, invalid source/LLM data, and budget exhaustion fail the stock independently. Other stocks continue. Error types/reasons and each stage transition persist in SQLite; console logs omit credential-bearing requests/raw exceptions. Failed jobs can be retried with `--resume` after fixing the cause. Expensive successful stages are reused. If the source data or settings must change, use a new version instead of manually deleting database rows:

```bash
python orchestrator.py --all --date 2026-10-10 --version v2
```

Nondefault versions write to `reports/versions/VERSION/YYYY-MM-DD/` so they cannot overwrite v1 artifacts. Settings, demo/live mode, and output location are fingerprinted; resuming with different settings under the same date/version is rejected. Keep a configuration copy for old-version recovery. Valid completed artifacts are skipped; missing/corrupt artifacts regenerate from cached research/valuation without new API calls. Atomic same-directory writes and hashes protect final files. PDF failures never mark jobs complete and do not replace an existing valid PDF.

One POSIX lock serializes orchestrators sharing a database; the first process still runs tickers concurrently. A duplicate active invocation exits 1 without running jobs; a later duplicate is idempotent. Use a single authoritative database and output root; separate databases writing the same output directory are unsupported. SIGINT/SIGTERM stop at stage boundaries after any in-flight bounded request. Even a hard kill leaves persisted checkpoints recoverable. Wait for shutdown before launching resume. Back up SQLite with its online backup API (or after stopping the process), including analyst history; copying only the main file during WAL activity is unsafe.

## Proposed Saturday production operation — not registered

OpenClaw was not installed/discoverable in the inspected environment. No OpenClaw configuration was changed and **no recurring production job was registered**. The offline MVP and mocked live-adapter tests are validated; paid live compatibility, approved analyst references, coverage, account budgets, and the actual OpenClaw runtime still need validation before unattended investment use.

After those prerequisites are met, propose Saturday **08:00 Asia/Shanghai** (`0 8 * * 6` in that timezone) using your installed OpenClaw scheduler's documented command-job interface. Do not invent or copy an unverified OpenClaw registration command. Have it execute this wrapper from the checkout with secure API bindings:

```bash
/workspace/stock_monitor/scripts/run_weekly.sh
# Explicit date for a missed Saturday/recovery:
/workspace/stock_monitor/scripts/run_weekly.sh 2026-10-10
```

The wrapper computes its default calendar date in Asia/Shanghai and executes the one-command `--all` workflow. Set the scheduler's timezone independently; shell date timezone alone does not determine trigger time. Route exit-code 2 to a **partial/failed** alert using `status.json`, never a generic completion message. Read every job status before notification, and include failed tickers/reasons and data limitations. Production scheduling should follow a successful live three-stock run and source/valuation review; leave unrelated OpenClaw jobs/configuration untouched.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Mocked APIs require no paid requests. Tests cover rate limits/timeouts, permanent failures, pagination/fallback, malformed/undated/empty news, syndicated duplicates, invalid JSON/citations/unsupported numeric assertions, independent partial success, interrupted recovery, graceful stop, duplicate scheduling, artifact corruption, atomic PDF failure, immutable valuation checkpoints, incremental/full backfills, concurrent persistent budgets, approved and missing valuation drivers, stale/look-ahead inputs, overlap gates, enterprise-to-equity adjustment treatment, clinical peak sales, EODHD-to-LLM live adapters end to end and Tavily fallback, and a 36-stock workload.
