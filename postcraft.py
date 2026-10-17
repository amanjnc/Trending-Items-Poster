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
PROFILE_CACHE_VERSION = 2
FORMAT_ORDER = {'text_image': 3, 'text_video': 3, 'text': 2, 'text_image_link': 1, 'text_video_link': 1, 'text_link': 0}


def normalized(value):
    return re.sub(r'[^\w]', '', value.casefold())


def contains(text,name):
    flags=0 if name.isupper() and len(name)<=5 else re.I
    return bool(re.search(r'(?<![\w@])'+re.escape(name)+r'(?!\w)',text,flags))


def mention_names(text,entity):
    """Short aliases require their identity context in this same item."""
    names=list(entity['names'])
    names.extend(alias['name'] for alias in entity.get('contextual_names',[])
                 if re.search(alias['context'],text,re.I))
    return names


def resolve_entities(texts, discovered=(), client=None, budget=45):
    """Only curated or explicitly source-linked identities; never guessed handles."""
    identities=json.loads(REGISTRY.read_text())['entities']+list(discovered)
    if client is not None and isinstance(getattr(client,'timeout',None),(int,float)):
        client.timeout=min(client.timeout,8)
    identities=[e|{'names':list(dict.fromkeys(e['names']+[e['handle']]))} for e in identities]
    unique={}
    for entity in identities:
        if any(contains(text,n) for text in texts for n in mention_names(text,entity)):
            unique.setdefault(entity['handle'].lower(),entity)
    result={}
    diagnostics={}
    deadline=time.monotonic()+budget
    for key,entity in unique.items():
        if not re.fullmatch(r'[A-Za-z0-9_]{1,15}',entity['handle']):
            continue
        with prepared.database() as c:
            row=c.execute('SELECT value FROM entity_cache WHERE handle=? AND expires>?',(key,app.now())).fetchone()
        if row:
            cached=json.loads(row[0])
            expected={normalized(n) for n in entity.get('profile_names',entity['names'])}
            # Retry legacy negatives: the old implementation called a nonexistent SDK method.
            if cached.get('resolver_version')==PROFILE_CACHE_VERSION:
                diagnostics[key]=cached.get('status','unknown')
                if cached.get('valid') and any(normalized(n) in expected for n in cached.get('names',[])):
                    result[key]=entity|{'names':list(dict.fromkeys(entity['names']+cached.get('names',[])))}
                continue
        if client is None or time.monotonic()>=deadline:
            diagnostics[key]='lookup unavailable' if client is None else 'lookup budget exhausted'
            continue
        try:
            profile=client.user_profile(entity['handle'])
            valid=(profile.get('ok') is True and str(profile.get('id','')).isdigit()
                   and str(profile.get('username','')).casefold()==key
                   and normalized(profile.get('name','')) in {normalized(n) for n in entity.get('profile_names',entity['names'])})
            status='verified' if valid else 'identity mismatch' if profile.get('ok') is True else 'lookup failed'
        except Exception:
            valid=False
            profile={}
            status='lookup failed'
        diagnostics[key]=status
        cached={'valid':valid,'names':[profile['name']] if valid else [],'resolver_version':PROFILE_CACHE_VERSION,'status':status}
        with prepared.database() as c:
            c.execute('INSERT OR REPLACE INTO entity_cache VALUES(?,?,?)',(key,json.dumps(cached),app.now()+(86400 if valid else 900 if status=='lookup failed' else 21600)))
        if valid:
            result[key]=entity|{'names':list(dict.fromkeys(entity['names']+cached['names']))}
    newsroom.state_set('identity_resolution',diagnostics)
    return list(result.values())


def entity_candidates(texts):
    """Extract complete names after titles before spending lookups on headlines."""
    people=[]
    other=[]
    token=r"[A-ZÀ-Ž][A-Za-zÀ-ž’'-]+"
    roles=r'(?:Prime Minister|Foreign Minister|Defen[cs]e Secretary|Crown Prince|chief executive|President|Chancellor|Senator|Governor|CEO|CTO|CFO|Founder|Chairman|King|Dr\.?|Mr\.?|Ms\.?)'
    for text in texts:
        people.extend(re.findall(r'\b'+roles+r'\s+('+token+r'(?:\s+(?:'+token+r'|bin|al|el|van|von|de)){0,5})',text))
        other.extend(re.findall(r'\b'+token+r'(?: (?:of |the |and |for )?'+token+r'){1,7}\b',text))
        other.extend(re.findall(r'\b([A-Z][A-Za-z0-9&.-]{2,}) (?=is |has |wants |will |announced |says |said |launches |leaves |ends |plans |joins |stops |raises )',text))
    names=list(dict.fromkeys(n.strip() for n in people+other))
    known=json.loads(REGISTRY.read_text())['entities']
    known_names={normalized(name) for e in known for name in e['names']+[a['name'] for a in e.get('contextual_names',[])]}
    # A recognised title/name should not also generate a failed lookup for the
    # surrounding nationality and job title.
    names=[n for n in names if normalized(n) not in known_names
            and not any(contains(n,p) and n!=p for p in people)
            and not n.startswith(('The ','A ','An ','Read More','On Wednesday','Western Ukraine'))]
    return [n for n in names if not any(n!=full and contains(full,n) for full in names)]


