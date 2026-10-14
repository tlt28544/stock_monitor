import hashlib
import re
from difflib import SequenceMatcher
from .models import DataQualityError, Research

CATEGORIES = {
 'earnings': (['earnings','revenue','margin','cash flow','guidance'], 5),
 'regulatory': (['regulatory','regulator','lawsuit','antitrust','recall'], 5),
 'company_announcement': (['announces','announcement','acquisition','investment'], 4),
 'competition': (['competition','competitor','market share','rival'], 4),
 'product': (['product','launch','release','delivery'], 3),
 'industry': (['industry','demand','supply','sector'], 3),
}


def normalize(title):
    return ' '.join(re.findall(r'[a-z0-9]+', title.lower()))


def deduplicate(articles):
    events = []
    for a in sorted(articles, key=lambda a:(a.published_at,a.provider,a.article_id)):
        key = normalize(a.title)
        match = None
        for e in events:
            if any(source['url']==a.url for source in e['sources']) or (a.event_id and a.event_id == e.get('provider_event_id') and a.provider == e.get('provider')) or (
                abs((a.published_at.date()-__import__('datetime').date.fromisoformat(e['date'])).days)<=3 and
                (key == e['normalized_title'] or SequenceMatcher(None,key,e['normalized_title']).ratio() >= .9 or
                 (len(a.summary)>=120 and SequenceMatcher(None,normalize(a.summary[:1100]),normalize(e['summary'])).ratio() >= .95))):
                match = e
                break
        source = {'article_id':a.article_id,'provider':a.provider,'url':a.url,'published_at':a.published_at.isoformat(), 'summary':a.summary[:1000]}
        if match:
            if not any(s['url'] == a.url for s in match['sources']):
                match['sources'].append(source)
        else:
            events.append({'id':hashlib.sha256(f'{a.ticker}|{a.published_at.date()}|{key}'.encode()).hexdigest()[:20],
                           'title':a.title,'date':a.published_at.date().isoformat(),'summary':a.summary[:1100],
                           'normalized_title':key,'provider_event_id':a.event_id,'provider':a.provider,'sources':[source]})
    return events


def rank(events, max_events):
    for e in events:
        text = (e['title']+' '+e['summary']).lower()
        hits = [(name,score) for name,(words,score) in CATEGORIES.items() if any(w in text for w in words)]
        e['category'], e['materiality_score'] = max(hits,key=lambda x:x[1]) if hits else ('other',1)
        e['ranking_rationale'] = 'Deterministic keyword screening; analyst review recommended'
    return sorted(events,key=lambda e:(e['materiality_score'],e['date'],e['id']),reverse=True)[:max_events]


def validate_research(research, events):
    research = Research.model_validate(research)
    known = {e['id']:e for e in events}
    for section in ('executive_summary','investment_implications','what_to_watch','valuation_review'):
        for finding in getattr(research,section):
            if any(ref not in known for ref in finding.event_ids):
                raise DataQualityError('Unknown or missing event citation')
            supported = ' '.join(known[ref]['title']+' '+known[ref]['summary'] for ref in finding.event_ids)
            # Mechanical guard against new numbers, not a substitute for human factual review.
            numbers = re.findall(r'(?<![A-Za-z])\d+(?:[.,]\d+)*%?',finding.text)
            if any(n not in supported for n in numbers):
                raise DataQualityError('Research contains a numeric assertion absent from cited sources')
    return research
