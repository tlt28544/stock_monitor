import json
import threading
from datetime import date, datetime, timezone

import httpx
import pytest

from equity.adaptive_providers import AdaptiveTavilyNews, company_name, timestamp
from equity.models import Settings, DataQualityError, PermanentAPIError, report_window
from equity.narrative_research import NarrativeResearch, validate_research


def news_row(title='AI chip launch', date='2026-10-01T00:00:00Z', url='https://example.com/news'):
    return {'title': title, 'published_date': date, 'url': url,
            'content': 'AI chip AI chip revenue growth; revenue increased 12% to $1,200 million.'}


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'fake')
    monkeypatch.setenv('LLM_API_KEY', 'fake')
    return Settings(retries=0, retry_base_seconds=0)


def test_original_five_search_strategy_and_theme_order(settings):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'results': [news_row()]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = AdaptiveTavilyNews(settings, client)
        start, end = report_window(date(2026, 10, 10))
        result = provider.fetch('TSLA', start, end)
    assert len(requests) == 5 and len(result['searches']) == 5
    assert 'announced launched new product management appointed CEO hired acquired buyback insider buy' in requests[0]['query']
    assert 'AI breakthrough technology innovation partnership deal customer contract competitive advantage' in requests[1]['query']
    assert 'trending hot topic buzz viral news' in requests[2]['query']
    assert 'ai chip details analysis impact' in requests[3]['query']
    assert 'revenue growth details analysis impact' in requests[4]['query']
    assert [r['max_results'] for r in requests] == [15, 15, 15, 10, 10]
    assert all('October 2026' in r['query'] and '2026-10-10' in r['query'] for r in requests)
    assert all(r['start_date'] == '2026-09-10' and r['end_date'] == '2026-10-11' and 'days' not in r for r in requests)
    assert len(result['articles']) == 1  # duplicate sources from searches are collapsed


def test_dates_missing_malformed_historical_and_shanghai_boundaries(settings):
    rows = [news_row(date=d, url=f'https://example.com/{i}', title=f'Article {i}') for i, d in enumerate([
        None, 'bad date', '2026-09-10T15:59:59Z', '2026-09-10T16:00:00Z',
        'Sat, 10 Oct 2026 15:59:59 GMT', '2026-10-10T16:00:00Z', '2099-01-01T00:00:00Z'])]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'results': rows}))) as client:
        provider = AdaptiveTavilyNews(settings, client)
        start, end = report_window(date(2026, 10, 10))
        result = provider.fetch('TSLA', start, end)
    assert {a['title'] for a in result['articles']} == {'Article 3', 'Article 4'}
    assert all(s['skipped'] == 5 for s in result['searches'])
    assert timestamp('2026-10-01').tzinfo == timezone.utc


def test_resume_search_four_does_not_repeat_first_three(settings):
    calls, cached = [], {}
    def fail_on_four(request):
        calls.append(json.loads(request.content)['query'])
        return httpx.Response(503 if len(calls) == 4 else 200, json={'results': [news_row()]})
    start, end = report_window(date(2026, 10, 10))
    with httpx.Client(transport=httpx.MockTransport(fail_on_four)) as client:
        with pytest.raises(Exception, match='HTTP 503'):
            AdaptiveTavilyNews(settings, client).fetch('TSLA', start, end, cached.get, cached.__setitem__)
    assert set(cached) == {'search_1', 'search_2', 'search_3'}
    resumed = []
    def success(request):
        resumed.append(json.loads(request.content)['query'])
        return httpx.Response(200, json={'results': [news_row()]})
    with httpx.Client(transport=httpx.MockTransport(success)) as client:
        result = AdaptiveTavilyNews(settings, client).fetch('TSLA', start, end, cached.get, cached.__setitem__)
    assert len(resumed) == 2 and len(result['searches']) == 5
    assert resumed[0] == calls[3]


