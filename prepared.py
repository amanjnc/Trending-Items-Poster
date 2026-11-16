"""Small publishing path: read a prepared row, reserve, send once, persist, exit."""
import json
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

import app
import automation

# Quality over volume: X suppresses accounts that flood low-engagement posts.
DAILY_LIMIT = int(os.environ.get('DAILY_LIMIT', '30'))
MIN_GAP = int(os.environ.get('MIN_GAP_MINUTES', '40'))*60
BREAKING_GAP = int(os.environ.get('BREAKING_GAP_MINUTES', '8'))*60
MIN_SCORE = float(os.environ.get('MIN_SCORE', '60'))
QUIET_HOURS_UTC = os.environ.get('QUIET_HOURS_UTC', '5-10')
# Off by default: posts carry no links at all (main post or reply).
SOURCE_REPLY = os.environ.get('SOURCE_REPLY', 'false').lower() == 'true'


def database():
    c = automation.ledger()
    c.executescript('''CREATE INDEX IF NOT EXISTS publication_time ON publications(attempted);
        CREATE TABLE IF NOT EXISTS prepared_posts (
        story_id TEXT PRIMARY KEY, text TEXT NOT NULL, media_url TEXT,
        source_url TEXT NOT NULL, format TEXT NOT NULL, rank REAL NOT NULL,
        expires_at REAL NOT NULL, position INTEGER NOT NULL, reasons TEXT NOT NULL,
        created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS prepared_position ON prepared_posts(position);
        CREATE TABLE IF NOT EXISTS entity_cache (
        handle TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS identity_discovery (name TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS publication_payloads (story_id TEXT PRIMARY KEY,text TEXT NOT NULL,format TEXT NOT NULL,attempted REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS posting_events (story_id TEXT PRIMARY KEY,phase TEXT NOT NULL,category TEXT NOT NULL,http_status INTEGER,recorded REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS posting_backoff (category TEXT PRIMARY KEY,until_time REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS editor_cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS post_meta (story_id TEXT PRIMARY KEY, post_id TEXT NOT NULL, category TEXT,
        format TEXT, rank REAL, posted REAL NOT NULL, ai INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS post_metrics (post_id TEXT PRIMARY KEY, likes INTEGER, reposts INTEGER,
        replies INTEGER, views INTEGER, checked REAL NOT NULL);''')
    return c


def score_bar(c=None):
    """Minimum rank to publish. Rises as the day fills up and during quiet hours."""
    def count(conn):
        return conn.execute('SELECT COUNT(*) FROM publications WHERE state="posted" AND attempted>?',(app.now()-86400,)).fetchone()[0]
    if c is None:
        with database() as conn:
            posted=count(conn)
    else:
        posted=count(c)
    bar=MIN_SCORE+1.5*max(0,posted-15)
    try:
        start,end=(int(v) for v in QUIET_HOURS_UTC.split('-'))
        hour=app.dt.datetime.fromtimestamp(app.now(),app.UTC).hour
        if (start<=hour<end) if start<=end else (hour>=start or hour<end):
            bar+=15
    except ValueError:
        pass
    return bar


def next_post(story_id=None, min_rank=None):
    with database() as c:
        row = c.execute('''SELECT q.* FROM prepared_posts q JOIN stories s ON s.id=q.story_id
            LEFT JOIN publications p ON p.story_id=q.story_id
            WHERE p.story_id IS NULL AND s.status IN ('new','approved') AND q.expires_at>? AND s.published>?
            AND (? IS NULL OR q.story_id=?) AND (? IS NULL OR q.rank>=?) ORDER BY q.position LIMIT 1''',
            (app.now(),app.now()-86400,story_id,story_id,min_rank,min_rank)).fetchone()
    return dict(row) if row else None


def reasons_of(row):
    try:
        return json.loads(row.get('reasons') or '{}')
    except ValueError:
        return {}


# Exact image hosts of the configured mainstream feeds (no wildcard suffixes).
NEWS_IMAGE_HOSTS = ('ichef.bbci.co.uk','i.guim.co.uk','static01.nyt.com','www.aljazeera.com','image.cnbcfm.com',
                    'a57.foxnews.com','static.foxnews.com','techcrunch.com','thehill.com','media.npr.org',
                    'www.politico.com','static.politico.com')


