"""Bounded reply review; optional likes for retrieved direct replies."""
import app
import automation
import newsroom


def direct_replies(client, post_id):
    """Capture parent IDs omitted by pinned TweetKit's public flattened reply output."""
    parents = {}
    def walk(node):
        if isinstance(node, dict):
            lg = node.get('legacy')
            if isinstance(lg, dict) and lg.get('in_reply_to_status_id_str'):
                identity = lg.get('id_str') or node.get('rest_id')
                if identity:
                    parents[str(identity)] = str(lg['in_reply_to_status_id_str'])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    def capture(response, *args, **kwargs):
        if 'TweetDetail' in response.url and response.url.startswith('https://x.com/'):
            try:
                walk(response.json())
            except (ValueError, TypeError):
                pass
    session = getattr(client, '_session', None)
    previous = list(session.hooks.get('response', [])) if session is not None else []
    if session is not None:
        session.hooks['response'] = previous+[capture]
    try:
        rows = client.get_replies(post_id, limit=100)
    finally:
        if session is not None:
            session.hooks['response'] = previous
    return [r for r in rows if str(r.get('in_reply_to_status_id_str') or parents.get(str(r.get('id')), '')) == str(post_id)]


def scan(checkpoint, auto_like=False):
    client = automation.authorization()
    own_id = client.whoami().get('user_id')
    if isinstance(getattr(client,'timeout',None),(int,float)):
        client.timeout = min(client.timeout,8)
    with newsroom.database() as c:
        parents = [r[0] for r in c.execute('SELECT p.post_id FROM publications p LEFT JOIN reply_scan_posts r ON r.post_id=p.post_id WHERE p.state="posted" AND p.attempted>? ORDER BY COALESCE(r.checked,0),p.attempted DESC LIMIT 20', (app.now()-2*86400,))]
    deadline = app.now()+45
    report = dict(checked_posts=0,replies=[],liked=0,errors=0,identity_ready=bool(own_id))
    allow_likes = bool(auto_like and own_id)
    def drain():
        nonlocal allow_likes
        if not allow_likes:
            return
        with newsroom.database() as c:
            pending = [dict(r) for r in c.execute('SELECT * FROM reply_actions WHERE state="pending" ORDER BY attempted LIMIT 100')]
        for row in pending:
            if app.now()>=deadline:
                break
            with newsroom.database() as c:
                if not c.execute('UPDATE reply_actions SET state="uncertain",attempted=? WHERE reply_id=? AND state="pending"', (app.now(),row['reply_id'])).rowcount:
                    continue
            checkpoint()
            try:
                result = client.like(row['reply_id'])
                if result.get('ok') is not True:
                    raise RuntimeError('Like not confirmed')
            except Exception:
                report['errors'] += 1
                allow_likes = False  # Back off until a later scan; never retry this ID.
                break
            with newsroom.database() as c:
                c.execute('UPDATE reply_actions SET state="liked" WHERE reply_id=?', (row['reply_id'],))
            checkpoint()
            report['liked'] += 1
    drain()
    for parent in parents:
        if app.now()>=deadline:
            break
        try:
            replies = direct_replies(client,parent)
        except Exception:
            report['errors'] += 1
            continue
        report['checked_posts'] += 1
        with newsroom.database() as c:
            c.execute('INSERT OR REPLACE INTO reply_scan_posts VALUES(?,?)', (parent,app.now()))
            for reply in replies:
                identity = str(reply.get('id',''))
                if not identity.isdigit() or str(reply.get('author_id')) == str(own_id):
                    continue
                text = reply.get('text','')
                report['replies'].append(dict(id=identity,post_id=parent,text=text[:500]))
                # Hold observed replies durably, even when time budget postpones likes.
                if not own_id or not reply.get('author_id'):
                    continue
                c.execute('INSERT OR IGNORE INTO reply_actions VALUES(?,?,?,?,?)',
                          (identity,parent,text[:500],'pending',app.now()))
    drain()
    with newsroom.database() as c:
        report['uncertain_reply_ids'] = [r[0] for r in c.execute('SELECT reply_id FROM reply_actions WHERE state="uncertain"')]
        report['pending_likes'] = c.execute('SELECT COUNT(*) FROM reply_actions WHERE state="pending"').fetchone()[0]
    newsroom.state_set('last_reply_scan',app.now())
    newsroom.state_set('reply_report',report)
    checkpoint()
    return report


def engagement_score(row):
    """Replies and reposts spread a post; weight them above likes."""
    return (row.get('likes') or 0)+3*(row.get('reposts') or 0)+4*(row.get('replies') or 0)


def track(client, limit=25, budget=40):
    """Read public metrics of our recent posts (read-only, bounded)."""
    import prepared
    with prepared.database() as c:
        posts = [r[0] for r in c.execute('''SELECT m.post_id FROM post_meta m LEFT JOIN post_metrics x ON x.post_id=m.post_id
            WHERE m.posted>? ORDER BY COALESCE(x.checked,0),m.posted DESC LIMIT ?''', (app.now()-3*86400, limit))]
    deadline = app.now()+budget
    checked = errors = 0
    for post_id in posts:
        if app.now() >= deadline:
            break
        try:
            tweet = client.get_tweet(post_id)
            if tweet.get('ok') is not True:
                raise RuntimeError('metrics unavailable')
        except Exception:
            errors += 1
            continue
        views = tweet.get('views')
        with prepared.database() as c:
            c.execute('INSERT OR REPLACE INTO post_metrics VALUES(?,?,?,?,?,?)',
                      (post_id, tweet.get('likes') or 0, tweet.get('retweets') or 0, tweet.get('replies') or 0,
                       int(views) if str(views or '').isdigit() else None, app.now()))
        checked += 1
    return dict(checked=checked, errors=errors)


def learn(min_posts=6):
    """Nudge category weights toward what our audience engages with (bounded 0.7-1.4)."""
    import prepared
    with prepared.database() as c:
        rows = [dict(r) for r in c.execute('''SELECT m.category,m.posted,x.likes,x.reposts,x.replies,p.text
            FROM post_meta m JOIN post_metrics x ON x.post_id=m.post_id
            LEFT JOIN publication_payloads p ON p.story_id=m.story_id
            WHERE m.posted BETWEEN ? AND ?''', (app.now()-14*86400, app.now()-3*3600))]
    for row in rows:
        row['score'] = engagement_score(row)
    report = dict(posts=len(rows), best=[dict(score=r['score'], text=r['text'] or '') for r in sorted(rows, key=lambda r: -r['score'])[:5]])
    if len(rows) < min_posts:
        report['learning'] = 'waiting for more measured posts'
        return report
    overall = sum(r['score'] for r in rows)/len(rows) or 1
    weights = {}
    for category in sorted({r['category'] for r in rows if r['category']}):
        scores = [r['score'] for r in rows if r['category'] == category]
        if len(scores) < 3:
            continue
        ratio = (sum(scores)/len(scores))/overall
        # Shrink toward 1.0 for small samples; never let one category disappear.
        shrink = len(scores)/(len(scores)+10)
        weights[category] = round(max(.7, min(1.4, 1+(ratio-1)*shrink)), 3)
    newsroom.state_set('learned_weights', {'updated': app.now(), 'category': weights})
    report['category_weights'] = weights
    return report
