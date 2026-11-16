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
import editor
import events
import newsroom
import prepared
import scoring

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


def replace_mentions(text,entities,limit=None):
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
    used=[]
    def substitute(match):
        if match[0].casefold() in acronyms and match[0]!=acronyms[match[0].casefold()]:
            return match[0]
        if match[0].casefold()=='meta' and text[match.end():].lower().startswith('-analysis'):
            return match[0]
        handle=mapping[match[0].casefold()]
        if limit is not None:
            # Editorial mode: tag each account once, at most `limit` accounts, and
            # never as the first characters (X treats that like a reply).
            if match.start()==0 or handle in used or len(used)>=limit:
                return match[0]
            used.append(handle)
        return handle
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
    return min(expires,row['published']+86400),hours


TOP_EVENTS = int(os.environ.get('TOP_EVENTS', '12'))
AI_PER_TICK = int(os.environ.get('AI_PER_TICK', '4'))
AI_DAILY_CALLS = int(os.environ.get('AI_DAILY_CALLS', '250'))
X_LINK_HOSTS = ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com')


def pending_rows(selected_source=None):
    with newsroom.database() as c:
        rows = [dict(r) for r in c.execute('''SELECT s.*,n.body,n.priority,n.views,COALESCE(g.related,0) AS related,
            (SELECT COUNT(DISTINCT source) FROM observations o WHERE o.story_id=s.id) AS coverage
            FROM stories s JOIN news_items n ON n.story_id=s.id LEFT JOIN publications p ON p.story_id=s.id
            LEFT JOIN story_signals g ON g.story_id=s.id
            WHERE p.story_id IS NULL AND s.status IN ('new','approved') AND s.published BETWEEN ? AND ?
            AND (? IS NULL OR s.url=?)''', (app.now()-86400, app.now()+300, selected_source, selected_source))]
    return [r for r in rows if not re.search(r'\b(?:coupon|promo code|discount|deal sale|limited.time offer)\b', r['body'], re.I)]


def posted_rows(hours=24):
    with prepared.database() as c:
        return [dict(r) for r in c.execute('''SELECT s.id,s.title AS body,s.source,s.published,p.attempted,p.post_id,
            pp.text AS posted_text FROM publications p JOIN stories s ON s.id=p.story_id
            LEFT JOIN publication_payloads pp ON pp.story_id=p.story_id
            WHERE p.state='posted' AND p.attempted>?''', (app.now()-hours*3600,))]


def media_tables():
    with prepared.database() as c:
        media = {r['story_id']: r['media_url'] for r in c.execute('SELECT story_id,media_url FROM prepared_posts')}
    with newsroom.database() as c:
        source_media = {r['story_id']: r['media_url'] for r in c.execute('SELECT story_id,media_url FROM story_media')}
        attachments = {r['story_id']: dict(r) for r in c.execute('SELECT * FROM story_attachments')}
    return media, source_media, attachments


def draft(row, entities, extra, tables):
    """Rule-based draft plus media/format/expiry for one source story."""
    media, source_media, attachments = tables
    expires, hours = lifetime(row)
    image = source_media.get(row['id']) or extra.get('media_url') or media.get(row['id'])
    attachment = attachments.get(row['id'])
    video = bool(attachment and attachment['kind'] == 'video')
    if video:
        image = attachment['url']  # Never substitute a video poster as a photo.
    elif attachment and attachment['kind'] == 'none':
        image = None
    image = image if prepared.media_allowed(image) else None
    needs_link = bool(re.search(r'\b(?:download (?:it|the (?:app|report))|read the full (?:report|study)|apply (?:here|now))\b', row['body'], re.I))
    urls = []
    for url in re.findall(r'https?://[^\s<>]+', row['body']):
        try:
            urls.append(app.clean_url(url.rstrip('.,);]')))
        except ValueError:
            pass
    internal = next((u for u in urls if urllib.parse.urlsplit(u).hostname in X_LINK_HOSTS), None)
    selected_url = (urls[0] if urls else row['url']) if needs_link else internal
    external = bool(selected_url and urllib.parse.urlsplit(selected_url).hostname not in X_LINK_HOSTS)
    fmt = (('text_video' if video else 'text_image') if image else 'text')+('_link' if external else '')
    text = compose(row | ({'outbound_url': selected_url} if selected_url else {}), entities, extra.get('context', ''), bool(selected_url))
    return dict(story_id=row['id'], text=text, media_url=image, source_url=row['url'], format=fmt,
                expires_at=expires, hours=hours, source=row['source'])


