"""Monthly news -> LLM narrative -> PDF, run weekly with resumable checkpoints."""
import fcntl
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

from .adaptive_providers import AdaptiveTavilyNews, company_name
from .demo import DemoNews, DemoResearch
from .models import DataQualityError, DeepResearch, TemporaryAPIError, PermanentAPIError, InterruptedRun, report_window
from .narrative_research import NarrativeResearch, validate_research
from .narrative_reports import render_narrative, validate_artifacts, atomic_write
from .store import Store

log = logging.getLogger('equity')


@contextmanager
def run_lock(path):
    with Path(str(path) + '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another orchestrator owns this database') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Pipeline:
    def __init__(self, settings, db, output, report_date, version='v2', demo=False,
                 news_factory=None, research_factory=None, renderer=render_narrative, progress_callback=None):
        self.settings, self.store = settings, Store(db)
        self.date, self.version, self.demo = report_date, version, demo
        self.output = Path(output).resolve()
        if version != 'v2':
            self.output = self.output / 'versions' / version
        self.start, self.end = report_window(report_date)
        self.news_factory, self.research_factory = news_factory, research_factory
        self.renderer, self.progress_callback = renderer, progress_callback
        self.stop = threading.Event()

    def progress(self, completed, ticker=None, total=None):
        if self.progress_callback:
            try:
                self.progress_callback(completed, ticker, total=total)
            except Exception:
                log.warning('Progress callback failed; report state is unaffected')

    def worker(self, ticker):
        d, v, stage, notes = self.date.isoformat(), self.version, 'news', []
        providers = []
        try:
            def check_stop():
                if self.stop.is_set():
                    raise InterruptedRun()

            def load(name):
                return self.store.cached(ticker, d, v, name)

            def save(name, payload):
                self.store.checkpoint(ticker, d, v, name, payload)

            check_stop()
            self.store.mark(ticker, d, v, 'running', stage)
            news = load('news')
            if news is None:
                provider = self.news_factory(ticker) if self.news_factory else (
                    DemoNews(self.settings) if self.demo else AdaptiveTavilyNews(self.settings))
                providers.append(provider)
                news = provider.fetch(ticker, self.start, self.end, load, save, self.stop)
                save('news', news)
            articles, notes = news['articles'], news['notes']
            check_stop()
            stage = 'narrative'
            self.store.mark(ticker, d, v, 'running', stage, notes=notes)
            raw = load('narrative')
            if raw is not None:
                research = validate_research(raw, articles)
            elif not articles:
                research = DeepResearch(narrative_blocks=[])
                save('narrative', research.model_dump())
            else:
                provider = self.research_factory(ticker) if self.research_factory else (
                    DemoResearch() if self.demo else NarrativeResearch(self.settings))
                providers.append(provider)
                research = validate_research(provider.synthesize(ticker, articles, self.start, self.end, self.stop), articles)
                save('narrative', research.model_dump())
            check_stop()
            stage = 'pdf'
            self.store.mark(ticker, d, v, 'running', stage, notes=notes)
            artifacts = self.renderer(self.output / d, ticker, company_name(ticker), d, v,
                                      self.start, self.end, articles, research, notes, news['searches'], self.demo)
            if not validate_artifacts(artifacts, ticker, articles):
                raise DataQualityError('Report artifact validation failed')
            self.store.mark(ticker, d, v, 'complete', 'validated', notes=notes, artifacts=artifacts)
        except InterruptedRun:
            self.store.mark(ticker, d, v, 'interrupted', stage, notes=notes)
        except Exception as exc:
            safe = isinstance(exc, (DataQualityError, TemporaryAPIError, PermanentAPIError))
            error = str(exc) if safe else type(exc).__name__
            self.store.mark(ticker, d, v, 'failed', stage, error=error, notes=notes)
            log.error('%s failed at %s: %s', ticker, stage, error)
        finally:
            for provider in providers:
                if hasattr(provider, 'close'):
                    provider.close()

    def status(self):
        jobs = self.store.jobs(self.date.isoformat(), self.version)
        for job in jobs:
            job['notes'] = json.loads(job['notes'])
            job['artifacts'] = json.loads(job['artifacts']) if job['artifacts'] else None
            news = self.store.cached(job['ticker'], self.date.isoformat(), self.version, 'news') or {}
            if job['status'] == 'complete' and not validate_artifacts(job['artifacts'], job['ticker'], news.get('articles', [])):
                job['status'], job['error'] = 'failed', 'Completed artifacts are missing or corrupt'
        completed = [j['ticker'] for j in jobs if j['status'] == 'complete']
        failed = [{'ticker': j['ticker'], 'stage': j['stage'], 'reason': j['error']} for j in jobs if j['status'] == 'failed']
        if not jobs:
            outcome = 'not_started'
        elif len(completed) == len(jobs):
            outcome = 'complete'
        elif len(failed) == len(jobs):
            outcome = 'failed'
        else:
            outcome = 'partial'
        return {'report_date': self.date.isoformat(), 'report_version': self.version, 'outcome': outcome,
                'window_start': self.start.isoformat(), 'window_end_exclusive': self.end.isoformat(),
                'total': len(jobs), 'completed': completed, 'failed': failed, 'jobs': jobs}

    def run(self, tickers):
        d, v = self.date.isoformat(), self.version
        with run_lock(self.store.path):
            config = self.settings.model_dump(exclude={'watchlist', 'concurrency', 'retries', 'retry_base_seconds', 'timeout_seconds'})
            names = json.loads((Path(__file__).resolve().parent.parent / 'config/company_names.json').read_text())
            fingerprint = hashlib.sha256(json.dumps({'algorithm': 'monthly-five-search-v2', 'settings': config,
                'company_names': names, 'demo': self.demo}, sort_keys=True).encode()).hexdigest()
            self.store.bind_run(d, v, fingerprint)
            tickers = list(dict.fromkeys(tickers))
            for ticker in tickers:
                self.store.ensure(ticker, d, v)
            pending = []
            for job in self.store.jobs(d, v):
                if job['ticker'] not in tickers:
                    continue
                artifacts = json.loads(job['artifacts']) if job['artifacts'] else None
                news = self.store.cached(job['ticker'], d, v, 'news') or {}
                same_output = artifacts and Path(artifacts.get('pdf', {}).get('path', '')).parent == self.output / d
                if job['status'] == 'complete' and same_output and validate_artifacts(artifacts, job['ticker'], news.get('articles', [])):
                    continue
                pending.append(job['ticker'])
            self.progress(0, total=len(pending))
            with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
                futures = {pool.submit(self.worker, ticker): ticker for ticker in pending}
                for count, future in enumerate(as_completed(futures), 1):
                    future.result()
                    if count % 4 == 0 or count == len(pending):
                        self.progress(count, futures[future])
            status = self.status()
            destination = self.output / d
            atomic_write(destination / 'status.json', json.dumps(status, indent=2) + '\n')
            atomic_write(destination / 'failed_tickers.json', json.dumps(status['failed'], indent=2) + '\n')
            lines = [f'# Run {d} ({v}): {status["outcome"].upper()}',
                     f'Validated reports: {len(status["completed"])}/{status["total"]}']
            lines.extend(f'- {j["ticker"]}: {j["status"]} | {j["error"] or "validated"}' for j in status['jobs'])
            atomic_write(destination / 'completion_summary.md', '\n'.join(lines) + '\n')
            return status
