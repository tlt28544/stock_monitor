from datetime import datetime, timedelta, timezone
from .models import Article


class DemoNews:
    def __init__(self, report_date):
        self.report_date = report_date

    def fetch(self, ticker, start, end):
        articles = []
        for i, subject in enumerate(['earnings guidance review','regulatory review','product launch']):
            published = datetime.combine(self.report_date-timedelta(days=7*(i+1)),datetime.min.time(),tzinfo=timezone.utc)
            articles.append(Article(provider='synthetic-demo',article_id=f'{ticker}-{i}',ticker=ticker,
                            title=f'SYNTHETIC {ticker} {subject}',summary=f'Fabricated test fixture for {subject}; no actual company event or financial claim.',
                            url=f'https://example.com/synthetic/{ticker}/{i}',published_at=published,retrieved_at=datetime.now(timezone.utc)))
        return articles, ['SYNTHETIC DEMO: fabricated test fixtures, not real company news']
