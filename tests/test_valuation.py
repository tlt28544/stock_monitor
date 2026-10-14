from copy import deepcopy
from datetime import date
from decimal import Decimal
import pytest
from pydantic import ValidationError
from equity.valuation import Reference, calculate
from equity.store import Store

ASOF = date(2026,10,10)


def test_sotp_deterministic_no_debt_double_count(reference):
    v = calculate(reference,ASOF)
    # 1000*.20*15 + (100*3 - 10) + 50 - 40
    assert Decimal(v['equity_value'])==Decimal('3300')
    assert Decimal(v['implied_price'])==Decimal('33')
    assert Decimal(v['upside'])==Decimal('10')


def test_missing_segment_never_treated_as_zero(reference):
    reference['segments'][1]['multiple']=None
    v = calculate(reference,ASOF)
    assert v['equity_value'] is None
    assert v['rows'][1]['value'] is None


@pytest.mark.parametrize('field',['approved','overlap_reviewed','adjustments_reviewed'])
def test_approval_and_overlap_gates(reference,field):
    reference[field]=False
    v = calculate(reference,ASOF)
    assert v['equity_value'] is None
    assert all(row['value'] is None for row in v['rows'])


def test_missing_shares_and_price(reference):
    reference['diluted_shares']=None
    reference['price_timestamp']=None
    v = calculate(reference,ASOF)
    assert v['equity_value'] is not None
    assert v['implied_price'] is None
    assert v['upside'] is None


def test_staleness_and_lookahead(reference):
    assert any('Stale' in w for w in calculate(reference,date(2027,1,1))['warnings'])
    assert calculate(reference,date(2026,9,1))['equity_value'] is None


def test_peak_sales(reference):
    reference['segments']=[{**reference['segments'][0],'methodology':'peak_sales','value_basis':'enterprise',
                           'peak_sales':'2000','success_probability':'.25','discount_factor':'.6','multiple':'4','allocated_net_debt':'0'}]
    reference['adjustments']=[]
    assert Decimal(calculate(reference,ASOF)['equity_value'])==Decimal('1200')


def test_mature_loss_cannot_use_pe(reference):
    reference['segments'][0]['net_margin']='-.10'
    assert calculate(reference,ASOF)['equity_value'] is None


def test_inconsistent_income_na(reference):
    reference['segments'][0]['net_income']='999'
    assert calculate(reference,ASOF)['equity_value'] is None


@pytest.mark.parametrize('value',['NaN','Infinity','-1','0'])
def test_invalid_share_counts(reference,value):
    reference['diluted_shares']=value
    with pytest.raises((ValidationError,ValueError)): Reference.model_validate(reference)


def test_no_debt_adjustment_on_pe(reference):
    reference['segments'][0]['allocated_net_debt']='10'
    with pytest.raises(ValidationError): Reference.model_validate(reference)


def test_analyst_history_preserved(tmp_path,reference):
    store=Store(tmp_path/'db')
    assert store.import_reference('TEST','Analyst',True,reference)==1
    previous=deepcopy(store.reference('TEST'))
    reference['segments'][0]['multiple']='20'
    assert store.import_reference('TEST','Analyst',True,reference)==2
    assert previous['segments'][0]['multiple']=='15'
    with store.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM valuation_reference').fetchone()[0]==2


def test_no_reference_na():
    assert calculate(None,ASOF)['implied_price'] is None
