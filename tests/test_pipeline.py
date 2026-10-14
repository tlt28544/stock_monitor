import json
import os
import subprocess
from datetime import date
from pathlib import Path

import httpx
import pytest
from pypdf import PdfReader

from equity.adaptive_providers import AdaptiveTavilyNews
from equity.models import Settings, DataQualityError
from equity.pipeline import Pipeline, run_lock
from equity.narrative_reports import render_narrative
from equity.narrative_research import NarrativeResearch


@pytest.fixture
def pipeline(tmp_path):
    return Pipeline(Settings(retries=0), tmp_path / 'db', tmp_path / 'reports', date(2026, 10, 10), demo=True)


def test_demo_all_companies_and_pdf_has_no_valuation(pipeline):
    tickers = Settings.model_validate_json(Path('config/settings.json').read_text()).watchlist
    result = pipeline.run(tickers)
    assert result['outcome'] == 'complete' and len(result['completed']) == 40
    for ticker in tickers:
        reader = PdfReader(pipeline.output / '2026-10-10' / f'{ticker}.pdf')
        text = '\n'.join(p.extract_text() for p in reader.pages)
        assert ticker in text and 'SYNTHETIC DEMO' in text and 'Sources' in text
        assert 'Valuation' not in text
        meta = json.loads((pipeline.output / '2026-10-10' / f'{ticker}.json').read_text())
        assert len(meta['searches']) == 5


def test_completed_narrative_skipped_and_corrupt_pdf_repaired(pipeline):
    assert pipeline.run(['TSLA'])['outcome'] == 'complete'
    path = pipeline.output / '2026-10-10/TSLA.pdf'
    before = path.stat().st_mtime_ns
    pipeline.news_factory = lambda t: pytest.fail('Repeated news')
    pipeline.research_factory = lambda t: pytest.fail('Repeated LLM')
    pipeline.renderer = lambda *a: pytest.fail('Repeated render')
    assert pipeline.run(['TSLA'])['outcome'] == 'complete'
    assert path.stat().st_mtime_ns == before
    path.write_bytes(b'broken')
    assert pipeline.status()['jobs'][0]['status'] == 'failed'
    pipeline.renderer = render_narrative
    assert pipeline.run(['TSLA'])['outcome'] == 'complete'


def test_pdf_failure_reuses_news_and_llm(pipeline):
    def fail(*args):
        raise OSError('PDF error')
    pipeline.renderer = fail
    result = pipeline.run(['TSLA'])
    assert result['outcome'] == 'failed' and result['failed'][0]['stage'] == 'pdf'
    pipeline.news_factory = lambda t: pytest.fail('Repeated news')
    pipeline.research_factory = lambda t: pytest.fail('Repeated LLM')
    pipeline.renderer = render_narrative
    assert pipeline.run(['TSLA'])['outcome'] == 'complete'


def test_one_company_failure_does_not_abort_other_reports(pipeline):
    from equity.demo import DemoNews
    class BrokenNews:
        def fetch(self, *args):
            raise DataQualityError('Malformed Tavily results')
    pipeline.news_factory = lambda t: BrokenNews() if t == 'META' else DemoNews(pipeline.settings)
    result = pipeline.run(['TSLA', 'META'])
    assert result['outcome'] == 'partial' and result['completed'] == ['TSLA']
    assert result['failed'][0]['reason'] == 'Malformed Tavily results'


def test_empty_month_no_llm_but_honest_pdf(pipeline):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'results': []})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        pipeline.news_factory = lambda t: AdaptiveTavilyNews(pipeline.settings, client)
        pipeline.research_factory = lambda t: pytest.fail('No news must not call LLM')
        with pytest.MonkeyPatch.context() as m:
            m.setenv('TAVILY_API_KEY', 'fake')
            assert pipeline.run(['TSLA'])['outcome'] == 'complete'
    assert len(calls) == 5
    text = PdfReader(pipeline.output / '2026-10-10/TSLA.pdf').pages[0].extract_text()
    assert 'No usable dated news' in text


def test_lock_stop_and_resume(pipeline):
    with run_lock(pipeline.store.path):
        with pytest.raises(RuntimeError, match='Another orchestrator'):
            pipeline.run(['TSLA'])
    pipeline.stop.set()
    assert pipeline.run(['TSLA'])['jobs'][0]['status'] == 'interrupted'
    pipeline.stop.clear()
    assert pipeline.run(['TSLA'])['outcome'] == 'complete'


def test_changed_model_requires_new_version_but_concurrency_does_not(pipeline):
    pipeline.run(['TSLA'])
    pipeline.settings.concurrency = 1
    assert pipeline.run(['TSLA'])['outcome'] == 'complete'
    pipeline.settings.llm_model = 'different-model'
    with pytest.raises(ValueError, match='Configuration changed'):
        pipeline.run(['TSLA'])


def test_live_adapters_mocked_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'fake'); monkeypatch.setenv('LLM_API_KEY', 'fake')
    calls = {'news': 0, 'llm': 0}
    def handler(request):
        if request.url.host == 'api.tavily.com':
            calls['news'] += 1
            return httpx.Response(200, json={'results': [{'published_date': '2026-10-01T00:00:00Z',
                'url': 'https://example.com/product', 'title': 'Product launch', 'content': 'The company launched a product.'}]})
        calls['llm'] += 1
        articles = json.loads(json.loads(request.content)['messages'][1]['content'])['articles']
        raw = {'narrative_blocks': [{'title': 'Product strategy', 'content': 'The company launched a product.',
                                    'event_ids': [articles[0]['id']]}]}
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(raw)}}]})
    settings = Settings(retries=0)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        p = Pipeline(settings, tmp_path / 'db', tmp_path / 'reports', date(2026, 10, 10),
                     news_factory=lambda t: AdaptiveTavilyNews(settings, client),
                     research_factory=lambda t: NarrativeResearch(settings, client))
        assert p.run(['TSLA', 'META'])['outcome'] == 'complete'
        assert p.run(['TSLA', 'META'])['outcome'] == 'complete'
    assert calls == {'news': 10, 'llm': 2}
    md = (p.output / '2026-10-10/TSLA.md').read_text()
    assert 'The company launched a product. [1]' in md


def test_cli_demo_status_resume(tmp_path, capsys):
    from orchestrator import main
    args = ['--date', '2026-10-10', '--demo', '--db', str(tmp_path / 'db'), '--output', str(tmp_path / 'reports')]
    assert main(['--tickers', 'TSLA'] + args) == 0
    assert main(['--status'] + args) == 0
    assert main(['--resume'] + args) == 0


@pytest.mark.parametrize('runner', ['run_weekly.sh', 'scripts/run_weekly.sh'])
@pytest.mark.parametrize('code', [0, 1, 2])
def test_shell_runner_preserves_exit_code_from_any_directory(tmp_path, runner, code):
    root = Path(__file__).resolve().parents[1]
    fake = tmp_path / 'fake-python'
    fake.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@"\nexit {code}\n')
    fake.chmod(0o755)
    env = dict(os.environ, STOCK_MONITOR_PYTHON=str(fake), STOCK_MONITOR_ENV_FILE=str(tmp_path / 'absent'))
    result = subprocess.run(['bash', str(root / runner), '2026-10-10'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == code
    assert str(root / 'orchestrator.py') in result.stdout
    assert '--all\n--date\n2026-10-10' in result.stdout
    assert 'successfully' not in result.stdout
