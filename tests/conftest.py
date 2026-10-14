from datetime import date
import pytest
from equity.models import Settings
from equity.store import Store
from equity.pipeline import Pipeline


@pytest.fixture
def settings():
    return Settings(retries=1,retry_base_seconds=0)


@pytest.fixture
def pipeline(tmp_path,settings):
    return Pipeline(settings,tmp_path/'state.sqlite3',tmp_path/'reports',date(2026,10,10),demo=True)


@pytest.fixture
def reference():
    return {'ticker':'TEST','analyst':'Unit Test Analyst','approved':True,'methodology':'sotp',
            'currency':'USD','monetary_unit':'millions','segments':[
                {'name':'Mature','forecast_year':2027,'methodology':'pe','value_basis':'equity',
                 'revenue':'1000','net_margin':'0.20','multiple':'15','multiple_rationale':'Test only',
                 'scope':'Excludes new business','source':'Synthetic test','last_updated':'2026-10-01'},
                {'name':'New business','forecast_year':2027,'methodology':'ps','value_basis':'enterprise',
                 'revenue':'100','multiple':'3','allocated_net_debt':'10','multiple_rationale':'Test only',
                 'scope':'Excludes mature business','source':'Synthetic test','last_updated':'2026-10-01'}],
            'adjustments':[{'name':'Unallocated net cash','kind':'net_cash','amount':'50',
                            'treatment':'Not reflected in segment values','source':'Synthetic test','last_updated':'2026-10-01'},
                           {'name':'Shared costs','kind':'corporate_cost','amount':'-40',
                            'treatment':'Capitalized once, excluded from segment earnings','source':'Synthetic test','last_updated':'2026-10-01'}],
            'diluted_shares':'100','current_share_price':'30','price_timestamp':'2026-10-09T20:00:00+00:00',
            'source':'Synthetic test','last_updated':'2026-10-01','overlap_reviewed':True,
            'coverage_notes':'Nonoverlapping test units','adjustments_reviewed':True,'adjustment_notes':'Unallocated only'}
