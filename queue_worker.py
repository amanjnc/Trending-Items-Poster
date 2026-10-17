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


def run(state,mode='collect',live=False,auto_like=False):
    start=time.monotonic()
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
        client=None
        if os.environ.get('TWEETKIT_COOKIE'):
            try:
                client=automation.authorization()
            except ValueError:
                pass  # Names remain unchanged if lookup is unavailable.
        preparation=postcraft.prepare(client=client)
        state.checkpoint()
        report={'sources':source_report,'preparation':preparation}
        if source_report and all('error' in r for r in source_report.values()) and not preparation['prepared']:
            report['ok']=False
        if live:
            report['publication']=prepared.publish_one(state.checkpoint,live=True)
    report['task_seconds']=round(time.monotonic()-start,3)
    report.setdefault('ok',not str(report.get('publishing','')).startswith('paused'))
    if str(report.get('publication',{}).get('publishing','')).startswith('paused'):
        report['ok']=False
    return report


def main():
    started=time.monotonic()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['collect','publish','replies'],default='collect')
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