def discover_entities(texts,limit=32):
    """Find unambiguous structured X-account claims, then require profile validation."""
    names=entity_candidates(texts)
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
        query=urllib.parse.urlencode(dict(action='wbgetentities',ids=identity,props='labels|aliases|claims',languages='en|fa|ar|uk|ru|zh|ja|hi|fr',format='json'))
        item=json.loads(app.fetch(api+query)).get('entities',{}).get(identity,{})
        claims=[r for r in item.get('claims',{}).get('P2002',[]) if r.get('rank')!='deprecated' and not r.get('qualifiers',{}).get('P582')]
        preferred=[r for r in claims if r.get('rank')=='preferred']
        handles={r.get('mainsnak',{}).get('datavalue',{}).get('value') for r in preferred or claims}
        handles={h for h in handles if isinstance(h,str) and re.fullmatch(r'[A-Za-z0-9_]{1,15}',h)}
        if len(handles)!=1:
            return None
        label=item.get('labels',{}).get('en',{}).get('value',name)
        aliases=[v.get('value','') for values in item.get('aliases',{}).values() for v in values]
        labels=[v.get('value','') for v in item.get('labels',{}).values()]
        return dict(names=list(dict.fromkeys([name,label])),profile_names=list(dict.fromkeys([name,label]+aliases+labels)),handle=next(iter(handles)),reference='https://www.wikidata.org/wiki/'+identity)
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
    newsroom.state_set('identity_discovery_report',{'candidates':names,'attempted':missing,
                       'resolved_names':[e['names'][0] for e in resolved],'lookup_limit':limit})
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
    pairs=sorted([(name,'@'+e['handle']) for e in filtered for name in mention_names(text,e)],key=lambda p:len(p[0]),reverse=True)
    if not pairs:
        return text
    mapping={name.casefold():handle for name,handle in pairs}
    acronyms={name.casefold():name for name,_ in pairs if name.isupper() and len(name)<=5}
    pattern=r'(?<![\w@])(?:'+'|'.join(re.escape(n) for n,_ in pairs)+r')(?!\w)'
    def substitute(match):
        if match[0].casefold() in acronyms and match[0]!=acronyms[match[0].casefold()]:
            return match[0]
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
    # Telegram line breaks delimit the author's paragraphs. Keep them through
    # mention replacement and length trimming; never split on abbreviations.
    paragraphs=[re.sub(r'\s+',' ',part).strip() for part in re.split(r'\r?\n+',text)]
    text='\n\n'.join(part for part in paragraphs if part)
    text=role_attribution(text,context,entities)
    text=replace_mentions(text,entities)
    # Tighten wording without strengthening uncertainty or inventing quotations.
    for old,new in ((r'\bin order to\b','to'),(r'\bat this point in time\b','now'),(r'\bhas made the announcement that\b','announced that')):
        text=re.sub(old,new,text,flags=re.I)
    text=re.sub(r'\bwants to take on the longevity-startup crowd\b','wants to challenge longevity startups',text,flags=re.I)
    budget=256 if include_link else 280
    if weight(text)>budget:
        chosen=''
        for boundary in re.finditer(r'(?<=[.!?])(?<!\b[A-Z]\.)\s+',text):
            candidate=text[:boundary.start()].rstrip()
            if weight(candidate)>budget:
                break
            chosen=candidate
        if not chosen:
            for word in re.finditer(r'\S+',text):
                candidate=text[:word.end()].rstrip()
                if weight(candidate+'…')>budget:
                    break
                chosen=candidate
            chosen+='…'
        text=chosen
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


