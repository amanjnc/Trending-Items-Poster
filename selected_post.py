"""Explicitly refresh and optionally send one configured Telegram source item."""
import argparse
import json
from pathlib import Path
import re

import app
import automation
from actions_runner import GitState, StateError
import newsroom
import postcraft
import prepared


def run(state,source_url,live=False):
    match=re.fullmatch(r'https://t\.me/([A-Za-z0-9_]+)/(\d+)',source_url)
    sources=newsroom.config()['telegram']
    source=next((s for s in sources if match and s['channel'].casefold()==match[1].casefold()),None)
    if source is None:
        raise ValueError('Select a message URL from a configured Telegram source.')
    source_url='https://t.me/'+source['channel']+'/'+match[2]
    with newsroom.database() as c:
        prior=c.execute('SELECT p.state,p.post_id FROM publications p JOIN observations o ON o.story_id=p.story_id WHERE o.url=?',(source_url,)).fetchone()
    if prior:
        return {'ok':False,'publishing':'selected item already reserved or posted; no repeat sent','source_url':source_url}
    # Inspect the actual message HTML, not its cached thumbnail URL.
    rows=newsroom.telegram(source['channel'],pages=1,message_id=match[2])
    item=next((r for r in rows if r['url']==source_url),None)
    if item is None:
        raise ValueError('Selected message is unavailable in the public Telegram preview.')
    newsroom.enqueue([item],source['priority'],[s['channel'] for s in sources])
    client=automation.authorization()
    preparation=postcraft.prepare(client,selected_source=source_url)
    report={'source_url':source_url,'attachment_kind':item['attachment_kind'],
            'attachment_download_available':bool(item['attachment_url']),
            'media_audit':[{'source_url':r['url'],'kind':r['attachment_kind'],'download_available':bool(r['attachment_url'])}
                          for r in rows if r['url'] in (source_url,'https://t.me/ClashReport/101475')],
            'preparation':preparation}
    if not preparation['prepared']:
        return report|{'ok':False,'publishing':'selected item rejected, expired, or already posted'}
    if any(status!='verified' for status in preparation['identity_resolution'].values()):
        return report|{'ok':False,'publishing':'identity verification incomplete; no post sent'}
    with prepared.database() as c:
        identity=c.execute('SELECT story_id FROM prepared_posts WHERE source_url=?',(source_url,)).fetchone()[0]
    if live:
        state.checkpoint()
    publication=prepared.publish_one(state.checkpoint,live=live,story_id=identity,
                                     require_media=item['attachment_kind'] in ('image','video'))
    return report|{'publication':publication,'ok':publication.get('ok',not str(publication.get('publishing','')).startswith('paused'))}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-url',required=True)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    report=run(GitState(app.ROOT),args.source_url,args.live)
    Path('posting-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2),flush=True)
    if not report['ok']:
        raise SystemExit(1)


if __name__=='__main__':
    try:
        main()
    except (ValueError,StateError):
        print('Selected post stopped; inspect source availability, identity verification and posting history. No automatic retry.',flush=True)
        raise SystemExit(1)
    except Exception:
        print('Selected post stopped; source or account service unavailable. Inspect the report before retrying.',flush=True)
        raise SystemExit(1)
