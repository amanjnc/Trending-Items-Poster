"""Collection-time editorial preparation; never imported by the publisher."""
import concurrent.futures
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.parse

from bs4 import BeautifulSoup
import app
import automation
import newsroom
import prepared

REGISTRY = app.ROOT/'entity_map.json'


def normalized(value):
    return re.sub(r'[^\w]', '', value.casefold())


def contains(text,name):
    return bool(re.search(r'(?<![\w@])'+re.escape(name)+r'(?!\w)',text,re.I))


def resolve_entities(texts, discovered=(), client=None, budget=45):
    """Only curated or explicitly source-linked identities; never guessed handles."""
    identities=json.loads(REGISTRY.read_text())['entities']+list(discovered)
    if client is not None and isinstance(getattr(client,'timeout',None),(int,float)):
        client.timeout=min(client.timeout,8)
    identities=[e|{'names':list(dict.fromkeys(e['names']+[e['handle']]))} for e in identities]
    body='\n'.join(texts)
    unique={}
    for entity in identities:
        if any(contains(body,n) for n in entity['names']):
            unique.setdefault(entity['handle'].lower(),entity)
    result={}
    deadline=time.monotonic()+budget
    for key,entity in unique.items():
        if not re.fullmatch(r'[A-Za-z0-9_]{1,15}',entity['handle']):
            continue
        with prepared.database() as c:
            row=c.execute('SELECT value FROM entity_cache WHERE handle=? AND expires>?',(key,app.now())).fetchone()
        if row:
            cached=json.loads(row[0])
            expected={normalized(n) for n in entity.get('profile_names',entity['names'])}
            if cached.get('valid') and any(normalized(n) in expected for n in cached.get('names',[])):
                result[key]=entity|{'names':list(dict.fromkeys(entity['names']+cached.get('names',[])))}
            continue
        if client is None or time.monotonic()>=deadline:
            continue
        try:
            profile=client.get_user_profile(entity['handle'])
            valid=(profile.get('ok') is True and str(profile.get('id','')).isdigit()
                   and str(profile.get('username','')).casefold()==key
                   and normalized(profile.get('name','')) in {normalized(n) for n in entity.get('profile_names',entity['names'])})
        except Exception:
            valid=False
            profile={}
        cached={'valid':valid,'names':[profile['name']] if valid else []}
        with prepared.database() as c:
            c.execute('INSERT OR REPLACE INTO entity_cache VALUES(?,?,?)',(key,json.dumps(cached),app.now()+(86400 if valid else 21600)))
        if valid:
            result[key]=entity|{'names':list(dict.fromkeys(entity['names']+cached['names']))}
    return list(result.values())


def discover_entities(texts,limit=8):
    """Find unambiguous structured X-account claims, then require profile validation."""
    known=json.loads(REGISTRY.read_text())['entities']
    known_names={normalized(name) for e in known for name in e['names']}
    names=[]
    for text in texts:
        names += re.findall(r'\b[A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){1,3}\b',text)
        names += re.findall(r'^([A-Z][A-Za-z0-9&.-]{2,}) (?=is |has |wants |will |announced |says |said |launches |leaves |ends |plans |joins |stops |raises )',text)
    names=[re.sub(r'^(?:CEO|CTO|CFO|President|Founder|Chairman|Dr|Mr|Ms) ', '', n) for n in names]
    names=list(dict.fromkeys(n for n in names if normalized(n) not in known_names and not n.startswith(('The ','A ','An '))))
    resolved=[]
    missing=[]
    with prepared.database() as c:
        for name in names:
            row=c.execute('SELECT value FROM identity_discovery WHERE name=? AND expires>?',(normalized(name),app.now())).fetchone()
            if row:
                value=json.loads(row[0])
                if value:
                    resolved.append(value)
            elif len(missing)<limit:
                missing.append(name)
    def lookup(name):
        api='https://www.wikidata.org/w/api.php?'
        query=urllib.parse.urlencode(dict(action='wbsearchentities',search=name,language='en',format='json',limit=5))
        data=json.loads(app.fetch(api+query))
        exact=[r for r in data.get('search',[]) if normalized(r.get('label',''))==normalized(name) or normalized(r.get('match',{}).get('text',''))==normalized(name)]
        if len(exact)!=1:
            return None
        identity=exact[0].get('id','')
        if not re.fullmatch(r'Q\d+',identity):
            return None
        query=urllib.parse.urlencode(dict(action='wbgetentities',ids=identity,props='labels|aliases|claims',languages='en',format='json'))
        item=json.loads(app.fetch(api+query)).get('entities',{}).get(identity,{})
        claims=[r for r in item.get('claims',{}).get('P2002',[]) if r.get('rank')!='deprecated' and not r.get('qualifiers',{}).get('P582')]
        preferred=[r for r in claims if r.get('rank')=='preferred']
        handles={r.get('mainsnak',{}).get('datavalue',{}).get('value') for r in preferred or claims}
        handles={h for h in handles if isinstance(h,str) and re.fullmatch(r'[A-Za-z0-9_]{1,15}',h)}
        if len(handles)!=1:
            return None
        label=item.get('labels',{}).get('en',{}).get('value',name)
        aliases=[v.get('value','') for v in item.get('aliases',{}).get('en',[])]
        return dict(names=list(dict.fromkeys([name,label])),profile_names=list(dict.fromkeys([name,label]+aliases)),handle=next(iter(handles)),reference='https://www.wikidata.org/wiki/'+identity)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs=[(n,pool.submit(lookup,n)) for n in missing]
        for name,future in jobs:
            try:
                value=future.result()
                ttl=7*86400 if value else 86400
            except Exception:
                value=None
                ttl=900
            with prepared.database() as c:
                c.execute('INSERT OR REPLACE INTO identity_discovery VALUES(?,?,?)',(normalized(name),json.dumps(value),app.now()+ttl))
            if value:
                resolved.append(value)
    return resolved


