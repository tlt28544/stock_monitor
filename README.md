# Stock Monitor

Run weekly to create one company-news narrative PDF for each ticker in `config/settings.json`.
Each report covers the **30 calendar dates ending on the report date**, in Asia/Shanghai.

## The five Tavily searches

The existing search strategy and theme extraction are preserved:

1. Fixed strategic/product search: product launches, management changes, acquisitions, buybacks and insider buys.
2. Fixed business-fundamentals search: AI, innovation, partnerships, customers and competitive advantages.
3. Hot-topic discovery search.
4. Deep dive on the first theme extracted from searches 1–3.
5. Deep dive on the second theme extracted from searches 1–3.

Theme selection uses the original bigram frequencies, then single-word frequencies and generic fallbacks.
The first three searches request 15 results each; deep dives request 10 each. Dates are anchored to the
report date, including when rerunning a historical week. All five logical searches run even if some
return no usable articles. Temporary HTTP failures may retry the same search; completed searches are
checkpointed so resuming does not repeat them.

Results are filtered to the exact publication window and deduplicated. Missing or unparseable dates
are excluded, never replaced by today's date. ISO and RFC-style dates are accepted. The latest 30
unique articles by default (configurable up to 50) are passed to the LLM as source snippets. These
searches do not guarantee exhaustive coverage or confirm that an underlying event occurred this month.

The LLM writes source-cited narrative sections. Python validates the structure, citation IDs and
numeric assertions, then renders a readable PDF with a linked source appendix. There is no valuation,
market-price API, scoring pipeline or fixed page-count requirement. An empty month produces a clearly
labelled no-news PDF without asking the LLM to invent content.

## Setup

Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pytest -q
```

Supply `TAVILY_API_KEY` and `LLM_API_KEY` in the environment or a local `.env` file.
`OPENAI_API_KEY` is accepted as a fallback for the default `LLM_API_KEY` setting.
The current model is `gpt-4.1-mini` using the OpenAI-compatible Chat Completions API.
No EODHD key is needed. Do not commit `.env` or generated reports.

```bash
bash run_weekly.sh                         # all companies, today's Shanghai date
bash run_weekly.sh 2026-10-10               # historical publication window
bash run_with_env.sh --tickers TSLA META --date 2026-10-10
bash run_with_env.sh --resume --date 2026-10-10
bash run_with_env.sh --status --date 2026-10-10
```

Both root and `scripts/run_weekly.sh` work from any working directory. Set `STOCK_MONITOR_ENV_FILE`
for a different env file, or `STOCK_MONITOR_PYTHON` for a different Python executable. For compatibility,
the env loader also checks `/home/admin/.openclaw/.env` when there is no repo-local `.env`.

## Outputs and scheduling

Outputs are `reports/YYYY-MM-DD/TICKER.pdf`, `.md`, `.json`, plus `status.json`,
`failed_tickers.json` and `completion_summary.md`. The PDF and Markdown include matching citations;
JSON retains source snippets and the five query descriptions. Pages grow naturally with content.

Exit code **0** means every company has a validated report, **2** means some jobs failed or were
interrupted, and **1** means a configuration/runtime failure. Shell runners preserve these codes.
Coverage notes are informational and do not turn a successful report into a failed run.

Use an existing weekly OpenClaw/cron schedule to invoke `bash /absolute/path/run_weekly.sh`.
For standard cron, for example:

```cron
CRON_TZ=Asia/Shanghai
0 9 * * 6 bash /absolute/path/stock_monitor/run_weekly.sh >> /absolute/path/stock_monitor/weekly.log 2>&1
```

The program does not register or duplicate schedules. When OpenClaw delivery variables are present,
it emits `[PROGRESS_MESSAGE]` markers; delivery is performed by the caller, not by this program.

## Resume, versions and migration

SQLite stores only job status and search/narrative checkpoints. Failed searches resume at the missing
search; a PDF failure reuses existing news and LLM output. Valid completed artifacts are skipped using
the same validator used at completion. Missing or corrupt artifacts are regenerated from checkpoints.
Only one writer runs per database.

This simplified workflow defaults to **v2** and `state/monthly.sqlite3`, isolated from previous valuation
checkpoints. Old databases are not migrated or erased. Changes to the model, source selection or company
mapping require a fresh report version, for example `--version v3`; additional versions go under
`reports/versions/v3/`. Concurrency/retry changes and adding tickers can reuse the existing version.

## Offline demo

```bash
.venv/bin/python orchestrator.py --all --date 2026-10-10 --demo
```

Demo mode uses synthetic sources and narrative, makes no external calls, labels the PDF clearly, and
uses separate state/output. Tests cover five searches, the original theme logic, historical dates,
retries/checkpoint resume, malformed LLM output, artifact integrity, empty news, and shell exit codes.
