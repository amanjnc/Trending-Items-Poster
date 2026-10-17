"""Two-hour collection windows with durable one-at-a-time adaptive publishing."""
import argparse
import json
from pathlib import Path
import time

import app
import automation
from actions_runner import GitState, StateError
import newsroom


def run(state, live=False, minutes=115, max_posts=None, check_replies=False, auto_like=False):
    cfg = newsroom.config()
    deadline = app.now()+minutes*60
    outputs, collections, engagement_report = [], [], None
    if live:
        automation.authorization()
    while app.now()<deadline:
        if app.now()>=newsroom.state_get('last_collection',0)+cfg['collection_seconds']:
            result = newsroom.collect(cfg)
            collections.append(result)
            state.checkpoint()
            print(json.dumps({'collection':result}), flush=True)
            if result and all('error' in r for r in result.values()) and not newsroom.queue():
                return dict(ok=False,reason='All sources unavailable; no eligible queue',collections=collections,posts=outputs)
        rows = newsroom.queue()
        source_report = newsroom.state_get('collection_report',{})
        if not rows and source_report and all('error' in r for r in source_report.values()):
            return dict(ok=False,reason='Last collection failed; next collection is not due yet',source_report=source_report,collections=collections,posts=outputs)
        if not live:
            return dict(ok=True,mode='preview; no X writes',collections=collections,source_report=source_report,queue_size=len(rows),
                        drafts=[dict(source=r['source'],rank=r['rank'],text=newsroom.render(r)) for r in rows[:5]])
        if check_replies and app.now()>=newsroom.state_get('last_reply_scan',0)+1800:
            import engagement
            engagement_report = engagement.scan(state.checkpoint, auto_like)
        if not rows:
            break  # Do not spend runner minutes waiting on an empty queue.
        next_collection = newsroom.state_get('last_collection',app.now())+cfg['collection_seconds']
        delay = newsroom.adaptive_interval(len(rows), next_collection-app.now())
        due = newsroom.state_get('next_post_at',0)
        with newsroom.database() as c:
            last = c.execute('SELECT MAX(attempted) FROM publications').fetchone()[0]
        due = max(due, (last+120) if last is not None else 0)
        if app.now()<due:
            wait = min(due-app.now(),deadline-app.now(),max(1,next_collection-app.now()))
            if wait>0:
                time.sleep(wait)
            continue
        result = newsroom.post_one(state.checkpoint,cfg['daily_limit'],live=True,next_interval=delay)
        result['next_interval_seconds'] = delay
        outputs.append(result)
        print(json.dumps(result), flush=True)
        if result['publishing'] != 'posted':
            break
        if max_posts is not None and sum(r['publishing']=='posted' for r in outputs)>=max_posts:
            break
    return dict(ok=not any(r['publishing'].startswith('paused') for r in outputs),
                collections=collections,posts=outputs,remaining=len(newsroom.queue()),engagement=engagement_report)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live',action='store_true')
    p.add_argument('--initialize',action='store_true')
    p.add_argument('--minutes',type=int,default=115)
    p.add_argument('--max-posts',type=int)
    p.add_argument('--check-replies',action='store_true')
    p.add_argument('--auto-like',action='store_true')
    args = p.parse_args()
    if not 1<=args.minutes<=115 or (args.max_posts is not None and not 1<=args.max_posts<=400):
        p.error('Minutes must be 1–115; optional max-posts must be 1–400')
    if args.initialize and args.live:
        p.error('Initialize with a preview first')
    state = GitState(app.ROOT,initialize=args.initialize)
    newsroom.database().close()
    report = run(state,args.live,args.minutes,args.max_posts,args.check_replies,args.auto_like)
    Path('posting-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    if not report['ok']:
        raise SystemExit(1)

if __name__=='__main__':
    try:
        main()
    except StateError as e:
        print(str(e),flush=True)
        raise SystemExit(1)
    except Exception:
        # Never log third-party HTTP exceptions or credentials from public runner logs.
        print('Queue worker stopped. Inspect source reports, session secret, and posting history.',flush=True)
        raise SystemExit(1)
