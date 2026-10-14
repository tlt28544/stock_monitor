"""Write incomplete examples, never estimates or approved assumptions."""
import json
from pathlib import Path


def segment(name, method, scope):
    return {'name':name,'forecast_year':2027,'methodology':method,
            'value_basis':'enterprise' if method=='ps' else 'equity',
            'revenue':None,'net_margin':None,'net_income':None,'multiple':None,
            'allocated_net_debt':None,'multiple_rationale':'Analyst to supply rationale',
            'scope':scope,'source':'Analyst to supply financial source','last_updated':'2026-10-12'}


refs = []
for ticker,method,segments,coverage in [
 ('AMZN','sotp',[
  segment('AWS','pe','Standalone AWS after allocated costs; exclude from Retail'),
  segment('Advertising','pe','Advertising economics carved out of Retail; reconcile reported segment revenue and income'),
  segment('Retail','pe','Retail after removing Advertising contribution and AWS; document allocation of logistics and technology costs'),
  segment('Selected emerging businesses','ps','Named emerging assets excluded from every other segment; no generic optionality premium')],
  'Reconcile AWS, Advertising carve-out, Retail, and named emerging businesses to consolidated totals. No overlap. Shared corporate costs either allocated to segments or capitalized once as an adjustment.'),
 ('MSFT','consolidated_pe',[segment('Consolidated business','pe','Consolidated forward diluted earnings include all operating businesses and corporate costs')],
  'Initial consolidated forward P/E. No separate segment premiums; earnings already reflect finance costs.'),
 ('META','sotp',[segment('Core Business','pe','Exclude Muse revenue, earnings and costs if Muse modeled separately'),
                segment('Muse AI opportunity','ps','Analyst-defined opportunity; first verify existence and economic boundary with sources. No assumed financials.')],
  'Muse is user-requested optionality, not a verified business or revenue forecast. Require evidence and explicit exclusion from Core Business, including costs, before valuing separately.'),
 ('TSLA','custom',[segment('Consolidated business','pe','Analyst to select suitable company-specific methodology')],
  'Analyst to define coverage and justify methodology; no automated valuation estimates.')]:
    refs.append({'ticker':ticker,'analyst':'UNASSIGNED','approved':False,'methodology':method,'currency':'USD',
                 'monetary_unit':'millions','segments':segments,'adjustments':[],
                 'diluted_shares':None,'current_share_price':None,'price_timestamp':None,
                 'source':'Analyst to supply source','last_updated':'2026-10-12','overlap_reviewed':False,
                 'coverage_notes':coverage,'adjustments_reviewed':False,
                 'adjustment_notes':'Only UNALLOCATED net cash/debt may be added. P/E equity values already reflect finance costs. If adding separately valued net cash, remove its interest income from segment earnings. Capitalized shared corporate costs must be excluded from segment earnings if separately deducted. Document zero adjustments explicitly.'})
Path('config/valuation_templates.json').write_text(json.dumps(refs,indent=2)+'\n')