def article_context(row):
    """Bounded source enrichment for speaker attribution/images, only The Verge."""
    if row['source']!='The Verge':
        return {'context':row['body'],'media_url':None,'entities':[]}
    p=urllib.parse.urlsplit(row['url'])
    if p.scheme!='https' or p.hostname not in ('www.theverge.com','theverge.com'):
        return {'context':row['body'],'media_url':None,'entities':[]}
    try:
        soup=BeautifulSoup(app.fetch(row['url']),'html.parser')
        article=soup.find('article') or soup.find('main')
        if article is None:
            return {'context':row['body'],'media_url':None,'entities':[]}
        context=article.get_text(' ',strip=True)[:20000]
        media=soup.find('meta',property='og:image')
        image=media.get('content') if media else None
        identities=[]
        for link in article.find_all('a',href=True):
            url=urllib.parse.urlsplit(link['href'])
            match=re.fullmatch(r'/([A-Za-z0-9_]{1,15})/?',url.path)
            name=link.get_text(' ',strip=True).lstrip('@')
            if url.hostname in ('x.com','www.x.com','twitter.com','www.twitter.com') and match and name and contains(row['body'],name):
                identities.append({'names':[name],'profile_names':[name],'handle':match[1],'reference':row['url']})
        return {'context':context,'media_url':image if prepared.media_allowed(image) else None,'entities':identities}
    except Exception:
        return {'context':row['body'],'media_url':None,'entities':[]}


def replace_mentions(text,entities):
    filtered=[]
    for entity in entities:
        if entity['handle'].lower()=='apple' and re.search(r'\b(?:pie|orchard|cider|fruit)\b',text,re.I) and not re.search(r'\b(?:iphone|ipad|macbook|ios|tim cook)\b',text,re.I):
            continue
        filtered.append(entity)
    pairs=sorted([(name,'@'+e['handle']) for e in filtered for name in e['names']],key=lambda p:len(p[0]),reverse=True)
    if not pairs:
        return text
    mapping={name.casefold():handle for name,handle in pairs}
    pattern=r'(?<![\w@])(?:'+'|'.join(re.escape(n) for n,_ in pairs)+r')(?!\w)'
    def substitute(match):
        if match[0].casefold()=='meta' and text[match.end():].lower().startswith('-analysis'):
            return match[0]
        return mapping[match[0].casefold()]
    return re.sub(pattern,substitute,text,flags=re.I)


def role_attribution(text,context,entities):
    if not re.search(r',?\s+(?:the )?CEO says[.!]?$',text,re.I):
        return text
    subject=re.match(r'^(.{1,60}?)\s+(?:is|has|will|wants|plans|leaves|ends|announces|said|says|raises|launches|joins|stops)\b',text,re.I)
    if subject is None:
        return text
    company=re.sub(r'^(?:The|A|An) ', '', subject[1],flags=re.I)
    candidates=set()
    for person in entities:
        for name in person['names']:
            if len(name.split())<2 or normalized(name)==normalized(company):
                continue
            n,c=re.escape(name),re.escape(company)
            evidence=(r'\b'+c+r'[\s\S]{0,35}(?:CEO|chief executive)[\s\S]{0,35}\b'+n+r'\b|'
                      r'\b'+n+r'[\s\S]{0,35}(?:CEO|chief executive)[\s\S]{0,25}\b'+c+r'\b|'
                      r'\b'+n+r'[\s\S]{0,25}\b'+c+r'[\s\S]{0,15}(?:CEO|chief executive)')
            if re.search(evidence,context,re.I):
                candidates.add(person['handle'])
    if len(candidates)==1:
        return re.sub(r',?\s+(?:the )?CEO says[.!]?$',', says @'+next(iter(candidates))+'.',text,flags=re.I)
    return text


