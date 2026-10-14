"""Small, Pydantic 2-only configuration and report models."""
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

REPORT_ZONE = ZoneInfo('Asia/Shanghai')


class DataQualityError(Exception):
    pass


class TemporaryAPIError(Exception):
    pass


class PermanentAPIError(Exception):
    pass


class InterruptedRun(Exception):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Settings(StrictModel):
    watchlist: list[str] = Field(default_factory=lambda: ['TSLA', 'AMZN', 'META'], min_length=1)
    concurrency: int = Field(default=4, ge=1, le=16)
    retries: int = Field(default=2, ge=0, le=5)
    retry_base_seconds: float = Field(default=1, ge=0)
    timeout_seconds: float = Field(default=60, gt=0)
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_model: str = 'gpt-4.1-mini'
    llm_key_env: str = 'LLM_API_KEY'
    tavily_key_env: str = 'TAVILY_API_KEY'
    max_output_tokens: int = Field(default=6000, ge=512, le=16000)
    max_articles: int = Field(default=30, ge=1, le=50)

    @field_validator('watchlist')
    @classmethod
    def valid_tickers(cls, tickers):
        if any(not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,11}', t) for t in tickers):
            raise ValueError('Invalid ticker')
        return list(dict.fromkeys(tickers))

    @field_validator('llm_base_url')
    @classmethod
    def valid_base_url(cls, value):
        u = urlsplit(value)
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError('llm_base_url must be an HTTPS URL without credentials or query parameters')
        return value.rstrip('/')


class Article(StrictModel):
    id: str
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=6000)
    url: str
    published_at: datetime

    @field_validator('url')
    @classmethod
    def valid_url(cls, value):
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            raise ValueError('Invalid source URL')
        return value

    @field_validator('published_at')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError('Publication timestamp must include a timezone')
        return value


class NarrativeBlock(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)
    event_ids: list[str] = Field(min_length=1, max_length=30)


class DeepResearch(StrictModel):
    narrative_blocks: list[NarrativeBlock] = Field(max_length=5)


def report_window(report_date: date):
    """30 calendar dates, including report_date, in the scheduling timezone."""
    start = datetime.combine(report_date - timedelta(days=29), datetime.min.time(), REPORT_ZONE)
    end = datetime.combine(report_date + timedelta(days=1), datetime.min.time(), REPORT_ZONE)
    return start, end
