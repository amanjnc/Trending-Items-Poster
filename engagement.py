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
