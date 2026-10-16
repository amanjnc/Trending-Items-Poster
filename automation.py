"""Scheduled collection and X publishing with durable duplicate protection."""
import argparse
import json
import os
import time
import app

def authorization():
    """Validate a session locally; never print or save cookie values."""
    cookie = os.environ.get('TWEETKIT_COOKIE')
    if not cookie:
        raise ValueError('Missing credentials: TWEETKIT_COOKIE GitHub Actions secret')
    try:
        from tweetkit_x import TweetKit
        return TweetKit(cookie=cookie, timeout=30)
    except ImportError:
        raise ValueError('Install requirements.txt before using TweetKit-X') from None
    except Exception:
        raise ValueError('Session cookie is incomplete; update TWEETKIT_COOKIE securely') from None


def send(text):
    result = authorization().post(text)
    if result.get('ok') is not True or not str(result.get('id', '')).isdigit():
        # Upstream response bodies may contain sensitive information. Do not log them.
        raise RuntimeError('TweetKit-X did not confirm a successful post')
    return str(result['id'])


def ledger():
    c = app.connect()
    c.execute('CREATE TABLE IF NOT EXISTS publications (story_id TEXT PRIMARY KEY, attempted REAL NOT NULL, state TEXT NOT NULL, post_id TEXT)')
    c.commit()
    return c

def cycle(live=False, daily_limit=400, interval=216, checkpoint=None, collection=None):
    if daily_limit < 1 or interval < 1:
        raise ValueError("Daily limit and interval must be positive")
    results = collection if collection is not None else app.collect(['hn', 'trends'])
    # Recompute freshness now; stored discovery scores otherwise become stale.
    with ledger() as c:
        candidates = [dict(r) for r in c.execute('''SELECT s.* FROM stories s LEFT JOIN publications p ON p.story_id=s.id
            WHERE p.story_id IS NULL AND s.status IN ('new','approved') AND s.published>=?
            AND s.published<=? ORDER BY s.score DESC LIMIT 100''', (app.now()-24*3600, app.now()))]
    if not candidates:
        return {'collection': results, 'publishing': 'no fresh candidates'}
    s = max(candidates, key=lambda s: s['score'] / (1 + (app.now()-s['published'])/28800))
    text = app.draft(s)
    if not live:
        return {'collection': results, 'publishing': 'dry run; nothing posted', 'draft': text}
    # Validate credentials before reserving anything. Never log values or headers.
    authorization()
    with ledger() as c:
        c.execute('BEGIN IMMEDIATE')
        recent = c.execute('SELECT COUNT(*) FROM publications WHERE attempted>?', (app.now()-24*3600,)).fetchone()[0]
        last = c.execute('SELECT MAX(attempted) FROM publications').fetchone()[0]
        if recent >= daily_limit or (last is not None and app.now()-last < interval):
            return {'collection': results, 'publishing': 'waiting: daily limit or posting interval'}
        if c.execute('SELECT 1 FROM publications WHERE state="uncertain"').fetchone():
            return {'collection': results, 'publishing': 'paused: reconcile an uncertain attempt with X before continuing'}
        # Commit before sending. Crash or timeout can never trigger a blind retry.
        inserted = c.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,NULL)', (s['id'], app.now(), 'uncertain')).rowcount
        if not inserted:
            return {'collection': results, 'publishing': 'candidate already reserved'}
    if checkpoint:
        checkpoint()  # Must persist the reservation remotely before any public write.
    try:
        post_id = send(text)
    except Exception:
        return {'collection': results, 'publishing': 'paused: outcome uncertain; inspect X and publication ledger before retrying'}
    with ledger() as c:
        c.execute('UPDATE publications SET state="posted", post_id=? WHERE story_id=?', (post_id, s['id']))
    if checkpoint:
        checkpoint()
    return {'collection': results, 'publishing': 'posted', 'post_id': post_id}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live', action='store_true', help='Enable real X posts; default only previews')
    p.add_argument('--daemon', action='store_true', help='Collect and publish at the configured interval until stopped')
    p.add_argument('--daily-limit', type=int, default=400, help='Maximum attempts per rolling 24 hours')
    p.add_argument('--interval', type=int, default=216, help='Minimum seconds between attempts')
    args = p.parse_args()
    if args.daily_limit < 1 or args.interval < 1:
        p.error('Daily limit and interval must be positive')
    while True:
        try:
            result = cycle(args.live, args.daily_limit, args.interval)
        except ValueError as e:
            print(str(e), flush=True)
            raise SystemExit(1)
        print(json.dumps(result, indent=2), flush=True)
        if not args.daemon:
            break
        time.sleep(args.interval)

if __name__ == '__main__':
    main()
