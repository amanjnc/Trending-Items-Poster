"""Small publishing path: read a prepared row, reserve, send once, persist, exit."""
import json
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

import app
import automation


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
        CREATE TABLE IF NOT EXISTS posting_backoff (category TEXT PRIMARY KEY,until_time REAL NOT NULL);''')
    return c


def next_post(story_id=None):
    with database() as c:
        row = c.execute('''SELECT q.* FROM prepared_posts q JOIN stories s ON s.id=q.story_id
            LEFT JOIN publications p ON p.story_id=q.story_id
            WHERE p.story_id IS NULL AND s.status IN ('new','approved') AND q.expires_at>? AND s.published>?
            AND s.source NOT IN (SELECT s2.source FROM publications p2 JOIN stories s2 ON s2.id=p2.story_id
                WHERE p2.state="posted" ORDER BY p2.attempted DESC LIMIT 2)
            AND (? IS NULL OR q.story_id=?) ORDER BY q.position LIMIT 1''', (app.now(),app.now()-86400,story_id,story_id)).fetchone()
    return dict(row) if row else None


def media_allowed(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        return p.scheme=='https' and not p.username and not p.password and p.port in (None,443) and (
            host in ('cdn-telegram.org','telesco.pe','cdn.vox-cdn.com','www.theverge.com','platform.theverge.com') or
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


def publish_one(checkpoint, live=False, daily_limit=400, min_gap=120, story_id=None, require_media=False):
    expired=expire_queue()
    if live and expired:
        checkpoint()
    row=next_post(story_id)
    if not row:
        return {'publishing':'queue empty, expired, or waiting for source diversity'}
    if not live:
        return {'publishing':'preview','draft':row['text'],'format':row['format'],'rank':row['rank'],'expires_at':row['expires_at'],'source_url':row['source_url']}
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
            post_id=automation.send(row['text'],image_path=image) if image else automation.send(row['text'])
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
        cleanup_posted()
        checkpoint()
    actual_format=(('text_video' if video else 'text_image') if image else 'text')+('_link' if row['format'].endswith('_link') else '')
    return {'publishing':'posted','post_id':post_id,'format':actual_format,'image_uploaded':bool(image) and not video,'video_uploaded':bool(image) and video,'source_url':row['source_url'],'queue_payload_removed':True}
