"""Public news sources, conservative cleanup, ranked persistent queue and pacing."""
import datetime as dt
import concurrent.futures
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

from bs4 import BeautifulSoup
import app
import automation

CONFIG = app.ROOT / 'sources.json'
STOP = set('the a an and or to of in on for from at by with is are was were has have that this it as be its says said just breaking update'.split())


def config():
    value = json.loads(CONFIG.read_text())
    for source in value['telegram']:
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', source['channel']):
            raise ValueError('Invalid Telegram channel in sources.json')
    return value


def database():
    c = automation.ledger()
    c.executescript('''CREATE TABLE IF NOT EXISTS news_items (
        story_id TEXT PRIMARY KEY, body TEXT NOT NULL, fingerprint TEXT NOT NULL,
        priority REAL NOT NULL, views INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS news_fingerprint ON news_items(fingerprint);
        CREATE TABLE IF NOT EXISTS observations (
        url TEXT PRIMARY KEY, story_id TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reply_actions (
        reply_id TEXT PRIMARY KEY, post_id TEXT NOT NULL, text TEXT NOT NULL,
        state TEXT NOT NULL, attempted REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS reply_scan_posts (post_id TEXT PRIMARY KEY, checked REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS story_media (story_id TEXT PRIMARY KEY, media_url TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS story_attachments (story_id TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT);''')
    return c