def media_allowed(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        return p.scheme=='https' and not p.username and not p.password and p.port in (None,443) and (
            host in ('cdn-telegram.org','telesco.pe','cdn.vox-cdn.com','www.theverge.com','platform.theverge.com')+NEWS_IMAGE_HOSTS or
            host.endswith('.telesco.pe') or
            host.endswith('.cdn-telegram.org') or host.endswith('.vox-cdn.com'))
    except (ValueError, TypeError):
        return False


def download_media(url, directory, video=False):
    if not media_allowed(url):
        raise ValueError('Unsupported media host')
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not media_allowed(newurl):
                raise ValueError('Unsupported media redirect')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    req = urllib.request.Request(url,headers={'User-Agent':'TrendingItemsPoster/1.0'})
    limit=20_000_000 if video else 2_500_000
    with urllib.request.build_opener(Redirect).open(req,timeout=30 if video else 10) as r:
        data = r.read(limit+1)
    if len(data)>limit:
        raise ValueError('Media too large')
    if video:
        if len(data)<12 or data[4:8]!=b'ftyp':
            raise ValueError('Unsupported video content')
        suffix='.mp4'
    elif data.startswith(b'\xff\xd8\xff'):
        suffix='.jpg'
    elif data.startswith(b'\x89PNG\r\n\x1a\n'):
        suffix='.png'
    elif data[:6] in (b'GIF87a',b'GIF89a'):
        suffix='.gif'
    elif data[:4]==b'RIFF' and data[8:12]==b'WEBP':
        suffix='.webp'
    else:
        raise ValueError('Unsupported image content')
    path=Path(directory)/('image'+suffix)
    path.write_bytes(data)
    return path


def gate(c,daily_limit,min_gap):
    if c.execute('SELECT 1 FROM publications WHERE state="uncertain"').fetchone():
        return 'paused: uncertain prior post'
    if c.execute('SELECT 1 FROM publications WHERE state="account_blocked" AND attempted>?',(app.now()-1800,)).fetchone():
        return 'paused: X rejected account access; 30-minute cooldown; check session or account restrictions'
    if c.execute('SELECT 1 FROM posting_backoff WHERE until_time>?',(app.now(),)).fetchone():
        return 'X rate-limit cooldown; exiting without posting'
    if c.execute('SELECT COUNT(*) FROM publications WHERE attempted>?',(app.now()-86400,)).fetchone()[0]>=daily_limit:
        return 'daily limit reached'
    last=c.execute('SELECT MAX(attempted) FROM publications').fetchone()[0]
    if last is not None and app.now()-last<min_gap:
        return 'not due yet; exiting without sleeping'


def recover_stale_attempts():
    """Quarantine abandoned sends, never retry them; back off on repeated failures."""
    with database() as c:
        stale=[r[0] for r in c.execute('SELECT story_id FROM publications WHERE state="uncertain" AND attempted<=?',(app.now()-120,))]
        for identity in stale:
            c.execute('UPDATE publications SET state="quarantined_uncertain" WHERE story_id=? AND state="uncertain"',(identity,))
            c.execute('INSERT OR IGNORE INTO posting_events VALUES(?,?,?,?,?)',(identity,'watchdog','stale_attempt_quarantined',None,app.now()))
        recent=[r[0] for r in c.execute('SELECT state FROM publications WHERE attempted>? ORDER BY attempted DESC LIMIT 3',(app.now()-3600,))]
        failed={'media_failed','rejected','account_blocked','quarantined_uncertain'}
        existing=c.execute('SELECT until_time FROM posting_backoff WHERE category="consecutive_failures"').fetchone()
        # Tie the breaker to the last failed attempt, not the current clock. An
        # expired cooldown must not reopen forever from the same old failures.
        latest=c.execute('SELECT MAX(attempted) FROM publications').fetchone()[0]
        until=(latest or 0)+1800
        opened=len(recent)==3 and all(s in failed for s in recent) and until>app.now() and (not existing or existing[0]<until)
        if opened:
            c.execute('INSERT OR REPLACE INTO posting_backoff VALUES(?,?)',('consecutive_failures',until))
    return {'quarantined':stale,'circuit_opened':opened}


def health():
    with database() as c:
        counts={r[0]:r[1] for r in c.execute('SELECT state,COUNT(*) FROM publications WHERE attempted>? GROUP BY state',(app.now()-86400,))}
        cooldowns=[dict(r) for r in c.execute('SELECT category,until_time FROM posting_backoff WHERE until_time>?',(app.now(),))]
        last=c.execute('SELECT MAX(attempted) FROM publications WHERE state="posted"').fetchone()[0]
    return {'last_confirmed_post_at':last,'last24h':counts,'active_cooldowns':cooldowns}


def cleanup_posted():
    """Discard posted queue payloads; retain text/IDs needed for deduplication and replies."""
    with database() as c:
        removed=c.execute('DELETE FROM prepared_posts WHERE story_id IN (SELECT story_id FROM publications WHERE state="posted")').rowcount
        for table in ('story_media','story_attachments'):
            if c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():
                c.execute('DELETE FROM '+table+' WHERE story_id IN (SELECT story_id FROM publications WHERE state="posted")')
        # The stories row already holds the text needed for near-duplicate checks;
        # discard the duplicate editorial payload immediately after success.
        if c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="news_items"').fetchone():
            c.execute('DELETE FROM news_items WHERE story_id IN (SELECT story_id FROM publications WHERE state="posted")')
        c.execute('UPDATE stories SET title=substr(title,1,280) WHERE length(title)>280 AND id IN (SELECT story_id FROM publications WHERE state="posted" AND attempted<?)',(app.now()-7*86400,))
    return removed


def clear_pending():
    """Explicit queue reset: never touch a published or reserved story."""
    with database() as c:
        ids=[r[0] for r in c.execute('''SELECT s.id FROM stories s LEFT JOIN publications p ON p.story_id=s.id
            WHERE p.story_id IS NULL AND s.status IN ('new','approved','expired')''')]
        for identity in ids:
            for table,column in (('prepared_posts','story_id'),('story_media','story_id'),('story_attachments','story_id'),
                                 ('news_items','story_id'),('observations','story_id'),('stories','id')):
                if c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():
                    c.execute('DELETE FROM '+table+' WHERE '+column+'=?',(identity,))
    return len(ids)


def expire_queue():
    """Remove expired pending payloads, retaining small URL/dedup history."""
    with database() as c:
        ids=[r[0] for r in c.execute('''SELECT s.id FROM stories s LEFT JOIN prepared_posts q ON q.story_id=s.id
            LEFT JOIN publications p ON p.story_id=s.id WHERE p.story_id IS NULL AND s.status IN ('new','approved')
            AND (s.published<=? OR q.expires_at<=?)''',(app.now()-86400,app.now()))]
        for identity in ids:
            c.execute('UPDATE stories SET status="expired",title=substr(title,1,280) WHERE id=?',(identity,))
            for table in ('prepared_posts','story_media','story_attachments','news_items'):
                if c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():
                    c.execute('DELETE FROM '+table+' WHERE story_id=?',(identity,))
    return len(ids)


def source_reply_text(context, url):
    """Self-reply carrying the link (links in the main post reduce reach)."""
    def size(text):
        return sum(1 if ord(ch)<0x1100 else 2 for ch in text)+23  # X counts any URL as 23
    label='Source: '
    context=(context or '').strip()
    while context and size(context+'\n\n'+label)>280:
        context=context.rsplit(' ',1)[0].rstrip(',;:')+'…' if ' ' in context else ''
    return (context+'\n\n' if context else '')+label+url


def publish_one(checkpoint, live=False, daily_limit=None, min_gap=None, story_id=None, require_media=False):
    daily_limit=DAILY_LIMIT if daily_limit is None else daily_limit
    expired=expire_queue()
    recovery=recover_stale_attempts() if live else {'quarantined':[],'circuit_opened':False}
    if live and (expired or recovery['quarantined'] or recovery['circuit_opened']):
        checkpoint()
    # An explicitly selected story bypasses the quality bar, never the safety gates.
    bar=None if story_id else score_bar()
    row=next_post(story_id,bar)
    if not row and not live:
        # Dry runs still show the best draft, flagged as below the bar.
        best=next_post(story_id)
        if best:
            return {'publishing':'preview (below quality bar; would not be posted now)','draft':best['text'],'format':best['format'],
                    'rank':best['rank'],'score_bar':bar,'expires_at':best['expires_at'],'source_url':best['source_url']}
    if not row:
        return {'publishing':'nothing in the queue clears the quality bar yet','score_bar':bar}
    reasons=reasons_of(row)
    if min_gap is None:
        min_gap=BREAKING_GAP if reasons.get('breaking') else MIN_GAP
    if not live:
        return {'publishing':'preview','draft':row['text'],'format':row['format'],'rank':row['rank'],'score_bar':bar,
                'breaking':bool(reasons.get('breaking')),'expires_at':row['expires_at'],'source_url':row['source_url']}
    with database() as c:
        blocked=gate(c,daily_limit,min_gap)
    if blocked:
        return {'publishing':blocked}
    automation.authorization()
    with tempfile.TemporaryDirectory() as d:
        image=None
        video=row['format'].startswith('text_video')
        if row['media_url']:
            try:
                image=download_media(row['media_url'],d,video=True) if video else download_media(row['media_url'],d)
            except Exception:
                image=None
        if require_media and not image:
            return {'publishing':'media unavailable; selected post was not sent','ok':False}
        with database() as c:
            c.execute('BEGIN IMMEDIATE')
            blocked=gate(c,daily_limit,min_gap)
            if blocked:
                return {'publishing':blocked}
            source_time=c.execute('SELECT published FROM stories WHERE id=?',(row['story_id'],)).fetchone()[0]
            if row['expires_at']<=app.now() or source_time<=app.now()-86400:
                return {'publishing':'candidate expired; exiting'}
            if not c.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,NULL)',(row['story_id'],app.now(),'uncertain')).rowcount:
                return {'publishing':'already reserved'}
            c.execute('INSERT OR REPLACE INTO publication_payloads VALUES(?,?,?,?)',(row['story_id'],row['text'],row['format'],app.now()))
        checkpoint()
        try:
            options={}
            if image:
                options['image_path']=image
            if reasons.get('quote_of'):
                options['quote_of']=reasons['quote_of']
            post_id=automation.send(row['text'],**options)
        except (automation.MediaUploadFailed,automation.PostingRejected) as error:
            upload=isinstance(error,automation.MediaUploadFailed)
            category='upload_failed' if upload else error.category
            status=None if upload else error.status
            outcome='media_failed' if upload else 'account_blocked' if category=='account_access' else 'rejected'
            with database() as c:
                c.execute('UPDATE publications SET state=? WHERE story_id=?',(outcome,row['story_id']))
                c.execute('INSERT OR REPLACE INTO posting_events VALUES(?,?,?,?,?)',(row['story_id'],'upload' if upload else 'create_tweet',category,status,app.now()))
                if category=='rate_limit':
                    c.execute('INSERT OR REPLACE INTO posting_backoff VALUES(?,?)',('rate_limit',app.now()+1800))
                if outcome!='account_blocked':
                    c.execute('DELETE FROM prepared_posts WHERE story_id=?',(row['story_id'],))
                    for table in ('story_media','story_attachments'):
                        if c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():
                            c.execute('DELETE FROM '+table+' WHERE story_id=?',(row['story_id'],))
            checkpoint()
            return {'publishing':'paused: X rejected account access' if outcome=='account_blocked' else 'media upload failed before posting; item skipped' if upload else 'X rejected this post; item skipped',
                    'failure_category':category,'http_status':status,'source_url':row['source_url']}
        except Exception as error:
            with database() as c:
                category=error.category if isinstance(error,automation.PostOutcomeUncertain) else 'outcome_uncertain'
                status=error.status if isinstance(error,automation.PostOutcomeUncertain) else None
                c.execute('INSERT OR REPLACE INTO posting_events VALUES(?,?,?,?,?)',(row['story_id'],'create_tweet',category,status,app.now()))
            checkpoint()
            return {'publishing':'paused: posting outcome uncertain','failure_category':category,'http_status':status}
        with database() as c:
            c.execute('UPDATE publications SET state="posted",post_id=? WHERE story_id=?',(post_id,row['story_id']))
            c.execute('INSERT OR REPLACE INTO post_meta VALUES(?,?,?,?,?,?,?)',(row['story_id'],post_id,reasons.get('category'),
                      row['format'],row['rank'],app.now(),int(bool(reasons.get('ai')))))
        cleanup_posted()
        checkpoint()
    reply_id=None
    if SOURCE_REPLY and not story_id:
        reply_id=automation.reply(source_reply_text(reasons.get('reply',''),reasons.get('source_url') or row['source_url']),post_id)
    actual_format=(('text_video' if video else 'text_image') if image else 'text')+('_link' if row['format'].endswith('_link') else '')
    return {'publishing':'posted','post_id':post_id,'source_reply_id':reply_id,'rank':row['rank'],'breaking':bool(reasons.get('breaking')),'format':actual_format,'image_uploaded':bool(image) and not video,'video_uploaded':bool(image) and video,'source_url':row['source_url'],'queue_payload_removed':True}
