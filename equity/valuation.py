"""Analyst-owned valuation inputs. All arithmetic uses Decimal, never an LLM."""
import argparse
import json
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from typing import Optional, List
from typing_extensions import Literal
from pydantic import Field, validator, root_validator, conlist
from .models import StrictModel
from .store import Store


class Segment(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    forecast_year: int = Field(ge=2000, le=2100)
    methodology: Literal['pe','ps','peak_sales','direct_equity']
    value_basis: Literal['equity','enterprise']
    revenue: Optional[str] = None
    net_margin: Optional[str] = None
    net_income: Optional[str] = None
    peak_sales: Optional[str] = None
    success_probability: Optional[str] = None
    discount_factor: Optional[str] = None
    direct_equity_value: Optional[str] = None
    multiple: Optional[str] = None
    multiple_rationale: str = Field(min_length=1, max_length=600)
    # Mandatory for EV methods: subtract allocated net debt to derive segment equity.
    allocated_net_debt: Optional[str] = None
    scope: str = Field(min_length=1, max_length=600)
    source: str = Field(min_length=1, max_length=600)
    last_updated: date

    @validator('revenue','net_margin','net_income','peak_sales','success_probability','discount_factor','direct_equity_value','multiple','allocated_net_debt')
    def decimal_string(cls, v):
        if v is not None and (not Decimal(v).is_finite() or abs(Decimal(v))>Decimal('1e18')):
            raise ValueError('Value must be a finite decimal string, magnitude <= 1e18')
        return v

    @root_validator
    def consistent(cls, values):
        methodology = values.get('methodology')
        value_basis = values.get('value_basis')
        if methodology in ('pe','direct_equity') and value_basis != 'equity':
            raise ValueError('P/E and direct equity values must use equity basis')
        for name in ('success_probability','discount_factor','net_margin'):
            v = values.get(name)
            if v is not None and not (Decimal('-1') if name=='net_margin' else Decimal(0)) <= Decimal(v) <= 1:
                raise ValueError(f'{name} outside allowed range')
        for name in ('revenue','peak_sales','multiple','direct_equity_value'):
            v = values.get(name)
            if v is not None and Decimal(v)<0:
                raise ValueError(f'{name} must be nonnegative')
        if value_basis=='equity' and values.get('allocated_net_debt') not in (None,'0'):
            raise ValueError('Do not subtract debt again from an equity value')
        return values


class Adjustment(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal['net_cash','debt','other_equity','corporate_cost']
    amount: Optional[str]
    treatment: str = Field(min_length=1, max_length=600)
    source: str = Field(min_length=1, max_length=600)
    last_updated: date

    @validator('amount')
    def amount_valid(cls, v):
        if v is not None and (not Decimal(v).is_finite() or abs(Decimal(v)) > Decimal('1e18')):
            raise ValueError('Invalid adjustment amount')
        return v

    @root_validator
    def sign(cls, values):
        amount = values.get('amount')
        kind = values.get('kind')
        if amount is not None and kind in ('debt','corporate_cost') and Decimal(amount)>0:
            raise ValueError('Debt and capitalized corporate costs must be negative equity adjustments')
        return values


class Reference(StrictModel):
    ticker: str = Field(pattern=r'^[A-Z][A-Z0-9.-]{0,11}$')
    analyst: str = Field(min_length=1, max_length=120)
    approved: bool = False
    methodology: Literal['sotp','consolidated_pe','custom']
    currency: str = Field(default='USD', pattern=r'^[A-Z]{3}$')
    monetary_unit: Literal['millions'] = 'millions'
    segments: conlist(Segment, min_items=1, max_items=12)
    adjustments: conlist(Adjustment, max_items=12) = []
    # Shares must use the same millions unit as financial inputs.
    diluted_shares: Optional[str]
    current_share_price: Optional[str]
    price_timestamp: Optional[datetime]
    source: str = Field(min_length=1, max_length=600)
    last_updated: date
    overlap_reviewed: bool = False
    coverage_notes: str = Field(min_length=1, max_length=1200)
    adjustments_reviewed: bool = False
    adjustment_notes: str = Field(min_length=1, max_length=1200)

    @validator('diluted_shares','current_share_price')
    def positive(cls,v):
        if v is not None and (not Decimal(v).is_finite() or not 0 < Decimal(v) <= Decimal('1e18')):
            raise ValueError('Share count and price must be positive finite decimal strings')
        return v

    @validator('price_timestamp')
    def price_aware(cls,v):
        if v is not None and v.tzinfo is None:
            raise ValueError('Price timestamp requires timezone')
        return v

    @root_validator
    def unique(cls, values):
        segments = values.get('segments', [])
        adjustments = values.get('adjustments', [])
        methodology = values.get('methodology')
        if len({s.name for s in segments}) != len(segments):
            raise ValueError('Duplicate segment names')
        if len({a.name for a in adjustments}) != len(adjustments):
            raise ValueError('Duplicate adjustment names')
        if methodology=='consolidated_pe' and (len(segments)!=1 or segments[0].methodology!='pe'):
            raise ValueError('Consolidated P/E requires exactly one P/E segment')
        return values


def display(v, digits=2):
    if v is None:
        return 'N/A'
    with localcontext() as ctx:
        ctx.prec = 64
        return f'{v:,.{digits}f}'


def calculate(raw, as_of, stale_days=45, price_stale_days=7):
    result = {'rows':[], 'equity_value':None, 'implied_price':None, 'upside':None,
              'as_of':as_of.isoformat(), 'input_as_of':None, 'currency':'USD',
              'warnings':[], 'revision':raw.get('revision') if raw else None}
    if raw is None:
        result['warnings'].append('Valuation N/A: no analyst valuation_reference')
        result['rows'] = [{'name':'Not modeled','fundamental':'N/A','driver':'N/A','value':None}]
        return result
    ref = Reference.model_validate({k:v for k,v in raw.items() if k!='revision'})
    result['currency'] = ref.currency
    result['input_as_of'] = ref.last_updated.isoformat()
    if not ref.approved:
        result['warnings'].append('Valuation N/A: assumptions are not analyst approved')
    if not ref.overlap_reviewed or not ref.adjustments_reviewed:
        result['warnings'].append('Valuation N/A: segment overlap or equity adjustment treatment has not been reviewed')
    if ref.last_updated > as_of or any(s.last_updated>as_of for s in ref.segments) or any(a.last_updated>as_of for a in ref.adjustments):
        result['warnings'].append('Valuation N/A: inputs postdate the report (look-ahead)')
    for label, updated in [('reference',ref.last_updated)] + [(s.name,s.last_updated) for s in ref.segments] + [(a.name,a.last_updated) for a in ref.adjustments]:
        if (as_of-updated).days > stale_days:
            result['warnings'].append(f'Stale valuation input: {label}, last updated {updated}')
    if ref.price_timestamp and ref.price_timestamp.astimezone(timezone.utc).date()>as_of:
        result['warnings'].append('Valuation N/A: price postdates report')
    elif ref.price_timestamp and (as_of-ref.price_timestamp.astimezone(timezone.utc).date()).days>price_stale_days:
        result['warnings'].append(f'Stale current price: {ref.price_timestamp.isoformat()}')
    values = []
    with localcontext() as ctx:
        ctx.prec = 64
        for s in ref.segments:
            d = lambda name: Decimal(getattr(s,name)) if getattr(s,name) is not None else None
            value, fundamental = None, 'N/A'
            multiple = d('multiple')
            driver = f'{s.methodology.upper()} {display(multiple)}x ({s.value_basis})'
            if s.methodology=='pe':
                income = d('net_income')
                if income is None and d('revenue') is not None and d('net_margin') is not None:
                    income = d('revenue')*d('net_margin')
                elif income is not None and d('revenue') is not None and d('net_margin') is not None:
                    if abs(income-d('revenue')*d('net_margin')) > max(Decimal('0.01'),abs(income)*Decimal('.001')):
                        result['warnings'].append(f'Valuation N/A: inconsistent net income and revenue/margin for {s.name}')
                fundamental = f'{s.forecast_year} NI {display(income)}m'
                if income is not None and income>0 and multiple is not None:
                    value = income*multiple
            elif s.methodology=='ps':
                revenue = d('revenue')
                fundamental = f'{s.forecast_year} Revenue {display(revenue)}m'
                if revenue is not None and multiple is not None:
                    value = revenue*multiple
            elif s.methodology=='peak_sales':
                peak = d('peak_sales')
                fundamental = f'{s.forecast_year} Peak sales {display(peak)}m'
                probability, discount = d('success_probability'), d('discount_factor')
                driver += f'; PoS {display(probability)}; DF {display(discount)}'
                if all(v is not None for v in (peak,multiple,probability,discount)):
                    value = peak*multiple*probability*discount
            else:
                value = d('direct_equity_value')
                fundamental, driver = f'{s.forecast_year} Analyst equity value', 'Company-specific exception'
            if s.value_basis=='enterprise':
                if d('allocated_net_debt') is None:
                    value = None
                    result['warnings'].append(f'Missing allocated net debt for {s.name}')
                elif value is not None:
                    value -= d('allocated_net_debt')
            if value is None:
                result['warnings'].append(f'Missing or unsuitable valuation driver: {s.name}')
            values.append(value)
            result['rows'].append({'name':s.name,'fundamental':fundamental,'driver':driver,'value':str(value) if value is not None else None})
        for a in ref.adjustments:
            value = Decimal(a.amount) if a.amount is not None else None
            values.append(value)
            if value is None:
                result['warnings'].append(f'Missing equity adjustment: {a.name}')
            result['rows'].append({'name':a.name,'fundamental':a.kind,'driver':'Reviewed equity adjustment','value':a.amount})
        if all(v is not None for v in values) and not any(w.startswith('Valuation N/A:') for w in result['warnings']):
            equity = sum(values, Decimal(0))
            result['equity_value'] = str(equity)
            if ref.diluted_shares is not None:
                price = equity/Decimal(ref.diluted_shares)
                result['implied_price'] = str(price)
                if ref.current_share_price is not None and ref.price_timestamp is not None:
                    result['upside'] = str((price/Decimal(ref.current_share_price)-1)*100)
        if ref.diluted_shares is None:
            result['warnings'].append('Missing diluted shares; implied share price N/A')
        if ref.current_share_price is None or ref.price_timestamp is None:
            result['warnings'].append('Missing dated current share price; upside/downside N/A')
    # Unapproved/review-incomplete/look-ahead assumptions must not publish segment estimates either.
    if any(w.startswith('Valuation N/A:') for w in result['warnings']):
        for row in result['rows']:
            row['value'] = None
    result['coverage_notes'] = ref.coverage_notes
    result['adjustment_notes'] = ref.adjustment_notes
    result['assumptions'] = [{'segment':s.name,'rationale':s.multiple_rationale,'scope':s.scope,'source':s.source,'last_updated':str(s.last_updated)} for s in ref.segments]
    result['price_timestamp'] = ref.price_timestamp.isoformat() if ref.price_timestamp else None
    result['current_share_price'] = ref.current_share_price
    return result


def main():
    parser = argparse.ArgumentParser(description='Explicit analyst import; research never writes valuation_reference')
    parser.add_argument('--import-file', required=True)
    parser.add_argument('--db',default='state/research.sqlite3')
    args = parser.parse_args()
    raw = json.loads(open(args.import_file).read())
    raw = raw if isinstance(raw,list) else [raw]
    refs = [Reference.model_validate(r) for r in raw]
    store = Store(args.db)
    for ref in refs:
        revision = store.import_reference(ref.ticker,ref.analyst,ref.approved,ref.model_dump(mode='json'))
        print(f'{ref.ticker}: saved analyst revision {revision}; approved={ref.approved}')


if __name__=='__main__':
    main()
