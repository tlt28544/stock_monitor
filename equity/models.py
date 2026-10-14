from datetime import datetime, timezone
from typing import Optional, List
try:
    from typing_extensions import Literal
except ImportError:
    from typing_extensions import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, Field, validator


class DataQualityError(Exception):
    pass


class TemporaryAPIError(Exception):
    pass


class PermanentAPIError(Exception):
    pass


class BudgetError(Exception):
    pass


class StrictModel(BaseModel):
    class Config:
        extra = 'forbid'


class Article(StrictModel):
    provider: str = Field(min_length=1)
    article_id: str = Field(min_length=1)
    ticker: str
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=6000)
    url: str
    published_at: datetime
    retrieved_at: datetime
    event_id: Optional[str] = None

    @validator('url')
    @classmethod
    def valid_url(cls, v):
        u = urlparse(v)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            raise ValueError('Source must be an HTTP(S) URL without credentials')
        return v

    @validator('published_at', 'retrieved_at')
    @classmethod
    def aware(cls, v):
        if v.tzinfo is None:
            raise ValueError('Timestamp must include a timezone')
        return v.astimezone(timezone.utc)


class Finding(StrictModel):
    text: str = Field(min_length=1, max_length=420)
    event_ids: List[str] = Field(min_items=1, max_items=4)


class Research(StrictModel):
    executive_summary: List[Finding] = Field(min_items=3, max_items=5)
    investment_implications: List[Finding] = Field(min_items=1, max_items=3)
    what_to_watch: List[Finding] = Field(min_items=1, max_items=3)
    valuation_review: List[Finding] = Field(max_items=3)


class Settings(StrictModel):
    watchlist: List[str] = Field(default_factory=lambda: ['TSLA', 'AMZN', 'META'], min_items=1)
    concurrency: int = Field(default=4, ge=1, le=36)
    full_backfill_days: int = Field(default=28, ge=1)
    incremental_overlap_days: int = Field(default=3, ge=1, le=90)
    retries: int = Field(default=3, ge=0, le=8)
    retry_base_seconds: float = Field(default=1.0, ge=0)
    timeout_seconds: float = Field(default=30, gt=0)
    max_tokens: int = Field(default=120000, ge=1)
    max_cost_usd: float = Field(default=10, gt=0)
    input_usd_per_million: float = Field(default=5, ge=0)
    output_usd_per_million: float = Field(default=20, ge=0)
    max_output_tokens: int = Field(default=2200, ge=512, le=16000)
    max_prompt_bytes: int = Field(default=24000, ge=4000, le=50000)
    max_events: int = Field(default=8, ge=3, le=10)
    news_provider: Literal['eodhd', 'file', 'adaptive_tavily'] = 'eodhd'
    research_mode: Literal['standard', 'narrative'] = 'standard'
    news_file: Optional[str] = None
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_model: str = 'gpt-4.1-mini'
    llm_key_env: str = 'LLM_API_KEY'
    news_key_env: str = 'EODHD_API_KEY'
    tavily_key_env: str = 'TAVILY_API_KEY'
    max_news_requests: int = Field(default=24, ge=1, le=100)
    news_request_cost_usd: float = Field(default=0, ge=0)
    tavily_request_cost_usd: float = Field(default=0.02, ge=0)
    valuation_stale_days: int = Field(default=45, ge=1)
    price_stale_days: int = Field(default=7, ge=1)

    @validator('watchlist')
    @classmethod
    def valid_tickers(cls, values):
        import re
        if any(not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,11}', v) for v in values):
            raise ValueError('Invalid ticker')
        return list(dict.fromkeys(values))


def utcnow():
    return datetime.now(timezone.utc).isoformat()