def state_get(key, default=None):
    with database() as c:
        row = c.execute('SELECT value FROM worker_state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def state_set(key, value):
    with database() as c:
        c.execute('INSERT OR REPLACE INTO worker_state VALUES(?,?)', (key, json.dumps(value)))


def clean_text(text, source_handles=()):
    """Remove obvious ads/footers; never turn an advertisement into a news claim."""
    text = unicodedata.normalize('NFKC', html.unescape(text))
    text = re.sub(r'[\u200b-\u200f\ufeff]', '', text)
    if re.search(r'\b(sponsored post|paid promotion|paid partnership|use (?:our |my )?(?:promo|referral) code|claim your bonus|join our vip|guaranteed returns)\b', text, re.I):
        return ''
    handles = {h.lower().lstrip('@') for h in source_handles}
    brand_names = {re.sub(r'[^\w]', '', h) for h in handles}
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.sub(r'[^\w]', '', line.lower()) in brand_names:
            continue
        # Standalone channel tags and calls to follow are not story content.
        if re.fullmatch(r'[\W_]*(?:@[\w]+[\W_]*)+', line, re.UNICODE):
            continue
        if re.fullmatch(r'(?:#[\w]+[\s|•,]*)+', line):
            continue
        if re.match(r'^[\W_]*(?:follow|subscribe|join|advertise|advertising|contact us|promo code|buy now|sign up|download our app|support this channel|donate to us)\b', line, re.I):
            continue
        line = re.sub(r'\s+(?:subscribe (?:to|for|now)|follow (?:us|our|@)|join (?:us|our channel))\b.*$', '', line, flags=re.I)
        line = re.sub(r'https?://(?:t\.me|telegram\.me)/(?:joinchat/|\+)[^\s]+', '', line)
        line = re.sub(r'(?:[|•—-]\s*)?@[A-Za-z0-9_]+\s*$', '', line)
        line = re.sub(r'@([A-Za-z0-9_]+)', lambda m: '' if m[1].lower() in handles else m[1], line)
        # Keep meaningful topic words, without importing source hashtag formatting.
        line = re.sub(r'#([\w]+)', r'\1', line)
        line = re.sub(r'^[\W_]*(?:BREAKING|JUST IN|URGENT|UPDATE)\s*[:!—–-]+\s*', '', line, flags=re.I)
        if not re.match(r'^[−-]\d',line):
            line = re.sub(r'^[^\w\"“$€£¥]+', '', line)
        line = re.sub(r'\s+', ' ', line).strip(' |•—')
        if line:
            lines.append(line)
    return '\n'.join(lines).strip()


def signature(text):
    text = re.sub(r'https?://\S+', '', text.lower())
    words = re.findall(r'\w+', unicodedata.normalize('NFKC', text), re.UNICODE)
    return ' '.join(words)


def is_duplicate(a, b):
    a, b = signature(a), signature(b)
    if a == b:
        return True
    # Preserve distinct updates with changed figures.
    if re.findall(r'\d+', a) != re.findall(r'\d+', b):
        return False
    aw, bw = set(a.split())-STOP, set(b.split())-STOP
    if min(len(aw), len(bw)) < 8:
        return False
    overlap = len(aw & bw) / max(1, len(aw | bw))
    return overlap >= .82 or (overlap >= .65 and SequenceMatcher(None, a, b).ratio() >= .9)


def telegram(channel, pages=3, message_id=None):
    rows, before = [], str(int(message_id)+1) if message_id else None
    for _ in range(pages):
        url = 'https://t.me/s/' + channel + (('?before=' + before) if before else '')
        soup = BeautifulSoup(app.fetch(url), 'html.parser')
        messages = soup.select('.tgme_widget_message[data-post]')
        if not messages:
            if not rows:
                raise ValueError('No public Telegram preview available')
            break
        ids = []
        for msg in messages:
            post = msg.get('data-post', '')
            match = re.fullmatch(r'([A-Za-z0-9_]+)/(\d+)', post)
            if not match:
                continue
            ids.append(int(match[2]))
            body = msg.select_one('.tgme_widget_message_text')
            stamp = msg.select_one('time[datetime]')
            if not body or not stamp:
                continue  # media-only messages have no usable news text
            for br in body.find_all('br'):
                br.replace_with('\n')
            try:
                date = dt.datetime.fromisoformat(stamp['datetime'].replace('Z', '+00:00'))
                if date.tzinfo is None:
                    continue
            except ValueError:
                continue
            view = msg.select_one('.tgme_widget_message_views')
            raw = (view.get_text(strip=True) if view else '0').replace(',', '')
            vm = re.fullmatch(r'([\d.]+)([KM]?)', raw, re.I)
            views = int(float(vm[1]) * {'': 1, 'K': 1000, 'M': 1000000}[vm[2].upper()]) if vm else 0
            photo=msg.select_one('.tgme_widget_message_photo_wrap')
            image_match=re.search(r'''background-image:\s*url\(['"]?(.*?)['"]?\)''',photo.get('style','')) if photo else None
            video=msg.select_one('video, .tgme_widget_message_video_wrap, .tgme_widget_message_video_player, .tgme_widget_message_video_thumb')
            video_source=msg.select_one('video[src], video source[src]')
            kind='video' if video else 'image' if image_match else 'none'
            attachment=(video_source.get('src') if video_source else None) if video else (image_match[1] if image_match else None)
            rows.append(dict(title=body.get_text(), url='https://t.me/'+post,
                             source='Telegram @'+channel, published=date.timestamp(), views=views,
                             media_url=html.unescape(image_match[1]) if image_match and not video else None,
                             attachment_kind=kind,attachment_url=html.unescape(attachment) if attachment else None))
        if not ids or str(min(ids)) == before:
            break
        before = str(min(ids))
        if rows and max(r['published'] for r in rows[-len(messages):]) < app.now()-7200:
            break
    return rows


def feed_image(entry):
    for node in entry.iter():
        local=node.tag.rsplit('}',1)[-1]
        if local=='thumbnail' or (local=='content' and node.get('medium')=='image'):
            if node.get('url'):
                return node.get('url')
        if local in ('content','description') and node.text and '<img' in node.text:
            image=BeautifulSoup(node.text,'html.parser').find('img')
            if image:
                return image.get('src')
    return None


def verge(url):
    root = ET.fromstring(app.fetch(url))
    atom = '{http://www.w3.org/2005/Atom}'
    from email.utils import parsedate_to_datetime
    rows = []
    entries = root.findall(atom+'entry')
    if entries:
        for entry in entries:
            title = entry.findtext(atom+'title')
            stamp = entry.findtext(atom+'published') or entry.findtext(atom+'updated')
            links = [link.get('href') for link in entry.findall(atom+'link') if link.get('rel', 'alternate') == 'alternate']
            if title and stamp and links:
                try:
                    date = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    if date.tzinfo is not None:
                        rows.append(dict(title=title, url=links[0], source='The Verge', published=date.timestamp(),media_url=feed_image(entry)))
                except ValueError:
                    continue
    else:
        for entry in root.findall('./channel/item'):
            try:
                date = parsedate_to_datetime(entry.findtext('pubDate') or '')
                if date.tzinfo is None:
                    continue
                rows.append(dict(title=entry.findtext('title'), url=entry.findtext('link'), source='The Verge', published=date.timestamp(),media_url=feed_image(entry)))
            except (ValueError, TypeError):
                continue
    if not rows:
        raise ValueError('No usable RSS entries')
    return rows


def save_attachment(c,identity,row):
    if 'attachment_kind' in row:
        c.execute('INSERT OR REPLACE INTO story_attachments VALUES(?,?,?)',(identity,row['attachment_kind'],row.get('attachment_url')))
        c.execute('DELETE FROM story_media WHERE story_id=?',(identity,))
    if row.get('media_url'):
        c.execute('INSERT OR REPLACE INTO story_media VALUES(?,?)',(identity,row['media_url']))


def enqueue(rows, priority, handles, window_end=None):
    window_end=app.now() if window_end is None else window_end
    counts = dict(added=0, duplicates=0, rejected=0)
    for row in rows:
        body = clean_text(row['title'], handles)
        stamp = row['published']
        if len(signature(body).split()) < 7 or len(body) < 45 or not window_end-7200<stamp<=window_end:
            counts['rejected'] += 1
            continue
        try:
            url = app.clean_url(row['url'])
        except (ValueError, TypeError):
            counts['rejected'] += 1
            continue
        identity = hashlib.sha256(url.encode()).hexdigest()[:20]
        fingerprint = hashlib.sha256(signature(body).encode()).hexdigest()
        with database() as c:
            known = c.execute('SELECT story_id FROM observations WHERE url=?', (url,)).fetchone()
            if known:
                c.execute('UPDATE news_items SET views=MAX(views,?) WHERE story_id=?', (row.get('views',0),known[0]))
                if row.get('media_url') or 'attachment_kind' in row:
                    selected=c.execute('SELECT source,status FROM stories WHERE id=?',(known[0],)).fetchone()
                    posted=c.execute('SELECT 1 FROM publications WHERE story_id=?',(known[0],)).fetchone()
                    if selected and selected[0]==row['source'] and selected[1] in ('new','approved') and not posted:
                        save_attachment(c,known[0],row)
                counts['duplicates'] += 1
                continue
            recent = c.execute('''SELECT s.id,s.title,s.source,p.story_id AS posted,n.priority
                FROM stories s LEFT JOIN news_items n ON n.story_id=s.id
                LEFT JOIN publications p ON p.story_id=s.id WHERE s.published>?''', (app.now()-7*86400,)).fetchall()
            duplicate = next((r for r in recent if is_duplicate(body, r['title'])), None)
            if duplicate:
                counts['duplicates'] += 1
                target = duplicate['id']
                c.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?)', (url, target, row['source']))
                if not duplicate['posted'] and priority > (duplicate['priority'] or 0):
                    c.execute('UPDATE stories SET title=?,url=?,source=?,published=? WHERE id=?', (body,url,row['source'],stamp,target))
                    c.execute('INSERT OR REPLACE INTO news_items VALUES(?,?,?,?,?)', (target,body,fingerprint,priority,row.get('views',0)))
                    c.execute('DELETE FROM story_media WHERE story_id=?',(target,))
                    save_attachment(c,target,row)
                continue
            if c.execute('SELECT 1 FROM stories WHERE id=? OR url=?', (identity,url)).fetchone():
                counts['duplicates'] += 1
                continue
            c.execute('INSERT INTO stories(id,title,url,source,published,collected,score) VALUES(?,?,?,?,?,?,?)',
                      (identity,body,url,row['source'],stamp,app.now(),0))
            c.execute('INSERT INTO news_items VALUES(?,?,?,?,?)', (identity,body,fingerprint,priority,row.get('views',0)))
            c.execute('INSERT INTO observations VALUES(?,?,?)', (url,identity,row['source']))
            save_attachment(c,identity,row)
            counts['added'] += 1
    return counts


