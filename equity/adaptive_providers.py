"""Five Tavily searches per company: two baselines, discovery, two deep dives."""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urldefrag

import httpx
from .http import request_json, retry
from .models import Article, DataQualityError, PermanentAPIError, InterruptedRun


def company_name(ticker):
    mapping = json.loads((Path(__file__).resolve().parent.parent / 'config/company_names.json').read_text())
    return mapping.get(ticker, mapping.get(ticker.split('.')[0], ticker))


def timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing publication date')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        result = parsedate_to_datetime(value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def deduplicate(articles):
    seen_urls, seen_titles, result = set(), set(), []
    for article in sorted(articles, key=lambda a: (a.published_at, a.id), reverse=True):
        url = urldefrag(article.url)[0]
        title = ' '.join(re.findall(r'\w+', article.title.lower()))
        if url in seen_urls or title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        result.append(article)
    return result


class AdaptiveTavilyNews:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()

    def search(self, query, start, end, max_results, stop=None):
        key = os.getenv(self.settings.tavily_key_env)
        if not key:
            raise PermanentAPIError(f'Missing {self.settings.tavily_key_env}')
        # Widen API date boundaries by one day; enforce the exact Shanghai window below.
        raw = retry(lambda: request_json(self.client, 'POST', 'https://api.tavily.com/search',
                    headers={'Authorization': f'Bearer {key}'}, json={
                        'query': query, 'topic': 'news', 'search_depth': 'advanced',
                        'max_results': max_results,
                        'start_date': (start.date() - timedelta(days=1)).isoformat(),
                        'end_date': end.date().isoformat(),
                    }), self.settings, stop)
        if not isinstance(raw, dict) or not isinstance(raw.get('results'), list):
            raise DataQualityError('Malformed Tavily results')
        articles, skipped = [], 0
        for row in raw['results']:
            try:
                published = timestamp(row.get('published_date'))
                if not start <= published < end:
                    skipped += 1
                    continue
                url = urldefrag(row['url'])[0]
                articles.append(Article(id=hashlib.sha256(url.encode()).hexdigest()[:20],
                    title=row['title'][:500], summary=(row.get('content') or row.get('snippet') or '')[:6000],
                    url=url, published_at=published))
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                skipped += 1
        return {'query': query, 'articles': [a.model_dump(mode='json') for a in articles], 'skipped': skipped}

    def fetch(self, ticker, start, end, load=lambda stage: None, save=lambda stage, payload: None, stop=None):
        self.ticker = ticker
        name = company_name(ticker)
        report_date = end.date() - timedelta(days=1)
        current_month = report_date.strftime('%B %Y')
        date_context = f'report date is {report_date}; news published {start.date()} through {report_date}'
        baseline = [
            f'{name} announced launched new product management appointed CEO hired acquired buyback insider buy {current_month} past month {date_context}',
            f'{name} AI breakthrough technology innovation partnership deal customer contract competitive advantage {current_month} latest {date_context}',
            f'{name} trending hot topic buzz viral news {current_month} past month what happened {date_context}',
        ]
        all_articles, searches = [], []
        deep_themes = None
        for index in range(5):
            if stop is not None and stop.is_set():
                raise InterruptedRun()
            if index == 3:
                deep_themes = self._extract_themes(all_articles, name)
            query = baseline[index] if index < 3 else f'{name} {deep_themes[index - 3]} details analysis impact {current_month} past month {date_context}'
            stage = f'search_{index + 1}'
            payload = load(stage)
            if payload is None:
                payload = self.search(query, start, end, 15 if index < 3 else 10, stop)
                save(stage, payload)
            articles = [Article.model_validate(a) for a in payload['articles']]
            if any(not start <= a.published_at < end for a in articles):
                raise DataQualityError('Cached news is outside the report window')
            all_articles.extend(articles)
            searches.append({'query': payload['query'], 'skipped': payload['skipped'], 'articles': len(articles)})
        unique = deduplicate(all_articles)
        selected = unique[:self.settings.max_articles]
        notes = ['Five targeted searches; search results are not exhaustive company coverage.']
        skipped = sum(s['skipped'] for s in searches)
        if skipped:
            notes.append(f'Excluded {skipped} results with missing/invalid dates, invalid content, or dates outside the window.')
        if len(unique) > len(selected):
            notes.append(f'Using the {len(selected)} most recently published unique articles from {len(unique)} results.')
        if not selected:
            notes.append('No usable dated news found in the report window; no LLM narrative generated.')
        return {'articles': [a.model_dump(mode='json') for a in selected], 'searches': searches, 'notes': notes}

    def _extract_themes(self, articles, company_name):
        """Extract top recurring themes from article titles and summaries.

        Simple keyword frequency approach:
        - Count keyword occurrences across all titles/summaries
        - Filter out generic terms and company name
        - Return top 3 themes as search keywords

        Returns:
            list: Top 3-5 theme keywords (e.g., ['earnings', 'AI chip', 'China'])
        """
        if not articles:
            return ['latest news', 'recent developments']

        # Combine all text
        text = ' '.join([a.title + ' ' + a.summary for a in articles]).lower()

        # Common stopwords to exclude
        stopwords = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'be',
            'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
            'would', 'could', 'should', 'may', 'might', 'must', 'can', 'this',
            'that', 'these', 'those', 'it', 'its', 'their', 'them', 'they',
            company_name.lower(), 'stock', 'shares', 'company', 'announced',
            'reported', 'said', 'news', 'latest', 'new', 'just', 'says'
        }

        # Bigram themes (2-word phrases) - higher signal
        bigram_candidates = [
            'earnings report', 'earnings beat', 'revenue growth', 'profit margin',
            'ai chip', 'ai model', 'ai breakthrough', 'data center',
            'product launch', 'new product', 'price target', 'analyst upgrade',
            'analyst downgrade', 'market share', 'customer win', 'partnership deal',
            'acquisition deal', 'ceo appointed', 'management change', 'insider buy',
            'stock buyback', 'dividend increase', 'guidance raised', 'guidance cut',
            'regulatory approval', 'fda approval', 'patent granted', 'lawsuit filed',
            'china market', 'europe market', 'us market', 'competition threat'
        ]

        # Count bigrams
        theme_counts = {}
        for bigram in bigram_candidates:
            count = text.count(bigram)
            if count > 0:
                theme_counts[bigram] = count

        # Sort by frequency
        sorted_themes = sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)


        # GUARANTEE exactly 2 themes for full 5-search coverage
        # Priority: bigrams > single words > generic fallback

        if len(sorted_themes) >= 2:
            # Happy path: found 2+ bigrams
            result = [theme for theme, _ in sorted_themes[:2]]
            return result

        elif len(sorted_themes) == 1:
            # Found 1 bigram - try to find a second from single words
            import re
            words = re.findall(r'\b[a-z]{4,}\b', text)
            word_counts = {}
            for word in words:
                if word not in stopwords:
                    word_counts[word] = word_counts.get(word, 0) + 1

            sorted_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)
            if sorted_words:
                result = [sorted_themes[0][0], sorted_words[0][0]]
            else:
                # Duplicate the one bigram we found
                result = [sorted_themes[0][0], sorted_themes[0][0]]
            return result

        else:
            # No bigrams found - fall back to single words
            import re
            words = re.findall(r'\b[a-z]{4,}\b', text)
            word_counts = {}
            for word in words:
                if word not in stopwords:
                    word_counts[word] = word_counts.get(word, 0) + 1

            sorted_words = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)

            if len(sorted_words) >= 2:
                result = [sorted_words[0][0], sorted_words[1][0]]
            elif len(sorted_words) == 1:
                result = [sorted_words[0][0], sorted_words[0][0]]
            else:
                # Ultimate fallback: generic themes
                result = ['latest news', 'recent developments']
            return result
