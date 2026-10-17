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
        CREATE TABLE IF NOT EXISTS identity_discovery (name TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL);''')
    return c


def next_post():
    with database() as c:
        row = c.execute('''SELECT q.* FROM prepared_posts q JOIN stories s ON s.id=q.story_id
            LEFT JOIN publications p ON p.story_id=q.story_id
            WHERE p.story_id IS NULL AND s.status IN ('new','approved') AND q.expires_at>?
            ORDER BY q.position LIMIT 1''', (app.now(),)).fetchone()
    return dict(row) if row else None


def media_allowed(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        return p.scheme=='https' and not p.username and not p.password and p.port in (None,443) and (
            host in ('cdn-telegram.org','cdn.vox-cdn.com','www.theverge.com','platform.theverge.com') or
            host.endswith('.cdn-telegram.org') or host.endswith('.vox-cdn.com'))
    except (ValueError, TypeError):
        return False


def download_media(url, directory):
    if not media_allowed(url):
        raise ValueError('Unsupported media host')
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not media_allowed(newurl):
                raise ValueError('Unsupported media redirect')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    req = urllib.request.Request(url,headers={'User-Agent':'TrendingItemsPoster/1.0'})
    with urllib.request.build_opener(Redirect).open(req,timeout=10) as r:
        data = r.read(2_500_001)
    if len(data)>2_500_000:
        raise ValueError('Image too large')
    if data.startswith(b'\xff\xd8\xff'):
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
    if c.execute('SELECT COUNT(*) FROM publications WHERE attempted>?',(app.now()-86400,)).fetchone()[0]>=daily_limit:
        return 'daily limit reached'
    last=c.execute('SELECT MAX(attempted) FROM publications').fetchone()[0]
    if last is not None and app.now()-last<min_gap:
        return 'not due yet; exiting without sleeping'


def publish_one(checkpoint, live=False, daily_limit=400, min_gap=120):
    row=next_post()
    if not row:
        return {'publishing':'queue empty or expired'}
    if not live:
        return {'publishing':'preview','draft':row['text'],'format':row['format'],'rank':row['rank'],'expires_at':row['expires_at'],'source_url':row['source_url']}
    with database() as c:
        blocked=gate(c,daily_limit,min_gap)
    if blocked:
        return {'publishing':blocked}
    automation.authorization()
    with tempfile.TemporaryDirectory() as d:
        image=None
        if row['media_url']:
            try:
                image=download_media(row['media_url'],d)
            except Exception:
                image=None
        with database() as c:
            c.execute('BEGIN IMMEDIATE')
            blocked=gate(c,daily_limit,min_gap)
            if blocked:
                return {'publishing':blocked}
            if row['expires_at']<=app.now():
                return {'publishing':'candidate expired; exiting'}
            if not c.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,NULL)',(row['story_id'],app.now(),'uncertain')).rowcount:
                return {'publishing':'already reserved'}
        checkpoint()
        try:
            post_id=automation.send(row['text'],image_path=image) if image else automation.send(row['text'])
        except Exception:
            return {'publishing':'paused: posting outcome uncertain'}
        with database() as c:
            c.execute('UPDATE publications SET state="posted",post_id=? WHERE story_id=?',(post_id,row['story_id']))
        checkpoint()
    actual_format=('text_image' if image else 'text')+('_link' if row['format'].endswith('_link') else '')
    return {'publishing':'posted','post_id':post_id,'format':actual_format,'image_uploaded':bool(image),'source_url':row['source_url']}