def collect(config_value=None):
    cfg = config_value or config()
    window_end=app.now()
    handles = [s['channel'] for s in cfg['telegram']]
    report = {}
    jobs = [('Telegram @'+s['channel'], lambda s=s: telegram(s['channel'], cfg['telegram_pages']), s['priority']) for s in cfg['telegram']]
    jobs += [(s['name'], lambda s=s: verge(s['url']), s['priority']) for s in cfg['rss']]
    # Preferred sources arrive first, so syndicated duplicates use that source's wording.
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        submitted = [(name,pool.submit(fn),priority) for name,fn,priority in jobs]
    for name, future, priority in sorted(submitted, key=lambda j:j[2], reverse=True):
        try:
            rows = future.result()
            report[name] = dict(received=len(rows),window_start=window_end-7200,window_end=window_end,
                                **enqueue(rows, priority, handles,window_end=window_end))
        except Exception as e:
            report[name] = {'error': type(e).__name__, 'detail': 'Source unavailable or response format changed'}
    state_set('last_collection', app.now())
    state_set('collection_report', report)
    return report


def queue():
    with database() as c:
        rows = [dict(r) for r in c.execute('''SELECT s.*,n.body,n.priority,n.views,
             (SELECT COUNT(DISTINCT source) FROM observations o WHERE o.story_id=s.id) AS coverage
             FROM stories s JOIN news_items n ON n.story_id=s.id
             LEFT JOIN publications p ON p.story_id=s.id
             WHERE p.story_id IS NULL AND s.status IN ('new','approved') AND s.published BETWEEN ? AND ?''',
             (app.now()-86400, app.now()))]
        last_sources = [r[0] for r in c.execute('SELECT s.source FROM publications p JOIN stories s ON s.id=p.story_id WHERE p.state="posted" ORDER BY p.attempted DESC LIMIT 2')]
    for row in rows:
        age = max(0, (app.now()-row['published'])/3600)
        row['rank'] = round(60/(1+age/6) + 25*(row['priority']-1) + min(15, math.log1p(row['views']))
                            + min(15, max(0,row['coverage']-1)*5) - (20 if last_sources.count(row['source']) == 2 else 0), 2)
    return sorted(rows, key=lambda r:r['rank'], reverse=True)


