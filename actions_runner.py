"""Finite GitHub Actions batches; persist a reservation before every X post."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

import app
import automation

REF = 'refs/heads/autopost-state'

class StateError(RuntimeError):
    pass

class GitState:
    """A separate state branch, without changing source checkout or its index."""
    def __init__(self, repo, initialize=False):
        self.repo = str(repo)
        self.parent = None
        result = self.git('ls-remote', '--exit-code', 'origin', REF, check=False)
        if result.returncode == 2:
            if not initialize:
                raise StateError('No posting history branch. Run a manual dry run with Initialize history checked first.')
            # First initialization must be explicit; a missing branch is not an empty history.
            app.DB.parent.mkdir(parents=True, exist_ok=True)
            if app.DB.exists():
                raise StateError('Refusing to initialize over an existing local database.')
            automation.ledger().close()
            self.checkpoint()
        elif result.returncode == 0:
            self.git('fetch', '--depth=1', '--no-tags', 'origin', REF)
            self.parent = self.git('rev-parse', 'FETCH_HEAD').stdout.decode().strip()
            content = self.git('show', f'{self.parent}:state.db').stdout
            app.DB.parent.mkdir(parents=True, exist_ok=True)
            if app.DB.exists():
                raise StateError('Refusing to overwrite a local database. Use a fresh Actions database path.')
            app.DB.write_bytes(content)
            with sqlite3.connect(app.DB) as c:
                if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise StateError('Posting history database failed its integrity check.')
                # Missing tables indicate incompatible/corrupt history, not a new database.
                c.execute('SELECT story_id, attempted, state, post_id FROM publications LIMIT 1')
                c.execute('SELECT id FROM stories LIMIT 1')
        else:
            raise StateError('Cannot read remote posting history; refusing to post.')

    def git(self, *args, data=None, check=True):
        env = dict(os.environ, GIT_AUTHOR_NAME='Trending Items Poster',
                   GIT_AUTHOR_EMAIL='actions@users.noreply.github.com',
                   GIT_COMMITTER_NAME='Trending Items Poster',
                   GIT_COMMITTER_EMAIL='actions@users.noreply.github.com')
        r = subprocess.run(['git', '-C', self.repo, *args], input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=90)
        if check and r.returncode:
            # Do not expose Git stderr: authenticated remote URLs/headers may be present.
            raise StateError('Posting history could not be saved. Check Actions write permissions and branch rules.')
        return r

    def checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / 'state.db'
            with sqlite3.connect(app.DB) as src, sqlite3.connect(dest) as backup:
                src.backup(backup)
            blob = self.git('hash-object', '-w', '--stdin', data=dest.read_bytes()).stdout.decode().strip()
        tree = self.git('mktree', data=f'100644 blob {blob}\tstate.db\n'.encode()).stdout.decode().strip()
        args = ['commit-tree', tree, '-m', 'Save posting history']
        if self.parent:
            args.extend(['-p', self.parent])
        commit = self.git(*args).stdout.decode().strip()
        # A stale/concurrent run must fail before sending, never overwrite newer history.
        self.git('push', f'--force-with-lease={REF}:{self.parent or ""}', 'origin', f'{commit}:{REF}')
        self.parent = commit

    def reconcile(self, identity, post_id, not_posted=False):
        if bool(post_id) == bool(not_posted):
            raise StateError('Choose either the confirmed X post ID or Confirm not posted.')
        if post_id and not post_id.isdigit():
            raise StateError('X post ID must contain digits only.')
        with automation.ledger() as c:
            changed = c.execute('UPDATE publications SET state=?,post_id=? WHERE story_id=? AND state="uncertain"',
                                ('not_posted' if not_posted else 'posted', post_id or None, identity)).rowcount
            if changed != 1:
                raise StateError('No uncertain attempt found for that story ID.')
        # Even confirmed not-posted stories stay reserved; no automatic retry.
        self.checkpoint()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live', action='store_true')
    p.add_argument('--initialize', action='store_true')
    p.add_argument('--batch', type=int, default=2)
    p.add_argument('--sources', nargs='+', choices=['hn', 'trends', 'techcrunch'], default=['techcrunch'])
    p.add_argument('--daily-limit', type=int, default=400)
    p.add_argument('--interval', type=int, default=5)
    p.add_argument('--reconcile-story', default='')
    p.add_argument('--confirmed-post-id', default='')
    p.add_argument('--confirm-not-posted', action='store_true')
    args = p.parse_args()
    if not 1 <= args.batch <= 20 or not 1 <= args.daily_limit <= 1000 or not 1 <= args.interval <= 60:
        p.error('Batch must be 1–20, daily limit 1–1000, interval 1–60 seconds.')
    state = GitState(app.ROOT, initialize=args.initialize)
    if args.reconcile_story:
        if args.live:
            raise StateError('Reconciliation must be a dry run.')
        state.reconcile(args.reconcile_story, args.confirmed_post_id, args.confirm_not_posted)
        print('History reconciled; no post was sent.')
        return
    if args.live:
        automation.authorization()
    collection = app.collect(args.sources)
    source_labels = {'hn': 'Hacker News', 'trends': 'Google Trends discovery', 'techcrunch': 'TechCrunch'}
    allowed_sources = [source_labels[name] for name in args.sources]
    # Exercise remote persistence even on dry runs; verify access before public writes.
    state.checkpoint()
    outputs = []
    for _ in range(args.batch if args.live else 1):
        result = automation.cycle(args.live, args.daily_limit, args.interval,
                                  checkpoint=state.checkpoint, collection=collection, allowed_sources=allowed_sources)
        outputs.append(result)
        if result['publishing'] != 'posted':
            break
        if len(outputs) < args.batch:
            time.sleep(args.interval)
    with automation.ledger() as c:
        uncertain = [r[0] for r in c.execute('SELECT story_id FROM publications WHERE state="uncertain"')]
    report = {'results': outputs, 'uncertain_story_ids': uncertain}
    print(json.dumps(report, indent=2), flush=True)
    Path('posting-report.json').write_text(json.dumps(report, indent=2))
    if uncertain or (collection and all('error' in r for r in collection.values())):
        raise SystemExit(1)

if __name__ == '__main__':
    try:
        main()
    except StateError as error:
        print(str(error), flush=True)
        raise SystemExit(1)
    except (ValueError, sqlite3.DatabaseError):
        # Suppress third-party/Git exceptions and values in public Actions logs.
        print('Automation stopped. Check history initialization, write permissions, session secret, and database integrity.', flush=True)
        raise SystemExit(1)