def representative(items, settings, tables):
    """The story whose wording/media best represents the event."""
    media, source_media, attachments = tables
    def key(row):
        setting = newsroom.source_setting(row['source'], settings)
        attachment = attachments.get(row['id'])
        has_media = bool(source_media.get(row['id']) or media.get(row['id']) or (attachment and attachment['kind'] in ('image', 'video')))
        return (2*has_media + setting.get('priority', row.get('priority', 1.0))
                + (0 if setting.get('aggregator') else .5) - (.4 if setting.get('state_media') else 0)
                + min(len(row['body']), 400)/800)
    return max(items, key=key)


def ai_budget():
    usage = newsroom.state_get('ai_usage', {})
    day = int(app.now()//86400)
    used = usage.get('count', 0) if usage.get('day') == day else 0
    return max(0, AI_DAILY_CALLS-used), day, used


def ai_record(day, used):
    newsroom.state_set('ai_usage', {'day': day, 'count': used+1})


def cached_edit(key):
    with prepared.database() as c:
        row = c.execute('SELECT value,created FROM editor_cache WHERE key=? AND created>?', (key, app.now()-6*3600)).fetchone()
    if not row:
        return None
    value = json.loads(row[0])
    # Provider failures are retried after 30 minutes; real verdicts last 6 hours.
    if 'error' in value and row[1] < app.now()-1800:
        return None
    return value


def save_edit(key, value):
    with prepared.database() as c:
        c.execute('INSERT OR REPLACE INTO editor_cache VALUES(?,?,?)', (key, json.dumps(value), app.now()))
        c.execute('DELETE FROM editor_cache WHERE created<?', (app.now()-86400,))


def outlet_bucket(n):
    return 1 if n <= 1 else 2 if n == 2 else 3 if n <= 4 else 5


def save_queue(ordered, selected_source=None):
    with prepared.database() as c:
        if selected_source:
            positions = {r['story_id']: r['position'] for r in c.execute('SELECT story_id,position FROM prepared_posts')}
            for row in ordered:
                c.execute('DELETE FROM prepared_posts WHERE story_id=?', (row['story_id'],))
        else:
            positions = {}
            c.execute('DELETE FROM prepared_posts')
        for position, row in enumerate(ordered):
            position = positions.get(row['story_id'], position) if selected_source else position
            c.execute('INSERT INTO prepared_posts VALUES(?,?,?,?,?,?,?,?,?,?)', (row['story_id'], row['text'], row['media_url'], row['source_url'], row['format'], row['rank'], row['expires_at'], position, json.dumps(row['reasons']), app.now()))


def summary(row):
    return dict(text=row['text'], format=row['format'], rank=row['rank'], source_url=row['source_url'], reasons=row['reasons'])


def prepare_selected(rows, client, selected_source):
    """Manual 'publish this story' path: one explicit item, rule-based wording."""
    entities = resolve_entities([r['body'] for r in rows], discover_entities([r['body'] for r in rows]) if client is not None else [], client)
    tables = media_tables()
    ordered, skipped = [], []
    for row in rows:
        try:
            item = draft(row, entities, {'context': row['body']}, tables)
            if item['expires_at'] <= app.now():
                continue
            item.update(rank=100.0, reasons=dict(selected=True, format_tier=FORMAT_ORDER[item['format']]))
            ordered.append(item)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            skipped.append({'story_id': row.get('id'), 'error_type': type(error).__name__})
    save_queue(ordered, selected_source)
    return ordered, skipped, entities


def prepare(client=None, selected_source=None):
    """Cluster, score, AI-edit and save the ranked queue (collection time only)."""
    prepared.expire_queue()
    rows = pending_rows(selected_source)
    if selected_source:
        ordered, skipped, entities = prepare_selected(rows, client, selected_source)
        return {'prepared': len(ordered), 'skipped_invalid_items': skipped, 'verified_handles': ['@'+e['handle'] for e in entities],
                'identity_resolution': newsroom.state_get('identity_resolution', {}),
                'identity_discovery': newsroom.state_get('identity_discovery_report', {}),
                'top5': [summary(r) for r in ordered[:5]], 'bottom5': [summary(r) for r in ordered[-5:]]}
    try:
        settings = newsroom.source_settings()
    except (OSError, ValueError, KeyError):
        settings = {}
    trend = newsroom.state_get('trend_terms', {})
    trend_terms = trend.get('terms', {}) if app.now()-trend.get('fetched', 0) < 3*3600 else {}
    weights = newsroom.state_get('learned_weights', {}).get('category', {})
    posted = posted_rows()
    recent_posts = [(events.key_terms(p['posted_text'] or p['body'])[0], (app.now()-p['attempted'])/3600)
                    for p in posted if app.now()-p['attempted'] < 6*3600]
    already = [p['posted_text'] or p['body'] for p in sorted(posted, key=lambda p: -p['attempted'])][:8]
    tables = media_tables()
    pool = rows+[dict(p, posted=True) for p in posted]
    clusters = events.cluster(pool)
    scored, skipped = [], []
    for event in clusters:
        pending = [s for s in event['items'] if not s.get('posted')]
        if not pending:
            continue
        try:
            m = events.metrics(event, app.now())
            update_of = None
            if m['posted']:
                last = max((s for s in event['items'] if s.get('posted')), key=lambda s: s['attempted'])
                old_numbers = set().union(*(events.key_terms(s['body'])[2] for s in event['items'] if s.get('posted')))
                new_numbers = set().union(*(events.key_terms(s['body'])[2] for s in pending))
                # Already covered: only a fresh, material development may become a quoted update.
                if (app.now()-last['attempted'] < 2700 or not (new_numbers-old_numbers) or m['outlets_last_hour'] < 2
                        or not editor.available()):
                    continue
                update_of = dict(text=last['posted_text'] or last['body'], post_id=last['post_id'])
                event = dict(event, items=pending)
                m = events.metrics(event, app.now())
            rep = representative(pending, settings, tables)
            setting = newsroom.source_setting(rep['source'], settings)
            text = '\n'.join([rep['body']]+[s['body'] for s in pending if s is not rep][:6])
            state_only = all(newsroom.source_setting(s['source'], settings).get('state_media') for s in pending)
            attachment = tables[2].get(rep['id'])
            has_media = bool(tables[1].get(rep['id']) or tables[0].get(rep['id']) or (attachment and attachment['kind'] in ('image', 'video')))
            total, category, parts = scoring.score(
                text, m, category_hint=setting.get('category'), priority=setting.get('priority', rep.get('priority', 1.0)),
                media=has_media, trend_terms=trend_terms, recent_posts=recent_posts, names=event['top_names'],
                weights=weights, state_media_only=state_only)
            scored.append(dict(event=event, metrics=m, rep=rep, pending=pending, total=total, category=category,
                               parts=parts, update_of=update_of))
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            skipped.append({'story_id': event['items'][0].get('id'), 'error_type': type(error).__name__})
    scored.sort(key=lambda e: -e['total'])
    top = scored[:TOP_EVENTS]
    # Enrich and resolve identities only for events that could actually be posted.
    enrich = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        jobs = [(e['rep']['id'], executor.submit(article_context, e['rep'])) for e in top if e['rep']['source'] == 'The Verge']
        for identity, future in jobs:
            enrich[identity] = future.result()
    texts = [s['body'] for e in top for s in e['pending']]+[v['context'] for v in enrich.values()]
    discovered = [x for v in enrich.values() for x in v['entities']]
    if client is not None and texts:
        discovered += discover_entities(texts)
    entities = resolve_entities(texts, discovered, client) if texts else []
    budget, day, used = ai_budget()
    saved_models = newsroom.state_get('ai_models', {})
    # Reuse the provider models that worked recently (re-discovered daily).
    if app.now()-saved_models.get('saved', 0) < 86400:
        editor.remember(saved_models)
    calls = 0
    ai_report = dict(enabled=editor.available(), calls=0, cached=0, declined=[], errors=[], budget_left=budget)
    ordered = []
    for e in top:
        rep, m = e['rep'], e['metrics']
        item = None
        # A malformed story must not take the whole event down: fall back to another report.
        for choice in [rep]+[s for s in e['pending'] if s is not rep]:
            try:
                extra = enrich.get(choice['id'], {'context': choice['body'], 'media_url': None})
                item = draft(choice, entities, extra, tables)
                rep = choice
                break
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                skipped.append({'story_id': choice.get('id'), 'error_type': type(error).__name__})
        if item is None or item['expires_at'] <= app.now():
            continue
        try:
            reports = [(events.outlet(s['source']), s['body']) for s in [rep]+[s for s in e['pending'] if s is not rep]]
            key = e['event']['items'][0]['id']+':'+str(outlet_bucket(m['outlets']))+(':update' if e['update_of'] else '')
            verdict = cached_edit(key)
            if verdict is not None:
                ai_report['cached'] += 1
            elif editor.available() and calls < AI_PER_TICK and budget > calls:
                calls += 1
                ai_record(day, used+calls-1)
                try:
                    verdict = editor.edit(reports, already, e['update_of']['text'] if e['update_of'] else None)
                except editor.EditorUnavailable as error:
                    verdict = {'error': str(error)[:300]}
                    ai_report['errors'].append(verdict['error'])
                save_edit(key, verdict)
            reasons = dict(category=e['category'], score_parts=e['parts'], rule_score=e['total'],
                           outlets=m['outlets'], outlets_last_hour=m['outlets_last_hour'], age_hours=round(m['age_hours'], 2),
                           names=e['event']['top_names'], format_tier=FORMAT_ORDER[item['format']],
                           expires_in_hours=round((item['expires_at']-app.now())/3600, 2))
            rank = e['total']
            if verdict and 'error' not in verdict:
                if not verdict['post']:
                    ai_report['declined'].append(dict(text=rep['body'][:120], reason=verdict.get('reason', '')))
                    continue
                item['text'] = replace_mentions(verdict['text'], entities, limit=2)
                if weight(item['text']) > 280:
                    item['text'] = verdict['text']
                item['format'] = item['format'].replace('_link', '')  # links belong in the source reply
                rank += editor.adjustment(verdict)
                reasons.update(ai=dict(newsworthiness=verdict['newsworthiness'], talkability=verdict['talkability'],
                                       provider=verdict.get('provider'), reason=verdict.get('reason', '')),
                               reply=verdict.get('reply', ''))
            elif e['update_of']:
                continue  # Updates are only posted with an AI-verified new development.
            if e['update_of']:
                reasons['quote_of'] = e['update_of']['post_id']
            reasons['breaking'] = scoring.breaking(rank, m, e['category'])
            reasons['source_url'] = rep['url']
            item.update(rank=round(rank, 2), reasons=reasons)
            ordered.append(item)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            skipped.append({'story_id': rep.get('id'), 'error_type': type(error).__name__})
    ai_report['calls'] = calls
    working = editor.remembered()
    if calls and any(working.values()):
        newsroom.state_set('ai_models', dict(working, saved=app.now()))
        ai_report['models'] = working
    ordered.sort(key=lambda r: -r['rank'])
    save_queue(ordered)
    return {'prepared': len(ordered), 'events': len(scored), 'skipped_invalid_items': skipped,
            'verified_handles': ['@'+x['handle'] for x in entities],
            'identity_resolution': newsroom.state_get('identity_resolution', {}),
            'identity_discovery': newsroom.state_get('identity_discovery_report', {}),
            'ai': ai_report,
            'top5': [summary(r) for r in ordered[:5]], 'bottom5': [summary(r) for r in ordered[-5:]]}
