"""Offline fixtures exercise the same five-search and narrative/PDF path."""
import hashlib
from datetime import timedelta
from .adaptive_providers import AdaptiveTavilyNews
from .models import Article, DeepResearch, NarrativeBlock


class DemoNews(AdaptiveTavilyNews):
    def search(self, query, start, end, max_results, stop=None):
        key = hashlib.sha256(query.encode()).hexdigest()[:12]
        article = Article(id=key, title='Synthetic AI chip and product launch update',
            summary='Synthetic company update: an AI chip product launch and partnership are announced. '
                    'The company is expanding its offering while customer adoption remains uncertain.',
            url=f'https://example.com/demo/{key}', published_at=end - timedelta(days=2))
        # Distinct titles let the demo exercise multiple thematic sections.
        article.title += ' ' + key.upper()
        return {'query': query, 'articles': [article.model_dump(mode='json')], 'skipped': 0}


class DemoResearch:
    def synthesize(self, ticker, articles, start, end, stop=None):
        return DeepResearch(narrative_blocks=[NarrativeBlock(title='Product strategy and customer adoption',
            content='The synthetic company update describes an AI chip product launch and a partnership. '
                    'These announcements indicate an expansion of the product offering. Customer adoption '
                    'remains uncertain in the supplied evidence, so the commercial impact cannot yet be established.',
            event_ids=[articles[0]['id']])])

    def close(self):
        pass
