"""Trending Items Poster: collect public stories and review attributed X drafts."""
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('TRENDING_DB', ROOT / '.local/stories.db'))
UTC = dt.timezone.utc

def now():
    return dt.datetime.now(UTC).timestamp()

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'TrendingItemsPoster/0.1 (public news discovery)'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read(4_000_000)

def clean_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Story must have a public HTTP(S) link')
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.startswith('utm_') and k not in ('fbclid', 'gclid')]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', urllib.parse.urlencode(query), ''))

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS stories (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT UNIQUE NOT NULL,
        source TEXT NOT NULL, published REAL NOT NULL, collected REAL NOT NULL,
        score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'new')''')
    c.commit()
    return c

def ingest(stories):
    count = 0
    with connect() as c:
        for s in stories:
            title = re.sub(r'\s+', ' ', html.unescape(s['title'])).strip()
            published = float(s['published'])
            if not title or published < now() - 48 * 3600 or published > now() + 3600:
                continue
            try:
                url = clean_url(s['url'])
            except ValueError:
                continue
            # A discovery score, not a prediction of virality or a fact check.
            age = max(0, (now() - published) / 3600)
            score = round(100 / (1 + age / 8) + 8 * math.log1p(max(0, s.get('engagement', 0))), 2)
            identity = hashlib.sha256(url.encode()).hexdigest()[:20]
            count += c.execute('INSERT OR IGNORE INTO stories(id,title,url,source,published,collected,score) VALUES(?,?,?,?,?,?,?)',
                               (identity, title, url, s['source'], published, now(), score)).rowcount
    return count

def hn(limit=30):
    ids = json.loads(fetch('https://hacker-news.firebaseio.com/v0/topstories.json'))[:limit]
    def item(i):
        s = json.loads(fetch(f'https://hacker-news.firebaseio.com/v0/item/{i}.json'))
        if s and s.get('type') == 'story' and s.get('url') and not s.get('dead') and not s.get('deleted'):
            return dict(title=s['title'], url=s['url'], source='Hacker News', published=s['time'], engagement=s.get('score', 0))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        return [s for s in pool.map(item, ids) if s]

def trends():
    geo = os.environ.get('TRENDS_GEO', 'US')
    if not re.fullmatch('[A-Z]{2}', geo):
        raise ValueError('TRENDS_GEO must be a two-letter uppercase country code')
    root = ET.fromstring(fetch(f'https://trends.google.com/trending/rss?geo={geo}'))
    from email.utils import parsedate_to_datetime
    result = []
    for item in root.findall('./channel/item'):
        ns = '{https://trends.google.com/trending/rss}'
        for news in item.findall(f'{ns}news_item'):
            date = item.findtext('pubDate')
            result.append(dict(title=news.findtext(f'{ns}news_item_title') or item.findtext('title'),
                               url=news.findtext(f'{ns}news_item_url') or '', source='Google Trends discovery',
                               published=parsedate_to_datetime(date).timestamp() if date else now()))
    return result

def gdelt():
    params = urllib.parse.urlencode(dict(query='(technology OR science OR AI) sourcelang:english', mode='artlist', format='json', maxrecords=30, timespan='24h', sort='datedesc'))
    data = json.loads(fetch('https://api.gdeltproject.org/api/v2/doc/doc?' + params))
    return [dict(title=s['title'], url=s['url'], source='GDELT / ' + s.get('domain', 'news'),
                 published=dt.datetime.strptime(s['seendate'], '%Y%m%dT%H%M%SZ').replace(tzinfo=UTC).timestamp())
            for s in data.get('articles', [])]

SOURCES = {'hn': hn, 'trends': trends, 'gdelt': gdelt}

def collect(names):
    results = {}
    for name in names:
        try:
            rows = SOURCES[name]()
            results[name] = dict(received=len(rows), added=ingest(rows))
        except Exception as e:
            # Do not log arbitrary exception bodies, which may include request data.
            results[name] = dict(error=type(e).__name__, detail='Source unavailable; check network access or source response.')
    return results

def stories():
    with connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM stories ORDER BY score DESC LIMIT 300')]

def draft(s):
    title = s['title']
    # X counts a URL as 23 characters. Conservative budget also covers Unicode.
    title = title[:110].rstrip() + ('…' if len(title) > 110 else '')
    return title + '\n' + s['url']

def approve(identity, status):
    if status not in ('new', 'approved', 'dismissed'):
        raise ValueError('Invalid status')
    with connect() as c:
        if c.execute('UPDATE stories SET status=? WHERE id=?', (status, identity)).rowcount != 1:
            raise ValueError('Story not found')

def export():
    return '\n\n---\n\n'.join(draft(s) for s in stories() if s['status'] == 'approved')

STYLE = '''body{margin:0;background:#0d1422;color:#e8edf7;font:16px system-ui}main{max-width:1050px;margin:auto;padding:40px 24px}header{display:flex;justify-content:space-between;align-items:center;gap:20px}h1{font-size:36px;margin:4px 0}h2{font-size:21px}p{line-height:1.6;color:#aebed3}.tag{color:#72dfc0;font-size:12px;letter-spacing:2px}.bar,.card{background:#172237;border:1px solid #2a3b55;border-radius:14px;padding:22px;margin:20px 0}.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.meta{font-size:13px;color:#9cb0cd}button,.button{background:#76e0be;color:#10251e;border:0;border-radius:7px;padding:10px 14px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}.muted{background:#2c3c55;color:#e8edf7}a{color:#8ec9ff}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#101a2c;padding:16px;border-radius:8px}form{display:inline}small{color:#aebed3}input{accent-color:#76e0be}footer{margin-top:30px;color:#91a5c2}'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body, status=200, content_type='text/html; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == '/health':
            self.reply(json.dumps({'ok': True, 'stories': len(stories())}), content_type='application/json')
            return
        if self.path == '/export':
            self.reply(export(), content_type='text/plain; charset=utf-8')
            return
        if self.path != '/':
            self.reply('Not found', 404)
            return
        rows = stories()
        cards = []
        for s in rows:
            esc = html.escape
            age = max(0, int((now() - s['published']) / 3600))
            cards.append(f'''<article class="card"><div class="meta">{esc(s['source'])} · {age}h old · Discovery score {s['score']} · {esc(s['status'])}</div>
<h2><a href="{esc(s['url'], quote=True)}" rel="noreferrer" target="_blank">{esc(s['title'])}</a></h2>
<pre>{esc(draft(s))}</pre><div class="row"><form method="post" action="/status"><input type="hidden" name="id" value="{s['id']}"><input type="hidden" name="status" value="approved"><label><input type="checkbox" name="reviewed" required> I checked the source and this draft</label> <button>Approve draft</button></form>
<form method="post" action="/status"><input type="hidden" name="id" value="{s['id']}"><input type="hidden" name="status" value="dismissed"><button class="muted">Dismiss</button></form></div></article>''')
        self.reply(f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Trending Items Poster</title><style>{STYLE}</style><main>
<header><div><div class="tag">TREND DESK / X</div><h1>Find the story. Make it yours.</h1></div><a class="button muted" href="/export">Export approved</a></header>
<p>A focused news discovery desk. Collect fresh candidates, inspect their sources, and approve attributed drafts for X.</p>
<section class="bar"><div class="row"><form method="post" action="/collect"><button>Collect latest stories</button></form><span>{len(rows)} stories · {sum(s['status']=='approved' for s in rows)} approved drafts</span></div><p>Scores reflect freshness and available engagement, not factual verification or guaranteed reach. GitHub automation can publish eligible stories when enabled. Approval is optional in automatic mode.</p></section>
{''.join(cards) or '<section class="card"><h2>Your next post starts here</h2><p>Collect stories to fill your review queue. No accounts or API keys are needed for discovery.</p></section>'}
<footer>Public discovery sources: Hacker News, Google Trends, GDELT. Always open the original article before approving.</footer></main></html>''')

    def do_POST(self):
        # Local-only service: reject cross-site form submission and DNS rebinding.
        host = self.headers.get('Host', '')
        if host not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            self.reply('Invalid host', 403)
            return
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + host:
            self.reply('Invalid origin', 403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > 4096:
                raise ValueError('Request too large')
            data = urllib.parse.parse_qs(self.rfile.read(length).decode())
            if self.path == '/collect':
                results = collect(['hn', 'trends', 'gdelt'])
                self.reply('<!doctype html><title>Collection results</title><a href="/">Back to review queue</a><pre>' + html.escape(json.dumps(results, indent=2)) + '</pre>')
                return
            if self.path != '/status':
                self.reply('Not found', 404)
                return
            status = data['status'][0]
            if status == 'approved' and data.get('reviewed') != ['on']:
                raise ValueError('Source review is required')
            approve(data['id'][0], status)
            self.send_response(303)
            self.send_header('Location', '/')
            self.end_headers()
        except (ValueError, KeyError):
            self.reply('Invalid request', 400)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('collect')
    p.add_argument('--sources', nargs='+', choices=SOURCES, default=list(SOURCES))
    p = sub.add_parser('serve')
    p.add_argument('--port', type=int, default=8000)
    sub.add_parser('export')
    sub.add_parser('report')
    args = parser.parse_args()
    if args.command == 'collect':
        results = collect(args.sources)
        print(json.dumps(results, indent=2))
        raise SystemExit(1 if any('error' in v for v in results.values()) else 0)
    elif args.command == 'export':
        print(export())
    elif args.command == 'report':
        print(json.dumps(stories(), indent=2))
    else:
        connect().close()
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
        print(f'Review server listening on port {args.port}', flush=True)
        server.serve_forever()

if __name__ == '__main__':
    main()
