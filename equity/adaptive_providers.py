"""Adaptive news provider using Tavily search instead of exhaustive EODHD scraping."""
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Tuple, List
try:
    from typing import Protocol
except ImportError:
    from typing_extensions import Protocol
import httpx
from .models import Article, DataQualityError, TemporaryAPIError, PermanentAPIError


def request_json(client, method, url, **kwargs):
    try:
        response = client.request(method, url, **kwargs)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        raise TemporaryAPIError(type(exc).__name__) from None
    if response.status_code in (408, 429) or response.status_code >= 500:
        raise TemporaryAPIError(f'HTTP {response.status_code}')
    if response.status_code >= 400:
        raise PermanentAPIError(f'HTTP {response.status_code}')
    try:
        return response.json()
    except ValueError:
        raise DataQualityError('API response is not JSON') from None


def retry(operation, settings, on_retry=lambda attempt, error: None):
    for attempt in range(settings.retries + 1):
        try:
            return operation()
        except TemporaryAPIError as exc:
            if attempt == settings.retries:
                raise
            on_retry(attempt+1, str(exc))
            time.sleep(settings.retry_base_seconds * 2**attempt)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Missing publication timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


class AdaptiveTavilyNews:
    """Adaptive search-based news provider. Does 1-3 targeted Tavily searches instead of scraping thousands of articles."""
    
    def __init__(self, settings, charge=lambda operation, cost: None, client=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False)
        self.charge = charge
        self.searches = 0
    
    def call(self, method, url, cost, **kwargs):
        def attempt():
            self.searches += 1
            if self.searches > 5:  # Max 5 searches per ticker (2 fixed + 3 adaptive)
                raise PermanentAPIError('Adaptive search limit reached (5 searches per ticker)')
            self.charge('tavily_search', cost)
            return request_json(self.client, method, url, **kwargs)
        return retry(attempt, self.settings)
    
    def fetch(self, ticker, start, end):
        """3-phase adaptive search: 2 fixed baseline + 1 theme discovery + 2 deep-dives.
        
        Phase 1 (Fixed Baseline - 2 searches):
        1. Strategic/Product Layer - new products, management changes, shareholder buys
        2. Business Fundamentals Layer - AI progress, competitive moat, business strength
        
        Phase 2 (Adaptive Theme Discovery - 1 search):
        3. Discover recent hot topics and trending themes for this company
        
        Phase 3 (Deep Dives - up to 2 searches):
        4-5. Dig deeper into top 2 discovered themes
        
        Total: 2 fixed + 1 discovery + 2 deep-dives = max 5 searches per ticker.
        
        Critical: Focus on events that HAPPENED in the past 30 days (extended from 7 days).
        Use company full name, not ticker symbol.
        Inject today's date for temporal context.
        """
        key = os.getenv(self.settings.tavily_key_env)
        if not key:
            raise PermanentAPIError(f'Missing {self.settings.tavily_key_env}')
        
        # Set ticker for Article creation
        self.ticker = ticker
        
        # Get company full name (load from config)
        company_name = self._get_company_name(ticker)
        
        # Get current date for time-specific queries
        from datetime import datetime
        now = datetime.now()
        current_month = now.strftime('%B %Y')  # e.g., "October 2026"
        today_date = now.strftime('%Y-%m-%d')  # e.g., "2026-10-14"
        
        # Build date context string
        date_context = f"today is {today_date}"
        
        # ========== PHASE 1: Fixed Baseline Searches (2 searches) ==========
        
        # Search 1: Strategic/Product Layer (FIXED)
        # Focus: New products, management changes, shareholder buys, M&A
        articles_1 = self._search(key, 
            f'{company_name} announced launched new product management appointed CEO hired acquired buyback insider buy {current_month} past month {date_context}',
            start, end, max_results=15)
        
        # Search 2: Business Fundamentals Layer (FIXED)
        # Focus: AI progress, technology breakthroughs, competitive wins, customer wins
        articles_2 = self._search(key,
            f'{company_name} AI breakthrough technology innovation partnership deal customer contract competitive advantage {current_month} latest {date_context}',
            start, end, max_results=15)
        
        baseline_articles = articles_1 + articles_2
        
        # ========== PHASE 2: Adaptive Theme Discovery (1 search) ==========
        
        # Search 3: Discover what's trending/hot for this company
        discovery_articles = self._search(key,
            f'{company_name} trending hot topic buzz viral news {current_month} past month what happened {date_context}',
            start, end, max_results=15)
        
        # Combine baseline + discovery for theme extraction
        all_discovery_articles = baseline_articles + discovery_articles
        
        # Extract top themes from all articles so far
        themes = self._extract_themes(all_discovery_articles, company_name)
        
        # ========== PHASE 3: Deep Dives into Top Themes (up to 2 searches) ==========
        
        deep_dive_articles = []
        for theme in themes[:2]:  # Max 2 deep-dive searches
            if self.searches >= 5:  # Hard cap at 5 total searches
                break
            
            # Search 4-5: Deep dive into specific theme
            theme_articles = self._search(key,
                f'{company_name} {theme} details analysis impact {current_month} past month {date_context}',
                start, end, max_results=10)
            deep_dive_articles.extend(theme_articles)
        
        # Combine all articles
        all_articles = baseline_articles + discovery_articles + deep_dive_articles
        
        # Deduplicate by URL
        seen_urls = set()
        unique_articles = []
        for a in all_articles:
            if a.url not in seen_urls:
                seen_urls.add(a.url)
                unique_articles.append(a)
        
        if not unique_articles:
            raise DataQualityError('Adaptive Tavily search returned no usable articles')
        
        warnings = [
            f'Adaptive mode: {self.searches} searches (2 baseline + 1 discovery + {min(len(themes), 2)} deep-dives)',
            f'Discovered themes: {themes[:2] if themes else "none"}',
            f'Extended time window: past 30 days (today: {today_date})',
            'Focus on event occurrence within target window, not just publication date'
        ]
        
        return unique_articles, warnings
    
    def _get_company_name(self, ticker):
        """Get company full name from ticker symbol.
        
        Loads from config/company_names.json mapping.
        Falls back to ticker symbol if not found.
        
        Args:
            ticker: Stock ticker (e.g., 'AAPL', 'NVDA.US')
        
        Returns:
            str: Company full name (e.g., 'Apple', 'Nvidia')
        """
        import json
        import os
        
        # Strip exchange suffix if present
        base_ticker = ticker.split('.')[0]
        
        # Load company names mapping
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'config',
            'company_names.json'
        )
        
        try:
            with open(config_path, 'r') as f:
                company_names = json.load(f)
            return company_names.get(base_ticker, base_ticker)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            # Fallback to ticker if config not found
            return base_ticker
    
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
            return []
        
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
        
        # DEBUG: Print what we found
        import sys
        print(f"\n[DEBUG] Ticker={getattr(self, 'ticker', 'UNKNOWN')} Found {len(sorted_themes)} bigrams: {sorted_themes[:5]}", file=sys.stderr, flush=True)
        
        # GUARANTEE exactly 2 themes for full 5-search coverage
        # Priority: bigrams > single words > generic fallback
        
        if len(sorted_themes) >= 2:
            # Happy path: found 2+ bigrams
            result = [theme for theme, _ in sorted_themes[:2]]
            print(f"[DEBUG] Ticker={getattr(self, 'ticker', 'UNKNOWN')} Returning {len(result)} themes: {result}", file=sys.stderr, flush=True)
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
            print(f"[DEBUG] Ticker={getattr(self, 'ticker', 'UNKNOWN')} Returning {len(result)} themes: {result}", file=sys.stderr, flush=True)
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
            print(f"[DEBUG] Ticker={getattr(self, 'ticker', 'UNKNOWN')} Returning {len(result)} themes: {result}", file=sys.stderr, flush=True)
            return result
    
    def _search(self, key, query, start, end, max_results):
        """Execute one Tavily search with 30-day recency focus (extended from 7 days)."""
        raw = self.call('POST', 'https://api.tavily.com/search', 
                       self.settings.tavily_request_cost_usd,
                       json={
                           'api_key': key,
                           'query': query,
                           'topic': 'news',
                           'search_depth': 'advanced',
                           'max_results': max_results,
                           'days': 30,  # Extended to 30 days for better coverage
                           # Note: Tavily's date filter helps but isn't perfect
                           # We'll also filter client-side below
                       })
        
        if not isinstance(raw, dict) or not isinstance(raw.get('results'), list):
            raise DataQualityError('Malformed Tavily results')
        
        articles = []
        for row in raw['results']:
            try:
                # Tavily published_date is often missing or unreliable
                # For recent news queries, assume results are recent enough
                if 'published_date' in row and row['published_date']:
                    try:
                        pub_date = timestamp(row['published_date'])
                    except (ValueError, TypeError):
                        # If date parsing fails, use current time
                        pub_date = datetime.now(timezone.utc)
                else:
                    # No date provided, assume recent
                    pub_date = datetime.now(timezone.utc)
                
                a = Article(
                    provider='tavily',
                    article_id=hashlib.sha256(row['url'].encode()).hexdigest(),
                    ticker=self.ticker,
                    title=row['title'],
                    summary=row.get('content', row.get('snippet', ''))[:6000],
                    url=row['url'],
                    published_at=pub_date,
                    retrieved_at=datetime.now(timezone.utc)
                )
                
                # Client-side filter: only keep articles from past 30 days
                days_old = (datetime.now(timezone.utc) - pub_date).days
                if days_old <= 30:
                    articles.append(a)
            except (KeyError, ValueError, TypeError, AttributeError):
                continue  # Skip malformed articles
        
        return articles
