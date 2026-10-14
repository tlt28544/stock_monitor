import json
from datetime import date
from pathlib import Path
import pytest
from pypdf import PdfReader
from equity.models import DataQualityError, Settings
from equity.pipeline import Pipeline, run_lock
from equity.demo import DemoNews
from equity.providers import DemoResearch
from equity.reports import render


def test_three_stock_demo(pipeline):
    result=pipeline.run(['TSLA','AMZN','META'])
    assert result['outcome']=='partial' and len(result['completed'])==3
    assert result['api_usage']=={'tokens':0,'cost_usd':0}
    for ticker in result['completed']:
        path=pipeline.output/'2026-10-10'/f'{ticker}.pdf'
        reader=PdfReader(path)
        assert len(reader.pages)==2
        assert 'Valuation Table' in reader.pages[1].extract_text()
        assert 'SYNTHETIC' in reader.pages[0].extract_text()
    assert json.loads((pipeline.output/'2026-10-10'/'status.json').read_text())['total']==3


def test_duplicate_schedule_no_work(pipeline):
    pipeline.run(['TSLA'])
    before=(pipeline.output/'2026-10-10'/'TSLA.pdf').stat().st_mtime_ns
    pipeline.news_factory=lambda t:pytest.fail('Duplicate collection')
    pipeline.research_factory=lambda t:pytest.fail('Duplicate synthesis')
    pipeline.run(['TSLA'])
    assert (pipeline.output/'2026-10-10'/'TSLA.pdf').stat().st_mtime_ns==before


def test_concurrent_schedule_excluded(pipeline):
    with run_lock(pipeline.store.path):
        with pytest.raises(RuntimeError,match='Another orchestrator'): pipeline.run(['TSLA'])


def test_partial_success_across_stocks(pipeline):
    class BrokenNews:
        def fetch(self,*args): raise DataQualityError('Missing data')
    pipeline.news_factory=lambda t:BrokenNews() if t=='META' else DemoNews(pipeline.date)
    result=pipeline.run(['TSLA','META','AMZN'])
    assert len(result['completed'])==2
    assert result['failed']==[{'ticker':'META','reason':'Missing data','type':'data_quality'}]


def test_restart_after_synthesis_interrupt(pipeline):
    class Interrupted:
        def synthesize(self,*args): raise KeyboardInterrupt()
    pipeline.research_factory=lambda t:Interrupted()
    with pytest.raises(KeyboardInterrupt): pipeline.run(['TSLA'])
    assert pipeline.store.cached('TSLA','2026-10-10','v1','classification')
    recovered=Pipeline(pipeline.settings,pipeline.store.path,pipeline.output,pipeline.date,demo=True,
                       news_factory=lambda t:pytest.fail('Collection should be checkpointed'),research_factory=lambda t:DemoResearch())
    assert recovered.run(['TSLA'])['completed']==['TSLA']


def test_pdf_failure_does_not_complete_and_retry_reuses_synthesis(pipeline):
    def broken(*args): raise OSError('simulated pdf failure')
    pipeline.renderer=broken
    result=pipeline.run(['TSLA'])
    assert result['outcome']=='failed'
    assert result['jobs'][0]['stage']=='report'
    assert not (pipeline.output/'2026-10-10'/'TSLA.pdf').exists()
    pipeline.renderer=render
    pipeline.research_factory=lambda t:pytest.fail('Already synthesized')
    assert pipeline.run(['TSLA'])['completed']==['TSLA']


def test_missing_completed_file_regenerated(pipeline):
    pipeline.run(['TSLA'])
    (pipeline.output/'2026-10-10'/'TSLA.pdf').write_bytes(b'bad')
    assert pipeline.status()['jobs'][0]['status']=='failed'
    assert pipeline.run(['TSLA'])['completed']==['TSLA']


def test_graceful_stop_records_interrupted(pipeline):
    pipeline.stop.set()
    result=pipeline.run(['TSLA','META'])
    assert all(j['status']=='interrupted' for j in result['jobs'])
    pipeline.stop.clear()
    assert len(pipeline.run(['TSLA','META'])['completed'])==2


def test_changed_config_requires_new_version(pipeline):
    pipeline.run(['TSLA'])
    pipeline.settings.max_events=4
    with pytest.raises(ValueError,match='configuration differs'): pipeline.run(['TSLA'])


def test_valuation_checkpoint_immutable(pipeline,reference):
    reference['ticker']='TSLA'
    pipeline.store.import_reference('TSLA','Analyst',True,reference)
    pipeline.run(['TSLA'])
    original=pipeline.store.cached('TSLA','2026-10-10','v1','valuation')
    reference['segments'][0]['multiple']='99'
    pipeline.store.import_reference('TSLA','Analyst',True,reference)
    # Re-render must preserve the valuation revision used by this report version.
    (pipeline.output/'2026-10-10'/'TSLA.pdf').unlink()
    pipeline.run(['TSLA'])
    assert pipeline.store.cached('TSLA','2026-10-10','v1','valuation')==original
    assert pipeline.store.reference('TSLA')['segments'][0]['multiple']=='99'


def test_36_stocks_same_architecture(pipeline):
    tickers=[f'T{i:02d}' for i in range(36)]
    result=pipeline.run(tickers)
    assert result['total']==36 and len(result['completed'])==36


