"""Deep narrative research synthesis for institutional-grade equity reports."""
import json
import os
from datetime import datetime
from typing import List
from pydantic import BaseModel, Field
from .models import TemporaryAPIError, PermanentAPIError, DataQualityError, BudgetError, StrictModel


class NarrativeBlock(StrictModel):
    """One thematic narrative block with depth and data."""
    title: str = Field(min_length=5, max_length=120, description="Thematic section title")
    content: str = Field(min_length=200, max_length=2500, description="Deep narrative with data, context, and implications")
    event_ids: List[str] = Field(min_items=1, max_items=8, description="Supporting event IDs")
    key_metrics: List[str] = Field(max_items=5, description="Key numbers or metrics mentioned (e.g., 'Q3 deliveries: 486k units', '$25B CapEx')")


class DeepResearch(StrictModel):
    """Institutional-grade narrative research structure."""
    narrative_blocks: List[NarrativeBlock] = Field(min_items=3, max_items=5, description="3-5 thematic narrative sections")
    valuation_review: List[str] = Field(min_items=1, max_items=5, description="Key valuation assumptions to review (no changes, just flags)")


def request_json(client, method, url, **kwargs):
    """HTTP request with error handling."""
    import httpx
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
    """Retry with exponential backoff."""
    import time
    for attempt in range(settings.retries + 1):
        try:
            return operation()
        except TemporaryAPIError as exc:
            if attempt == settings.retries:
                raise
            on_retry(attempt+1, str(exc))
            time.sleep(settings.retry_base_seconds * 2**attempt)


class NarrativeResearch:
    """LLM-based narrative research synthesis for institutional reports."""
    
    def __init__(self, settings, store, ticker, date, version, client=None):
        self.settings = settings
        self.store = store
        self.ticker = ticker
        self.date = date
        self.version = version
        import httpx
        self.client = client or httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False)
    
    def synthesize(self, ticker, events, valuation_context):
        """Generate deep narrative research from events."""
        key = os.getenv(self.settings.llm_key_env)
        if not key:
            raise PermanentAPIError(f'Missing {self.settings.llm_key_env}')
        
        # Build context payload
        payload = json.dumps({
            'ticker': ticker,
            'events': events,
            'valuation_reference_read_only': valuation_context
        }, ensure_ascii=False)
        
        # Institutional narrative prompt
        prompt = '''You are a sell-side equity research analyst writing for institutional investors (hedge funds, long-only funds, pension managers).

Your task: synthesize the provided news events into 3-5 DEEP NARRATIVE BLOCKS that tell the company's story over the past 90 days.

FOCUS PRIORITIES (in order):
1. STRATEGIC MOVES: Major product launches (e.g., new AI models, platforms, services), acquisitions, pivots, leadership changes
2. PRODUCT INNOVATION: What's NEW that changes the competitive landscape or business model
3. MARKET SENTIMENT: Stock movements, investor reactions, competitive threats, market share shifts
4. FINANCIAL PERFORMANCE: Earnings, revenue, margins — but ONLY in context of the strategic narrative
5. REGULATORY/LEGAL: Policy changes, lawsuits, compliance — how they shape the investment thesis

STRUCTURE REQUIREMENTS:
- Each narrative block should focus on ONE major theme (e.g., "Muse AI Launch & Enterprise Push", "CapEx Surge Driven by Physical AI Pivot", "Robotaxi Regulatory Path")
- Lead with WHAT CHANGED or WHAT'S NEW, then provide financial/operational evidence
- Each block must be 200-600 words of CONNECTED NARRATIVE, not bullet points
- Include specific numbers, dates, and metrics from the events
- Connect dots between events to show trends and implications
- Write in institutional analyst tone: data-driven, nuanced, acknowledging uncertainties

EXAMPLE NARRATIVE ARC:
- BAD: "Q2 revenue was $28B, up 26% YoY. EPS was $0.33, missing estimates..."
- GOOD: "Meta's July launch of Muse AI models marks a decisive enterprise pivot, pricing APIs 25% below competitors to capture developer share. This aggressive move comes amid $145B AI investment commitment and targets the $43B small business AI market by 2027. The strategy prioritizes ecosystem lock-in over near-term margins, as evidenced by Q2's 40% cost growth..."

CRITICAL RULES:
- ONLY use data and facts present in the provided events
- If events lack detail on a topic, acknowledge the gap (e.g., "Earnings details remain unreported pending October 21 release")
- Do NOT invent numbers, dates, forecasts, or catalysts
- Do NOT make bullet-point lists - write flowing narrative paragraphs
- Each narrative block MUST cite event_ids for all facts
- key_metrics should extract specific numbers mentioned (e.g., "Muse API pricing: 25% below competitors", "$145B AI investment")

VALUATION REVIEW:
- Identify 1-5 key assumptions institutional investors should revisit based on the narrative
- Frame as questions or assumption checks, NOT as value calculations
- Example: "Review TAM assumptions for enterprise AI given Muse's aggressive pricing and SMB focus"

Think like a Goldman Sachs or Morgan Stanley analyst writing a research update after a major product launch or strategic pivot.'''
        
        if len(payload.encode()) > self.settings.max_prompt_bytes:
            raise DataQualityError('Research prompt exceeds configured byte limit')
        
        # Token estimation
        estimated_input = len(payload.encode()) + len(prompt.encode()) + 3000
        output = self.settings.max_output_tokens * 2  # Need more tokens for narrative depth
        reserved_tokens = estimated_input + output
        reserved_cost = (estimated_input * self.settings.input_usd_per_million + output * self.settings.output_usd_per_million) / 1e6
        
        def attempt():
            ledger = self.store.reserve(self.date, self.version, ticker, 'llm', reserved_tokens, reserved_cost, self.settings)
            raw = request_json(
                self.client, 'POST',
                self.settings.llm_base_url.rstrip('/') + '/chat/completions',
                headers={'Authorization': f'Bearer {key}'},
                json={
                    'model': self.settings.llm_model,
                    'messages': [
                        {'role': 'system', 'content': prompt},
                        {'role': 'user', 'content': payload}
                    ],
                    'max_completion_tokens': output,
                    'response_format': {
                        'type': 'json_schema',
                        'json_schema': {
                            'name': 'deep_research',
                            'strict': True,
                            'schema': DeepResearch.model_json_schema()
                        }
                    }
                }
            )
            
            try:
                usage = raw['usage']
                input_tokens, output_tokens = usage['prompt_tokens'], usage['completion_tokens']
                if not isinstance(input_tokens, int) or not isinstance(output_tokens, int) or min(input_tokens, output_tokens) < 0:
                    raise ValueError()
                cost = (input_tokens * self.settings.input_usd_per_million + output_tokens * self.settings.output_usd_per_million) / 1e6
                self.store.settle(ledger, input_tokens + output_tokens, cost)
                
                choice = raw['choices'][0]
                if choice['finish_reason'] != 'stop':
                    raise DataQualityError('LLM response truncated or refused')
                
                return DeepResearch.model_validate_json(choice['message']['content'])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise DataQualityError('Invalid structured LLM response or usage metadata') from None
        
        return retry(attempt, self.settings)