def test_retry_does_not_reduce_five_logical_searches(settings):
    settings.retries = 1
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(429 if len(requests) == 1 else 200, json={'results': [news_row()]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = AdaptiveTavilyNews(settings, client).fetch('TSLA', *report_window(date(2026, 10, 10)))
    assert len(requests) == 6 and requests[0] == requests[1]
    assert len(result['searches']) == 5


def test_empty_results_still_do_five_searches(settings):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'results': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = AdaptiveTavilyNews(settings, client).fetch('TSLA', *report_window(date(2026, 10, 10)))
    assert len(calls) == 5 and result['articles'] == []
    assert 'latest news details analysis impact' in calls[3]['query']
    assert 'recent developments details analysis impact' in calls[4]['query']


def test_original_single_bigram_and_single_word_fallback(settings):
    from equity.models import Article
    article = Article(id='a', title='AI chip', summary='AI chip adoption adoption adoption',
                      url='https://example.com/a', published_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    provider = AdaptiveTavilyNews(settings)
    try:
        assert provider._extract_themes([article], 'Tesla') == ['ai chip', 'adoption']
        article.title = 'Growth outlook'; article.summary = 'growth growth outlook'
        assert provider._extract_themes([article], 'Tesla') == ['growth', 'outlook']
    finally:
        provider.close()


def test_ticker_mapping_avoids_gen_company_mismatch():
    assert company_name('GEN') == 'Gen Digital Inc.'
    assert company_name('NVDA.US') == 'NVIDIA Corporation'


def test_tavily_quota_exhaustion_is_actionable_and_not_retried(settings):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(432, json={'detail': {'error': 'usage limit'}})
    settings.retries = 2
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PermanentAPIError, match='quota/usage limit reached'):
            AdaptiveTavilyNews(settings, client).fetch('TSLA', *report_window(date(2026, 10, 10)))
    assert len(calls) == 1


def test_narrative_unknown_citation_and_unsupported_numbers():
    articles = [{'id': 'a', 'title': 'Revenue growth', 'summary': 'Revenue rose 12% to $1,200 million.',
                 'published_at': '2026-10-01T00:00:00Z'}]
    def report(content, ids):
        return {'narrative_blocks': [{'title': 'Growth', 'content': content, 'event_ids': ids}]}
    assert validate_research(report('Revenue rose 12% to 1200 million.', ['a']), articles)
    with pytest.raises(DataQualityError, match='unknown article'):
        validate_research(report('Growth improved.', ['invented']), articles)
    with pytest.raises(DataQualityError, match='numbers absent'):
        validate_research(report('Revenue rose 99%.', ['a']), articles)
    with pytest.raises(DataQualityError, match='empty narrative'):
        validate_research({'narrative_blocks': []}, articles)


@pytest.mark.parametrize('finish,content', [('length', '{}'), ('stop', 'not-json')])
def test_invalid_or_truncated_llm_responses(settings, finish, content):
    def handler(request):
        return httpx.Response(200, json={'choices': [{'finish_reason': finish, 'message': {'content': content}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DataQualityError):
            NarrativeResearch(settings, client).synthesize('TSLA', [], *report_window(date(2026, 10, 10)))


def test_llm_openai_key_fallback_and_strict_schema(settings, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY'); monkeypatch.setenv('OPENAI_API_KEY', 'standard-key')
    articles = [{'id': 'a', 'title': 'Launch', 'summary': 'A product launched.', 'published_at': '2026-10-01'}]
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer standard-key'
        body = json.loads(request.content)
        schema = body['response_format']['json_schema']['schema']
        assert schema['additionalProperties'] is False
        assert schema['$defs']['NarrativeBlock']['additionalProperties'] is False
        output = {'narrative_blocks': [{'title': 'Product', 'content': 'A product launched.', 'event_ids': ['a']}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert NarrativeResearch(settings, client).synthesize('TSLA', articles, *report_window(date(2026, 10, 10)))
