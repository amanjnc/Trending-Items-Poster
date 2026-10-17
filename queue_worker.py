"""Finite tasks: collect/prepare, publish one, or check replies; never sleep."""
import argparse
import json
import os
from pathlib import Path
import time

import app
import automation
from actions_runner import GitState, StateError
import prepared

PREPARATION_VERSION=3

def automatic_mode():
    """A small persisted-state read; publishing ticks never import collectors."""
    bucket=int(app.now()//7200)
    with prepared.database() as c:
        exists=c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="worker_state"').fetchone()
        row=c.execute('SELECT value FROM worker_state WHERE key="automatic_collection"').fetchone() if exists else None
    previous=json.loads(row[0]) if row else {}
    if previous.get('bucket')==bucket and previous.get('preparation_version')==PREPARATION_VERSION:
        # A source outage retries at most twice an hour instead of on every tick.
        if previous.get('ok') or app.now()-previous.get('attempted',0)<1800:
            return 'publish',bucket
    return 'collect',bucket


def run(state,mode='collect',live=False,auto_like=False):
    start=time.monotonic()
    automatic=mode=='auto'
    mode,bucket=automatic_mode() if automatic else (mode,int(app.now()//7200))
    if mode=='publish':
        # No collector/editorial imports, entity resolution, tests, or ranking here.
        report=prepared.publish_one(state.checkpoint,live=live)
    elif mode=='replies':
        if not live:
            report={'mode':'preview; no reply requests or likes'}
        else:
            import engagement
            report=engagement.scan(state.checkpoint,auto_like)
    else:
        import newsroom
        import postcraft
        source_report=newsroom.collect()
        prepared.cleanup_posted()
        client=None
        if os.environ.get('TWEETKIT_COOKIE'):
            try:
                client=automation.authorization()
            except ValueError:
                pass  # Names remain unchanged if lookup is unavailable.
        preparation=postcraft.prepare(client=client)
        collection_ok=bool(source_report) and any('error' not in r for r in source_report.values())
        newsroom.state_set('automatic_collection',{'bucket':bucket,'attempted':app.now(),'ok':collection_ok,'preparation_version':PREPARATION_VERSION})
        state.checkpoint()
        report={'sources':source_report,'preparation':preparation}
        if source_report and all('error' in r for r in source_report.values()) and not preparation['prepared']:
            report['ok']=False
        if live:
            report['publication']=prepared.publish_one(state.checkpoint,live=True)
        if automatic and not collection_ok:
            report['ok']=False
    if automatic:
        report['automatic_mode']=mode
        report['collection_period_start_utc']=app.dt.datetime.fromtimestamp(bucket*7200,app.UTC).isoformat()
    report['task_seconds']=round(time.monotonic()-start,3)
    report.setdefault('ok',not str(report.get('publishing','')).startswith('paused'))
    if str(report.get('publication',{}).get('publishing','')).startswith('paused'):
        report['ok']=False
    return report


def main():
    started=time.monotonic()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['auto','collect','publish','replies'],default='collect')
    p.add_argument('--live',action='store_true')
    p.add_argument('--auto-like',action='store_true')
    args=p.parse_args()
    state=GitState(app.ROOT)
    report=run(state,args.mode,args.live,args.auto_like)
    report['state_and_task_seconds']=round(time.monotonic()-started,3)
    Path('posting-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    if not report['ok']:
        raise SystemExit(1)

if __name__=='__main__':
    try:
        main()
    except StateError as error:
        print(str(error),flush=True)
        raise SystemExit(1)
    except Exception:
        print('Job stopped; inspect state, dependencies, and account connection. No automatic retry.',flush=True)
        raise SystemExit(1)
