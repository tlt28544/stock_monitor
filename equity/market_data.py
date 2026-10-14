"""Market data provider for institutional report headers."""
import os
import httpx
from datetime import datetime
from .models import TemporaryAPIError, PermanentAPIError, DataQualityError


def request_json(client, method, url, **kwargs):
    """HTTP request with error handling."""
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


class EODHDMarketData:
    """Fetch current price, market cap, and valuation metrics from EODHD."""
    
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False)
    
    def fetch_context(self, ticker):
        """
        Fetch market context: price, market cap, P/E ratio.
        Returns dict with 'price', 'market_cap', 'pe', or None on failure.
        """
        key = os.getenv(self.settings.news_key_env)  # Reuse EODHD key
        if not key:
            return None
        
        try:
            # EODHD real-time quote API
            ticker_code = ticker if '.' in ticker else ticker + '.US'
            raw = request_json(
                self.client, 'GET',
                f'https://eodhd.com/api/real-time/{ticker_code}',
                params={'api_token': key, 'fmt': 'json'}
            )
            
            price = raw.get('close')
            market_cap = raw.get('MarketCapitalization')
            pe = raw.get('PERatio')
            
            # Format nicely
            context = {}
            if price:
                context['price'] = f"${price:.2f}"
            if market_cap:
                # Convert to billions if large
                if market_cap >= 1e9:
                    context['market_cap'] = f"${market_cap / 1e9:.2f}B"
                elif market_cap >= 1e6:
                    context['market_cap'] = f"${market_cap / 1e6:.0f}M"
                else:
                    context['market_cap'] = f"${market_cap:,.0f}"
            if pe:
                context['pe'] = f"{pe:.1f}x"
            
            return context if context else None
        except (TemporaryAPIError, PermanentAPIError, DataQualityError, KeyError, TypeError):
            # Market data is optional; don't fail the entire report
            return None
