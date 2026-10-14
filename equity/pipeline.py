import json
import hashlib
import logging
import signal
import threading
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from .models import Article, DataQualityError, TemporaryAPIError, PermanentAPIError, BudgetError
from .store import Store
from .providers import EODHDNews, FileNews, OpenAIResearch, DemoResearch
from .adaptive_providers import AdaptiveTavilyNews
from .narrative_research import NarrativeResearch
from .market_data import EODHDMarketData
from .demo import DemoNews
from .events import deduplicate, rank, validate_research
from .adaptive_events import deduplicate_adaptive, validate_research as validate_research_adaptive
from .valuation import calculate
from .reports import render, validate_artifacts, atomic_write
from .narrative_reports import render_narrative
from .narrative_validation import validate_narrative_artifacts

log = logging.getLogger('equity')


@contextmanager
def run_lock(path):
    path = Path(str(path)+'.lock'); path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another orchestrator owns this database; use --status or retry later') from None
        try: yield
        finally: fcntl.flock(lock,fcntl.LOCK_UN)


class Pipeline:
    def __init__(self, settings, db, output, report_date, version='v1', demo=False, news_factory=None, research_factory=None, renderer=render, progress_callback=None):
        self.settings, self.store, self.output = settings, Store(db), Path(output)
        self.date, self.version, self.demo = report_date, version, demo
        if version != 'v1':
            self.output = self.output / 'versions' / version
        self.news_factory, self.research_factory, self.renderer = news_factory, research_factory, renderer
        self.stop = threading.Event()
        self.progress_callback = progress_callback
        self.completed_count = 0
        self.progress_lock = threading.Lock()

    def collect(self, ticker):
        end = datetime.combine(self.date+timedelta(days=1),datetime.min.time(),tzinfo=timezone.utc)
        start = end-timedelta(days=90)
        previous = self.store.ingestion(ticker)
        full = not previous or not previous['full_date'] or (self.date-date.fromisoformat(previous['full_date'])).days>=self.settings.full_backfill_days
        # Historical reports must backfill, never inherit a future ingestion watermark.
        if previous and date.fromisoformat(previous['through_date'])>=self.date:
            full = True
        fetch_start = start if full else max(start, datetime.combine(date.fromisoformat(previous['through_date'])-timedelta(days=self.settings.incremental_overlap_days),datetime.min.time(),tzinfo=timezone.utc))
        def charge(operation,cost):
            ledger = self.store.reserve(self.date.isoformat(),self.version,ticker,operation,0,cost,self.settings)
            self.store.settle(ledger,0,cost)
        if self.news_factory:
            provider = self.news_factory(ticker)
        elif self.demo:
            provider = DemoNews(self.date)
        elif self.settings.news_provider=='file':
            if not self.settings.news_file: raise DataQualityError('news_file required for file provider')
            provider = FileNews(self.settings.news_file)
        elif self.settings.news_provider=='adaptive_tavily':
            provider = AdaptiveTavilyNews(self.settings,charge)
        else:
            provider = EODHDNews(self.settings,charge)
        articles, warnings = provider.fetch(ticker,fetch_start,end)
        try:
            articles = [Article.model_validate(a) for a in articles]
        except ValueError:
            raise DataQualityError('Malformed provider articles') from None
        if any(a.ticker!=ticker or not fetch_start<=a.published_at<end for a in articles):
            raise DataQualityError('Provider returned wrong ticker or out-of-window articles')
        # No articles is an error for initial/backfill; incremental quiet days can reuse persisted news.
        if full and not articles: raise DataQualityError('No usable news for full report window')
        self.store.ingest(ticker,articles,self.date.isoformat(),full,coverage_complete=not warnings)
        all_articles = [Article.model_validate(a) for a in self.store.articles(ticker,start,end)]
        if not all_articles: raise DataQualityError('No news in trailing 90 calendar days')
        
        # Use simplified deduplication for adaptive mode
        if self.settings.news_provider == 'adaptive_tavily':
            events = deduplicate_adaptive(all_articles)
        else:
            events = deduplicate(all_articles)
        
        self.store.save_events(ticker,events)
        if len(events)<3: raise DataQualityError('Fewer than three distinct events; cannot support requested executive summary')
        if not full: warnings.append('Incremental ingestion with overlap; deletions/corrections await periodic full review')
        if self.settings.news_provider=='eodhd' and not self.demo and not self.news_factory:
            warnings.append('Ticker-tagged provider news; untagged competitor/industry developments may be absent')
        return {'events':events,'warnings':warnings,'window_start':start.isoformat(),'window_end_exclusive':end.isoformat(),'full_backfill':full}

    def worker(self, ticker):
        d, v = self.date.isoformat(), self.version
        stage = 'pending'
        warnings = []
        try:
            for stage in ('collection','classification','synthesis','valuation','report'):
                if self.stop.is_set():
                    self.store.mark(ticker,d,v,'interrupted',stage,warnings=warnings)
                    return
                self.store.mark(ticker,d,v,'running',stage,warnings=warnings)
                log.info('%s %s %s',ticker,stage,v)
                cached = self.store.cached(ticker,d,v,stage) if stage not in ('report',) else None
                if stage=='collection':
                    collected = cached or self.collect(ticker)
                    warnings = collected['warnings']
                    payload = collected
                elif stage=='classification':
                    classified = cached or {'events':rank(collected['events'],self.settings.max_events)}
                    events = classified['events']; payload = classified
                elif stage=='synthesis':
                    # Use adaptive validation for adaptive_tavily mode
                    validator = validate_research_adaptive if self.settings.news_provider == 'adaptive_tavily' else validate_research
                    if cached:
                        if self.settings.research_mode == 'standard':
                            research = validator(cached,events)
                        else:
                            # For narrative mode, reconstruct DeepResearch object from cached dict
                            from equity.narrative_research import DeepResearch
                            research = DeepResearch.model_validate(cached)
                    else:
                        # Choose provider based on research mode
                        if self.research_factory:
                            provider = self.research_factory(ticker)
                        elif self.demo:
                            provider = DemoResearch()
                        elif self.settings.research_mode == 'narrative':
                            provider = NarrativeResearch(self.settings, self.store, ticker, d, v)
                        else:
                            provider = OpenAIResearch(self.settings, self.store, ticker, d, v)
                        
                        ref = self.store.reference(ticker)
                        # Give the LLM only scope/rationale; no write path to analyst reference exists.
                        context = None if ref is None else {'methodology':ref['methodology'], 'segments':[
                            {'name':s['name'],'scope':s['scope'],'multiple_rationale':s['multiple_rationale']} for s in ref['segments']]}
                        prompt_events = []
                        for e in events:
                            prompt_events.append({k:e[k] for k in ('id','title','date','summary','category','materiality_score')})
                        
                        raw_research = provider.synthesize(ticker, prompt_events, context)
                        
                        # Validate only for standard mode
                        if self.settings.research_mode == 'standard':
                            research = validator(raw_research, events)
                        else:
                            research = raw_research
                    
                    payload = research.model_dump() if hasattr(research, 'model_dump') else research
                elif stage=='valuation':
                    valuation = cached or calculate(self.store.reference(ticker),self.date,self.settings.valuation_stale_days,self.settings.price_stale_days)
                    if not cached:
                        valuation['news_review_flags'] = [{'event_id':e['id'],'title':e['title'],'category':e['category']} for e in events if e['materiality_score']>=4]
                    payload = valuation
                else:
                    # Fetch market context for narrative reports
                    market_context = None
                    if self.settings.research_mode == 'narrative':
                        try:
                            market_data_provider = EODHDMarketData(self.settings)
                            market_context = market_data_provider.fetch_context(ticker)
                        except Exception:
                            # Market data is optional; don't fail the report
                            pass
                    
                    # Choose renderer based on mode
                    if self.settings.research_mode == 'narrative':
                        artifacts = render_narrative(self.output/d, ticker, d, v, events, research, valuation, warnings, self.demo, market_context)
                        validator = validate_narrative_artifacts
                    else:
                        artifacts = self.renderer(self.output/d, ticker, d, v, events, research, valuation, warnings, self.demo)
                        validator = validate_artifacts
                    
                    if not validator(artifacts, ticker, events):
                        raise DataQualityError('Generated artifact validation failed')
                    payload = artifacts
                self.store.checkpoint(ticker,d,v,stage,payload)
            self.store.mark(ticker,d,v,'complete','validated',warnings=warnings+valuation['warnings'],artifacts=artifacts)
            # Report progress
            with self.progress_lock:
                self.completed_count += 1
                if self.progress_callback and self.completed_count % 4 == 0:
                    self.progress_callback(self.completed_count, ticker)
        except Exception as exc:
            category = ('temporary_api' if isinstance(exc,TemporaryAPIError) else
                        'permanent_api' if isinstance(exc,PermanentAPIError) else
                        'data_quality' if isinstance(exc,DataQualityError) else
                        'budget' if isinstance(exc,BudgetError) else 'internal')
            # Unknown exceptions may contain credentials; retain type only, no raw traceback.
            error = str(exc) if category!='internal' else type(exc).__name__
            self.store.mark(ticker,d,v,'failed',stage,error=error,error_type=category,warnings=warnings)
            log.error('%s failed at %s: %s (%s)',ticker,stage,error,category)

    def status(self):
        jobs = self.store.jobs(self.date.isoformat(),self.version)
        details = []
        for job in jobs:
            item = {k:job[k] for k in ('ticker','status','stage','error','error_type','updated_at')}
            item['warnings'] = json.loads(job['warnings'])
            item['artifacts'] = json.loads(job['artifacts']) if job['artifacts'] else None
            if item['status']=='complete':
                events = (self.store.cached(job['ticker'],self.date.isoformat(),self.version,'classification') or {}).get('events',[])
                # Use appropriate validator based on research mode
                if self.settings.research_mode == 'narrative':
                    validation_ok = validate_narrative_artifacts(item['artifacts'], job['ticker'], events)
                else:
                    validation_ok = validate_artifacts(item['artifacts'], job['ticker'], events)
                
                if not validation_ok:
                    item['status']='failed'; item['error']='Completed artifacts are missing or corrupt'; item['error_type']='artifact_integrity'
            details.append(item)
        complete = [j['ticker'] for j in details if j['status']=='complete']
        failed = [{'ticker':j['ticker'],'reason':j['error'],'type':j['error_type']} for j in details if j['status']=='failed']
        coverage_warnings = any(j['warnings'] for j in details)
        outcome = ('failed' if details and not complete and all(j['status']=='failed' for j in details) else
                   'complete' if details and len(complete)==len(details) and not coverage_warnings else
                   'partial' if details else 'not_started')
        return {'report_date':self.date.isoformat(),'report_version':self.version,'outcome':outcome,'demo':self.store.run_mode(self.date.isoformat(),self.version),
                'total':len(details),'completed':complete,'failed':failed,'jobs':details,
                'api_usage':self.store.usage(self.date.isoformat(),self.version)}

    def write_summary(self):
        status = self.status()  # Every ticker and its artifacts checked before completion message.
        destination = self.output/self.date.isoformat()
        atomic_write(destination/'status.json',json.dumps(status,indent=2)+'\n')
        atomic_write(destination/'failed_tickers.json',json.dumps(status['failed'],indent=2)+'\n')
        summary = [f"# Run {status['report_date']} ({self.version}): {status['outcome'].upper()}",
                   f"Validated reports: {len(status['completed'])}/{status['total']}",
                   'Partial includes incomplete news coverage, missing/stale valuation inputs, and failed/interrupted jobs.']
        for j in status['jobs']:
            summary.append(f"- {j['ticker']}: {j['status']} | {j['error'] or '; '.join(j['warnings']) or 'validated'}")
        atomic_write(destination/'completion_summary.md','\n'.join(summary)+'\n')
        return status

    def run(self, tickers):
        with run_lock(self.store.path):
            fingerprint = hashlib.sha256(json.dumps({'settings':self.settings.dict(), 'demo':self.demo,
                                        'output':str(self.output.resolve())},sort_keys=True).encode()).hexdigest()
            self.store.bind_run(self.date.isoformat(),self.version,fingerprint,self.demo)
            for t in tickers: self.store.ensure(t,self.date.isoformat(),self.version)
            jobs = self.store.jobs(self.date.isoformat(),self.version)
            pending = []
            for job in jobs:
                t = job['ticker']
                if t not in tickers: continue
                events = (self.store.cached(t,self.date.isoformat(),self.version,'classification') or {}).get('events',[])
                artifacts = json.loads(job['artifacts']) if job['artifacts'] else None
                if job['status']=='complete' and validate_artifacts(artifacts,t,events): continue
                pending.append(t)
            
            # Send initial progress message if callback is set
            if self.progress_callback:
                self.progress_callback(0, None, total=len(pending))
            
            with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
                futures = [pool.submit(self.worker,t) for t in pending]
                for future in as_completed(futures): future.result()
            return self.write_summary()