def weight(text):
    return sum(1 if ord(ch)<0x1100 else 2 for ch in text)


def compose(row,entities,context='',include_link=False):
    text=re.sub(r'https?://\S+','',row['body'])
    text=re.sub(r'\s+',' ',text).strip()
    text=role_attribution(text,context,entities)
    text=replace_mentions(text,entities)
    # Tighten wording without strengthening uncertainty or inventing quotations.
    for old,new in ((r'\bin order to\b','to'),(r'\bat this point in time\b','now'),(r'\bhas made the announcement that\b','announced that')):
        text=re.sub(old,new,text,flags=re.I)
    text=re.sub(r'\bwants to take on the longevity-startup crowd\b','wants to challenge longevity startups',text,flags=re.I)
    budget=256 if include_link else 280
    if weight(text)>budget:
        sentences=re.split(r'(?<=[.!?])\s+',text)
        chosen=''
        for sentence in sentences:
            candidate=(chosen+' '+sentence).strip()
            if weight(candidate)>budget:
                break
            chosen=candidate
        if not chosen:
            for word in text.split():
                candidate=(chosen+' '+word).strip()
                if weight(candidate+'…')>budget:
                    break
                chosen=candidate
            chosen+='…'
        text=chosen
    sentences=re.split(r'(?<=[.!?])\s+',text,maxsplit=1)
    if len(sentences)==2 and weight(text)+1<=budget:
        text=sentences[0]+'\n\n'+sentences[1]
    return text+('\n'+row.get('outbound_url',row['url']) if include_link else '')


def lifetime(row):
    body=row['body'].lower()
    if re.search(r'\b(?:flash flood|evacuation|air raid|weather warning|outage|live results)\b',body):
        hours=6
    elif re.search(r'\b(?:stock price|share price|bitcoin price|crypto price|market update|tonight|this morning)\b',body):
        hours=12
    elif re.search(r'\b(?:how to|explainer|research|discovery|guide|hands.on review|in.depth review)\b|^why\b|\breview(?: of|:|$)',body):
        hours=120
    elif re.search(r'\b(?:launch|released|announced|partnership|funding|startup|model)\b',body):
        hours=48
    else:
        hours=24
    expires=row['published']+hours*3600
    relative=re.search(r'\b(?:ends|expires|closes|starts) in (\d{1,3}) (minutes?|hours?)\b',body)
    if relative:
        expires=min(expires,row['published']+int(relative[1])*(60 if relative[2].startswith('minute') else 3600))
    dated=re.search(r'\b(?:ends|expires|closes|starts)(?: on| at)? (\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})(Z|[+-]\d{2}:\d{2}))?',body,re.I)
    if dated:
        try:
            if dated[2] and dated[3]:
                stamp=dt.datetime.fromisoformat(dated[1]+'T'+dated[2]+dated[3].upper().replace('Z','+00:00')).timestamp()
            else:
                stamp=(dt.datetime.fromisoformat(dated[1]).replace(tzinfo=dt.timezone.utc)+dt.timedelta(days=1)).timestamp()
            expires=min(expires,stamp)
        except ValueError:
            pass
    return min(expires,row['published']+5*86400),hours