def prepare(client=None, selected_source=None):
    """Prepare and materialize the full ordered queue, only at collection time."""
    with newsroom.database() as c:
        rows=[dict(r) for r in c.execute('''SELECT s.*,n.body,n.priority,n.views,
            (SELECT COUNT(DISTINCT source) FROM observations o WHERE o.story_id=s.id) AS coverage
            FROM stories s JOIN news_items n ON n.story_id=s.id LEFT JOIN publications p ON p.story_id=s.id
            WHERE p.story_id IS NULL AND s.status IN ('new','approved') AND s.published BETWEEN ? AND ?
            AND (? IS NULL OR s.url=?)''',(app.now()-5*86400,app.now(),selected_source,selected_source))]
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
        discovery_rows=sorted(rows,key=lambda r:60/(1+max(0,(app.now()-r['published'])/3600)/6)+25*(r['priority']-1),reverse=True)
        discovered += discover_entities([r['body'] for r in discovery_rows]+[e['context'] for e in enrich.values()])
    entities=resolve_entities(all_text,discovered,client)
    with prepared.database() as c:
        media={r['story_id']:r['media_url'] for r in c.execute('SELECT story_id,media_url FROM prepared_posts')}
    # New source media is collected into metadata by newsroom; legacy rows have none.
    with newsroom.database() as c:
        source_media={r['story_id']:r['media_url'] for r in c.execute('SELECT story_id,media_url FROM story_media')}
        attachments={r['story_id']:dict(r) for r in c.execute('SELECT * FROM story_attachments')}
    candidates=[]
    for row in rows:
        expires,hours=lifetime(row)
        if expires<=app.now():
            continue
        extra=enrich.get(row['id'],{'context':row['body'],'media_url':None})
        image=source_media.get(row['id']) or extra['media_url'] or media.get(row['id'])
        attachment=attachments.get(row['id'])
        video=bool(attachment and attachment['kind']=='video')
        if video:
            image=attachment['url']  # Never substitute a video poster as a photo.
        elif attachment and attachment['kind']=='none':
            image=None
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
        fmt=(('text_video' if video else 'text_image') if image else 'text')+('_link' if external else '')
        text=compose(row|({'outbound_url':selected_url} if selected_url else {}),entities,extra['context'],bool(selected_url))
        age=max(0,(app.now()-row['published'])/3600)
        format_bonus=FORMAT_ORDER[fmt]*4
        quality=min(10,len(row['body'].split())/5)
        base=60/(1+age/6)+25*(row['priority']-1)+min(15,math.log1p(row['views']))+min(15,max(0,row['coverage']-1)*5)+quality+format_bonus
        # Urgency cannot rescue a weak item and never dominates the score.
        remaining=expires-app.now()
        urgency=min(5,max(0,1-remaining/(hours*3600))*5) if base>=35 else 0
        reasons=dict(format_tier=FORMAT_ORDER[fmt],freshness=round(60/(1+age/6),2),source_bonus=25*(row['priority']-1),format_bonus=format_bonus,urgency=round(urgency,2),expires_in_hours=round(remaining/3600,2))
        candidates.append(dict(story_id=row['id'],text=text,media_url=image,source_url=row['url'],format=fmt,rank=round(base+urgency,2),expires_at=expires,reasons=reasons,source=row['source'],event=event_key(row)))
    # Interleave sources and separate quotes from the same event before saving order.
    ordered=[]
    with newsroom.database() as c:
        previous=[dict(r) for r in c.execute('SELECT s.source,COALESCE(n.body,s.title) AS body,s.published FROM publications p JOIN stories s ON s.id=p.story_id LEFT JOIN news_items n ON n.story_id=s.id WHERE p.state="posted" ORDER BY p.attempted DESC LIMIT 5')]
    recent_sources=[r['source'] for r in reversed(previous[:2])]
    recent_events=[event_key(r) if r['body'] else '' for r in reversed(previous)]
    while candidates:
        selected=max(candidates,key=lambda r:(FORMAT_ORDER[r['format']],r['rank']-(20 if recent_sources[-2:]==[r['source']]*2 else 0)-(15 if r['event'] and r['event'] in recent_events[-5:] else 0)))
        source_penalty=20 if recent_sources[-2:]==[selected['source']]*2 else 0
        event_penalty=15 if selected['event'] and selected['event'] in recent_events[-5:] else 0
        selected['reasons'].update(source_diversity_penalty=source_penalty,event_diversity_penalty=event_penalty,
                                   selection_score=round(selected['rank']-source_penalty-event_penalty,2))
        candidates.remove(selected)
        ordered.append(selected)
        recent_sources.append(selected['source'])
        recent_events.append(selected['event'])
    with prepared.database() as c:
        if selected_source:
            positions={r['story_id']:r['position'] for r in c.execute('SELECT story_id,position FROM prepared_posts')}
            for row in ordered:
                c.execute('DELETE FROM prepared_posts WHERE story_id=?',(row['story_id'],))
        else:
            positions={}
            c.execute('DELETE FROM prepared_posts')
        for position,row in enumerate(ordered):
            position=positions.get(row['story_id'],position) if selected_source else position
            c.execute('INSERT INTO prepared_posts VALUES(?,?,?,?,?,?,?,?,?,?)',(row['story_id'],row['text'],row['media_url'],row['source_url'],row['format'],row['rank'],row['expires_at'],position,json.dumps(row['reasons']),app.now()))
    return {'prepared':len(ordered),'verified_handles':['@'+e['handle'] for e in entities],
            'identity_resolution':newsroom.state_get('identity_resolution',{}),
            'identity_discovery':newsroom.state_get('identity_discovery_report',{}),
            'top5':[dict(text=r['text'],format=r['format'],rank=r['rank'],source_url=r['source_url'],reasons=r['reasons']) for r in ordered[:5]],
            'bottom5':[dict(text=r['text'],format=r['format'],rank=r['rank'],source_url=r['source_url'],reasons=r['reasons']) for r in ordered[-5:]]}
