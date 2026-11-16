"""Finite tasks: collect/prepare, publish one, or check replies; never sleep."""
import argparse
import json
import os
import sqlite3
from pathlib import Path
import time

import app
import automation
from actions_runner import GitState, StateError
import prepared

PREPARATION_VERSION=5
# Each scheduler tick (~10 minutes) collects fresh news; duplicate or overlapping
# triggers inside this window only try to publish from the saved queue.
COLLECT_EVERY=int(os.environ.get('COLLECT_EVERY_SECONDS','480'))

def automatic_mode():
    """A small persisted-state read; publishing ticks never import collectors."""
    bucket=int(app.now()//COLLECT_EVERY)
    with prepared.database() as c:
        exists=c.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="worker_state"').fetchone()
        row=c.execute('SELECT value FROM worker_state WHERE key="automatic_collection"').fetchone() if exists else None
    previous=json.loads(row[0]) if row else {}
    if previous.get('preparation_version')==PREPARATION_VERSION and app.now()-previous.get('attempted',0)<COLLECT_EVERY:
        return 'publish',bucket
    return 'collect',bucket


def run(state,mode='collect',live=False,auto_like=False):
    start=time.monotonic()
    automatic=mode=='auto'
    mode,bucket=automatic_mode() if automatic else (mode,int(app.now()//COLLECT_EVERY))
    if mode=='publish':
        # No collector/editorial imports, entity resolution, tests, or ranking here.
        report=prepared.publish_one(state.checkpoint,live=live)
    elif mode=='replies':
        if not live:
            report={'mode':'preview; no reply requests or likes'}
        else:
            import engagement
            report=engagement.scan(state.checkpoint,auto_like)
            try:
                report['metrics']=engagement.track(automation.authorization())
                report['performance']=engagement.learn()
            except Exception as error:
                report['performance']={'error':type(error).__name__}
            state.checkpoint()
    else:
        import newsroom
        import postcraft
        component_errors=[]
        try:
            source_report=newsroom.collect()
        except sqlite3.DatabaseError:
            raise
        except Exception as error:
            source_report={}
            component_errors.append({'phase':'collection','error_type':type(error).__name__})
        prepared.cleanup_posted()
        client=None
        if os.environ.get('TWEETKIT_COOKIE'):
            try:
                client=automation.authorization()
            except ValueError:
                pass  # Names remain unchanged if lookup is unavailable.
        try:
            preparation=postcraft.prepare(client=client)
        except sqlite3.DatabaseError:
            raise
        except Exception as error:
            # Preparation commits its rebuilt queue atomically; an editorial
            # failure leaves the previous saved queue available for publication.
            preparation={'reused_saved_queue':True}
            component_errors.append({'phase':'preparation','error_type':type(error).__name__})
        collection_ok=bool(source_report) and any('error' not in r for r in source_report.values())
        preparation_ok=not preparation.get('reused_saved_queue',False)
        newsroom.state_set('automatic_collection',{'bucket':bucket,'attempted':app.now(),'ok':collection_ok and preparation_ok,'preparation_version':PREPARATION_VERSION})
        state.checkpoint()
        report={'sources':source_report,'preparation':preparation,'component_errors':component_errors}
        # Preview runs show exactly what would be published next.
        report['publication']=prepared.publish_one(state.checkpoint,live=live)
        if (not collection_ok or not preparation_ok) and not prepared.next_post() and report.get('publication',{}).get('publishing')!='posted':
            report['ok']=False
    if automatic:
        report['automatic_mode']=mode
        report['tick_utc']=app.dt.datetime.fromtimestamp(app.now(),app.UTC).isoformat()
    report['task_seconds']=round(time.monotonic()-start,3)
    report['health']=prepared.health()
    report.setdefault('ok',not str(report.get('publishing','')).startswith('paused'))
    if str(report.get('publication',{}).get('publishing','')).startswith('paused'):
        report['ok']=False
    return report


def write_editorial_summary(output,report):
    """Human-readable view of what the editor picked and why."""
    preparation=report.get('preparation',{})
    if preparation.get('top5'):
        output.write('\n### Top of the queue\n\n| rank | category | outlets | AI news/talk | draft |\n|---|---|---|---|---|\n')
        for row in preparation['top5']:
            r=row.get('reasons',{})
            ai=r.get('ai') or {}
            text=row['text'].replace('\n',' ').replace('|','/')[:160]
            output.write('| %s | %s | %s | %s | %s |\n'%(row['rank'],r.get('category',''),r.get('outlets',''),
                         ('%s/%s'%(ai.get('newsworthiness'),ai.get('talkability'))) if ai else 'rules',text))
    ai=preparation.get('ai')
    if ai:
        output.write('\nAI editor: enabled=%s, new calls=%s, cached=%s, declined=%s, errors=%s\n'%(
            ai.get('enabled'),ai.get('calls'),ai.get('cached'),len(ai.get('declined',[])),len(ai.get('errors',[]))))
    performance=report.get('performance')
    if performance and performance.get('best'):
        output.write('\n### Best recent posts\n\n')
        for row in performance['best'][:3]:
            output.write('- %s engagement: %s\n'%(row['score'],row['text'][:140].replace('\n',' ')))


def main():
    started=time.monotonic()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['auto','collect','publish','replies','check-ai'],default='collect')
    p.add_argument('--live',action='store_true')
    p.add_argument('--auto-like',action='store_true')
    args=p.parse_args()
    if args.mode=='check-ai':
        import editor
        report={'ai_check':editor.check()}
        print(json.dumps(report,indent=2),flush=True)
        if not any(isinstance(v,dict) and v.get('ok') for v in report['ai_check'].values()):
            raise SystemExit(1)
        return
    state=GitState(app.ROOT)
    report=run(state,args.mode,args.live,args.auto_like)
    report['state_and_task_seconds']=round(time.monotonic()-started,3)
    Path('posting-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        publication=report.get('publication',report)
        with open(summary,'a') as output:
            output.write('## News publisher\n\nOutcome: '+publication.get('publishing','collection completed')+'\n\n')
            output.write('Last 24 hours: `'+json.dumps(report['health']['last24h'])+'`\n\n')
            output.write('Active cooldowns: `'+json.dumps(report['health']['active_cooldowns'])+'`\n')
            output.write('\nSkipped invalid stories: '+str(len(report.get('preparation',{}).get('skipped_invalid_items',[])))+'\n')
            write_editorial_summary(output,report)
    if report['health']['active_cooldowns']:
        print('::warning::Publisher cooldown is active. See the job summary; no rapid retries are performed.',flush=True)
    if report.get('failure_category') or report.get('publication',{}).get('failure_category'):
        print('::warning::A posting failure was recorded. The item remains reserved and will not be repeated automatically.',flush=True)
    if report.get('component_errors') or report.get('preparation',{}).get('skipped_invalid_items') or any('error' in source for source in report.get('sources',{}).values()):
        print('::warning::A collection or preparation component failed. Other sources and any usable saved queue were allowed to continue.',flush=True)
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