def event_key(row):
    first=row['body'].split('\n',1)[0]
    if first.endswith(':') and len(first)<100:
        return normalized(first)+':'+str(int(row['published']//86400))
    return ''


def prepare(client=None):
    """Prepare and materialize the full ordered queue, only at collection time."""
    with newsroom.database() as c:
        rows=[dict(r) for r in c.execute('''SELECT s.*,n.body,n.priority,n.views,
            (SELECT COUNT(DISTINCT source) FROM observations o WHERE o.story_id=s.id) AS coverage
            FROM stories s JOIN news_items n ON n.story_id=s.id LEFT JOIN publications p ON p.story_id=s.id
            WHERE p.story_id IS NULL AND s.status IN ('new','approved') AND s.published BETWEEN ? AND ?''',(app.now()-5*86400,app.now()))]
    rows=[r for r in rows if not re.search(r'\b(?:coupon|promo code|discount|deal sale|limited.time offer)\b',r['body'],re.I)]
    enrich={}
    # Only a bounded number of external article reads; Telegram already supplies text.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs=[(r['id'],pool.submit(article_context,r)) for r in sorted([v for v in rows if v['source']=='The Verge'],key=lambda v:v['published'],reverse=True)[:8]]
        for identity,future in jobs:
            enrich[identity]=future.result()
    # Context is also used to confirm named speakers; it is never copied wholesale.
    all_text=[r['body'] for r in rows]+[e['context'] for e in enrich.values()]
    discovered=[e for v in enrich.values() for e in v['entities']]
    if client is not None:
        discovered += discover_entities([r['body'] for r in sorted(rows,key=lambda r:r['published'],reverse=True)]+[e['context'] for e in enrich.values()])
    entities=resolve_entities(all_text,discovered,client)
    with prepared.database() as c:
        media={r['story_id']:r['media_url'] for r in c.execute('SELECT story_id,media_url FROM prepared_posts')}
    # New source media is collected into metadata by newsroom; legacy rows have none.
    with newsroom.database() as c:
        source_media={r['story_id']:r['media_url'] for r in c.execute('SELECT story_id,media_url FROM story_media')}
    candidates=[]
    for row in rows:
        expires,hours=lifetime(row)
        if expires<=app.now():
            continue
        extra=enrich.get(row['id'],{'context':row['body'],'media_url':None})
        image=source_media.get(row['id']) or extra['media_url'] or media.get(row['id'])
        image=image if prepared.media_allowed(image) else None
        needs_link=bool(re.search(r'\b(?:download (?:it|the (?:app|report))|read the full (?:report|study)|apply (?:here|now))\b',row['body'],re.I))
        urls=[]
        for url in re.findall(r'https?://[^\s<>]+',row['body']):
            try:
                urls.append(app.clean_url(url.rstrip('.,);]')))
            except ValueError:
                pass
        internal=next((u for u in urls if urllib.parse.urlsplit(u).hostname in ('x.com','www.x.com','twitter.com','www.twitter.com')),None)
        selected_url=(urls[0] if urls else row['url']) if needs_link else internal
        external=bool(selected_url and urllib.parse.urlsplit(selected_url).hostname not in ('x.com','www.x.com','twitter.com','www.twitter.com'))
        fmt=('text_image' if image else 'text')+('_link' if external else '')
        text=compose(row|({'outbound_url':selected_url} if selected_url else {}),entities,extra['context'],bool(selected_url))
        age=max(0,(app.now()-row['published'])/3600)
        format_bonus={'text_image':12,'text':7,'text_link':0,'text_image_link':0}[fmt]
        quality=min(10,len(row['body'].split())/5)
        base=60/(1+age/6)+25*(row['priority']-1)+min(15,math.log1p(row['views']))+min(15,max(0,row['coverage']-1)*5)+quality+format_bonus
        # Urgency cannot rescue a weak item and never dominates the score.
        remaining=expires-app.now()
        urgency=min(5,max(0,1-remaining/(hours*3600))*5) if base>=35 else 0
        reasons=dict(freshness=round(60/(1+age/6),2),source_bonus=25*(row['priority']-1),format_bonus=format_bonus,urgency=round(urgency,2),expires_in_hours=round(remaining/3600,2))
        candidates.append(dict(story_id=row['id'],text=text,media_url=image,source_url=row['url'],format=fmt,rank=round(base+urgency,2),expires_at=expires,reasons=reasons,source=row['source'],event=event_key(row)))
    # Interleave sources and separate quotes from the same event before saving order.
    ordered=[]
    with newsroom.database() as c:
        previous=[dict(r) for r in c.execute('SELECT s.source,n.body,s.published FROM publications p JOIN stories s ON s.id=p.story_id LEFT JOIN news_items n ON n.story_id=s.id WHERE p.state="posted" ORDER BY p.attempted DESC LIMIT 5')]
    recent_sources=[r['source'] for r in reversed(previous[:2])]
    recent_events=[event_key(r) if r['body'] else '' for r in reversed(previous)]
    while candidates:
        selected=max(candidates,key=lambda r:r['rank']-(20 if recent_sources[-2:]==[r['source']]*2 else 0)-(15 if r['event'] and r['event'] in recent_events[-5:] else 0))
        candidates.remove(selected)
        ordered.append(selected)
        recent_sources.append(selected['source'])
        recent_events.append(selected['event'])
    with prepared.database() as c:
        c.execute('DELETE FROM prepared_posts')
        for position,row in enumerate(ordered):
            c.execute('INSERT INTO prepared_posts VALUES(?,?,?,?,?,?,?,?,?,?)',(row['story_id'],row['text'],row['media_url'],row['source_url'],row['format'],row['rank'],row['expires_at'],position,json.dumps(row['reasons']),app.now()))
    return {'prepared':len(ordered),'verified_handles':['@'+e['handle'] for e in entities],
            'top5':[dict(text=r['text'],format=r['format'],rank=r['rank'],source_url=r['source_url'],reasons=r['reasons']) for r in ordered[:5]],
            'bottom5':[dict(text=r['text'],format=r['format'],rank=r['rank'],source_url=r['source_url'],reasons=r['reasons']) for r in ordered[-5:]]}