def render(row):
    body = re.sub(r'https?://\S+', '', row['body'])
    body = re.sub(r'\s+', ' ', body).strip()
    weight = lambda text: sum(1 if ord(ch)<0x1100 else 2 for ch in text)
    budget = 256  # 280 minus newline and X's 23-character shortened link
    if weight(body) > budget:
        sentences = re.split(r'(?<=[.!?])\s+', body)
        chosen = ''
        for sentence in sentences:
            value = (chosen+' '+sentence).strip()
            if weight(value)>budget:
                break
            chosen = value
        if chosen:
            body = chosen
        else:
            words, chosen = body.split(), ''
            for word in words:
                value = (chosen+' '+word).strip()
                if weight(value+'…')>budget:
                    break
                chosen = value
            body = chosen+'…'
    return body+'\n'+row['url']


def adaptive_interval(items, seconds_to_collection):
    """Choose 2/3/4 minutes; sparse queues are slowed, dense queues accelerated."""
    if items <= 0:
        return 240
    ideal = max(0, seconds_to_collection)/items
    return 120 if ideal <= 120 else 180 if ideal <= 180 else 240


def post_one(checkpoint, daily_limit=400, live=False, next_interval=240):
    if next_interval not in (120,180,240):
        raise ValueError("Posting interval must be 120, 180, or 240 seconds")
    rows = queue()
    if not rows:
        return {'publishing': 'queue empty'}
    row = rows[0]
    if not live:
        return {'publishing': 'preview', 'source': row['source'], 'rank': row['rank'], 'draft': render(row)}
    automation.authorization()
    with database() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM publications WHERE state="uncertain"').fetchone():
            return {'publishing': 'paused: uncertain prior post'}
        if c.execute('SELECT COUNT(*) FROM publications WHERE attempted>?', (app.now()-86400,)).fetchone()[0]>=daily_limit:
            return {'publishing': 'daily limit reached'}
        due = c.execute('SELECT value FROM worker_state WHERE key="next_post_at"').fetchone()
        if due and app.now()<json.loads(due[0]):
            return {'publishing': 'waiting for adaptive interval'}
        last = c.execute('SELECT MAX(attempted) FROM publications').fetchone()[0]
        if last is not None and app.now()-last < 120:
            return {'publishing': 'waiting for minimum interval'}
        if not c.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,NULL)', (row['id'],app.now(),'uncertain')).rowcount:
            return {'publishing': 'already reserved'}
        c.execute('INSERT OR REPLACE INTO worker_state VALUES(?,?)', ('next_post_at',json.dumps(app.now()+next_interval)))
    checkpoint()
    try:
        post_id = automation.send(render(row))
    except Exception:
        return {'publishing': 'paused: posting outcome uncertain'}
    with database() as c:
        c.execute('UPDATE publications SET state="posted",post_id=? WHERE story_id=?', (post_id,row['id']))
    checkpoint()
    return {'publishing':'posted','post_id':post_id,'source':row['source'],'rank':row['rank']}