def test_cli_status_resume(tmp_path):
    from orchestrator import main
    args=['--date','2026-10-10','--demo','--db',str(tmp_path/'db'),'--output',str(tmp_path/'reports')]
    assert main(['--tickers','TSLA']+args)==2
    assert main(['--status']+args)==0
    assert main(['--resume']+args)==2


def test_live_adapters_end_to_end_mocked(tmp_path,settings,monkeypatch):
    import httpx
    from equity.providers import EODHDNews, OpenAIResearch
    monkeypatch.setenv('EODHD_API_KEY','fake'); monkeypatch.setenv('LLM_API_KEY','fake')
    def handler(request):
        if request.url.host=='eodhd.com':
            return httpx.Response(200,json=[{'date':'2026-10-01T00:00:00Z','title':title,'content':'Source-provided mock excerpt',
                                           'link':f'https://example.com/{i}'} for i,title in enumerate(['Earnings guidance','Regulatory action','Product launch'])])
        body=json.loads(request.content)
        data=json.loads(body['messages'][1]['content'])
        research=DemoResearch().synthesize(data['ticker'],data['events'],None)
        return httpx.Response(200,json={'usage':{'prompt_tokens':100,'completion_tokens':100},
                                      'choices':[{'finish_reason':'stop','message':{'content':research.model_dump_json()}}]})
    c=httpx.Client(transport=httpx.MockTransport(handler))
    p=Pipeline(settings,tmp_path/'db',tmp_path/'out',date(2026,10,10),news_factory=lambda t:EODHDNews(settings,client=c))
    p.research_factory=lambda t:OpenAIResearch(settings,p.store,t,'2026-10-10','v1',c)
    result=p.run(['TSLA','AMZN','META'])
    assert len(result['completed'])==3
    assert result['api_usage']['tokens']==600
    assert result['api_usage']['cost_usd']>0
    assert result['demo'] is False


def test_incremental_and_periodic_backfill(tmp_path,settings):
    windows=[]
    class News:
        def fetch(self,ticker,start,end):
            windows.append((start,end))
            # No new articles in next report week; stored articles remain relevant.
            return (DemoNews(date(2026,10,10)).fetch(ticker,start,end)[0],[]) if len(windows) in (1,3) else ([],[])
    p=Pipeline(settings,tmp_path/'db',tmp_path/'out',date(2026,10,10),demo=True,news_factory=lambda t:News())
    assert p.run(['TSLA'])['completed']==['TSLA']
    p2=Pipeline(settings,tmp_path/'db',tmp_path/'out',date(2026,10,17),demo=True,news_factory=lambda t:News())
    assert p2.run(['TSLA'])['completed']==['TSLA']
    assert windows[0][0].date()==date(2026,7,13)
    assert windows[1][0].date()==date(2026,10,7)
    # Backfill needs new within-window fixtures; keep original events within 90 days here.
    p3=Pipeline(settings,tmp_path/'db',tmp_path/'out',date(2026,11,7),demo=True,news_factory=lambda t:News())
    assert p3.run(['TSLA'])['completed']==['TSLA']
    assert windows[2][0].date()==date(2026,8,10)


def test_full_report_with_approved_reference_and_complete_provider(pipeline,reference):
    reference['ticker']='TSLA'
    pipeline.store.import_reference('TSLA','Analyst',True,reference)
    class CompleteNews:
        def fetch(self,*args): return DemoNews(pipeline.date).fetch(*args)[0],[]
    pipeline.news_factory=lambda t:CompleteNews()
    result=pipeline.run(['TSLA'])
    assert result['outcome']=='complete'
    assert result['jobs'][0]['warnings']==[]
    last=PdfReader(pipeline.output/'2026-10-10'/'TSLA.pdf').pages[1].extract_text()
    assert '3,300.00' in last and '33.00' in last and '10.00%' in last


def test_atomic_pdf_failure_preserves_previous_output(pipeline,monkeypatch):
    import equity.reports as reports
    pipeline.run(['TSLA'])
    old=(pipeline.output/'2026-10-10'/'TSLA.pdf').read_bytes()
    def fail(*args,**kwargs): raise OSError('Build failed')
    monkeypatch.setattr(reports.SimpleDocTemplate,'build',fail)
    pipeline.renderer=render
    # Re-render explicitly to exercise atomic writing, not the idempotent skip.
    events=pipeline.store.cached('TSLA','2026-10-10','v1','classification')['events']
    from equity.models import Research
    research=Research.model_validate(pipeline.store.cached('TSLA','2026-10-10','v1','synthesis'))
    valuation=pipeline.store.cached('TSLA','2026-10-10','v1','valuation')
    with pytest.raises(OSError): render(pipeline.output/'2026-10-10','TSLA','2026-10-10','v1',events,research,valuation,[],True)
    assert (pipeline.output/'2026-10-10'/'TSLA.pdf').read_bytes()==old
    assert not list((pipeline.output/'2026-10-10').glob('.TSLA.*'))


def test_incomplete_backfill_retried_not_treated_as_complete(pipeline):
    result=pipeline.run(['TSLA'])
    assert result['completed']==['TSLA']
    assert pipeline.store.ingestion('TSLA')['full_date'] is None
