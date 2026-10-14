import json
from datetime import datetime, timezone
import httpx
import pytest
from equity.models import DataQualityError, PermanentAPIError, TemporaryAPIError, BudgetError
from equity.providers import EODHDNews, OpenAIResearch, DemoResearch, retry, request_json, FileNews
from equity.demo import DemoNews
from equity.events import deduplicate, rank, validate_research
from equity.store import Store
from datetime import date

START=datetime(2026,7,13,tzinfo=timezone.utc)
END=datetime(2026,10,11,tzinfo=timezone.utc)


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('status',[429,408,500,503])
def test_transient_retry(settings,status):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status if len(calls)==1 else 200,json=[])
    result=retry(lambda:request_json(client(handler),'GET','https://example.com'),settings)
    assert result==[] and len(calls)==2


def test_timeout_retry(settings):
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('test timeout')
    with pytest.raises(TemporaryAPIError): retry(lambda:request_json(client(handler),'GET','https://example.com'),settings)
    assert len(calls)==2


def test_permanent_not_retried(settings):
    calls=[]
    def handler(request):
        calls.append(request); return httpx.Response(401)
    with pytest.raises(PermanentAPIError): retry(lambda:request_json(client(handler),'GET','https://example.com'),settings)
    assert len(calls)==1


def eod_row(i=0):
    return {'date':'2026-10-01T12:00:00+00:00','title':f'Earnings guidance {i}','content':'Source excerpt','link':f'https://example.com/{i}'}


def test_eodhd_pagination(settings,monkeypatch):
    monkeypatch.setenv('EODHD_API_KEY','fake')
    offsets=[]
    def handler(request):
        offsets.append(int(request.url.params['offset']))
        return httpx.Response(200,json=[eod_row(i) for i in range(1000)] if len(offsets)==1 else [eod_row(1000)])
    articles,warnings=EODHDNews(settings,client=client(handler)).fetch('TSLA',START,END)
    assert len(articles)==1001 and offsets==[0,1000] and not warnings


def test_fallback_is_incomplete(settings,monkeypatch):
    monkeypatch.setenv('EODHD_API_KEY','fake'); monkeypatch.setenv('TAVILY_API_KEY','fake')
    def handler(request):
        if request.url.host=='eodhd.com': return httpx.Response(429)
        return httpx.Response(200,json={'results':[{'title':'Product launch','url':'https://example.com/a','content':'Real snippet in mock','published_date':'2026-10-01'}]})
    articles,warnings=EODHDNews(settings,client=client(handler)).fetch('TSLA',START,END)
    assert articles[0].provider=='tavily'
    assert 'Incomplete coverage' in warnings[0]


def test_undated_fallback_not_invented(settings,monkeypatch):
    monkeypatch.delenv('EODHD_API_KEY',raising=False); monkeypatch.setenv('TAVILY_API_KEY','fake')
    c=client(lambda r:httpx.Response(200,json={'results':[{'title':'News','url':'https://example.com','content':'Snippet'}]}))
    with pytest.raises(DataQualityError): EODHDNews(settings,client=c).fetch('TSLA',START,END)


@pytest.mark.parametrize('raw',[{},'oops',[],[{'title':'Missing fields'}]])
def test_malformed_news(settings,monkeypatch,raw):
    monkeypatch.setenv('EODHD_API_KEY','fake'); monkeypatch.delenv('TAVILY_API_KEY',raising=False)
    with pytest.raises(DataQualityError): EODHDNews(settings,client=client(lambda r:httpx.Response(200,json=raw))).fetch('TSLA',START,END)


def test_duplicate_event_merge():
    articles,_=DemoNews(date(2026,10,10)).fetch('TSLA',START,END)
    duplicate=articles[0].model_copy(update={'article_id':'other','url':'https://example.com/second'})
    events=deduplicate(articles+[duplicate])
    assert len(events)==3
    assert len(next(e for e in events if e['title']==articles[0].title)['sources'])==2


def test_invalid_llm_output_and_missing_citations(settings,tmp_path,monkeypatch):
    monkeypatch.setenv('LLM_API_KEY','fake')
    for content in ['{}','not json']:
        c=client(lambda r:httpx.Response(200,json={'usage':{'prompt_tokens':20,'completion_tokens':20},'choices':[{'finish_reason':'stop','message':{'content':content}}]}))
        with pytest.raises(DataQualityError): OpenAIResearch(settings,Store(tmp_path/'db'),'TSLA','2026-10-10','v1',c).synthesize('TSLA',[],None)
    events=rank(deduplicate(DemoNews(date(2026,10,10)).fetch('TSLA',START,END)[0]),8)
    research=DemoResearch().synthesize('TSLA',events,None)
    research.executive_summary[0].event_ids=['invented']
    with pytest.raises(DataQualityError): validate_research(research,events)
    research=DemoResearch().synthesize('TSLA',events,None)
    research.executive_summary[0].text='Revenue grew 998877%'
    with pytest.raises(DataQualityError): validate_research(research,events)


def test_budget_persistent_across_retry(settings,tmp_path,monkeypatch):
    monkeypatch.setenv('LLM_API_KEY','fake'); settings.max_tokens=1
    calls=[]
    with pytest.raises(BudgetError):
        OpenAIResearch(settings,Store(tmp_path/'db'),'TSLA','2026-10-10','v1',client(lambda r:calls.append(r))).synthesize('TSLA',[],None)
    assert calls==[]


def test_missing_credentials_no_secret_echo(settings,monkeypatch):
    monkeypatch.delenv('EODHD_API_KEY',raising=False); monkeypatch.delenv('TAVILY_API_KEY',raising=False)
    with pytest.raises(PermanentAPIError,match='Missing EODHD_API_KEY'):
        EODHDNews(settings).fetch('TSLA',START,END)


def test_retry_preserves_spend_on_timeout(settings,tmp_path,monkeypatch):
    monkeypatch.setenv('LLM_API_KEY','fake')
    store=Store(tmp_path/'db')
    def handler(request): raise httpx.ReadTimeout('request may have been billed')
    with pytest.raises(TemporaryAPIError):
        OpenAIResearch(settings,store,'TSLA','2026-10-10','v1',client(handler)).synthesize('TSLA',[],None)
    with store.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM api_ledger').fetchone()[0]==2
    assert store.usage('2026-10-10','v1')['cost_usd']>0


def test_budget_atomic_under_concurrency(settings,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store=Store(tmp_path/'db'); settings.max_cost_usd=1
    def reserve(i):
        try: store.reserve('2026-10-10','v1',str(i),'test',10,.6,settings); return True
        except BudgetError: return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve,range(4)))==1
    assert store.usage('2026-10-10','v1')['cost_usd']==.6


def test_syndicated_body_deduplicated_even_with_different_headlines():
    articles,_=DemoNews(date(2026,10,10)).fetch('TSLA',START,END)
    article=articles[0].model_copy(update={'summary':'Identical syndicated source text about an earnings announcement and its outlook. '*4})
    duplicate=article.model_copy(update={'title':'Different syndicated headline','article_id':'syndicated','url':'https://example.com/syndicated'})
    assert len(deduplicate([article,duplicate]))==1
