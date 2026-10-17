from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import app
import actions_runner
import automation
from actions_runner import GitState, StateError

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db = app.DB
        self.remote = self.root / 'remote.git'
        self.git('init', '--bare', str(self.remote))
        self.repo = self.checkout('repo')
        app.DB = self.root / 'first.db'
    def tearDown(self):
        app.DB = self.old_db
        self.temp.cleanup()
    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True)
    def checkout(self, name):
        repo = self.root / name
        self.git('init', str(repo))
        self.git('-C', str(repo), 'remote', 'add', 'origin', str(self.remote))
        return repo
    def test_explicit_initialization_required(self):
        with self.assertRaises(StateError):
            GitState(self.repo)
        self.assertFalse(app.DB.exists())
    def test_full_dry_run_creates_report_and_next_runner_restores(self):
        def collect(names):
            added = app.ingest([dict(title='A candidate', url='https://example.com/candidate', source='Hacker News', published=app.now())])
            return {'fixture': {'added': added}}
        import os
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch('app.ROOT', self.repo), patch('app.collect', side_effect=collect), patch('sys.argv', ['actions_runner.py', '--initialize']), patch('automation.send') as send:
                actions_runner.main()
                send.assert_not_called()
            import json
            report = json.loads((self.root / 'posting-report.json').read_text())
            self.assertIn('dry run', report['results'][0]['publishing'])
            app.DB = self.root / 'next-run.db'
            GitState(self.checkout('fresh-run'))
            self.assertEqual(len(app.stories()), 1)
        finally:
            os.chdir(old_cwd)

    def test_live_runner_posts_only_two_hn_items_and_preserves_history(self):
        GitState(self.repo, initialize=True)
        app.DB = self.root / 'live-run.db'
        def collect(names):
            self.assertEqual(names, ['hn'])
            app.ingest([dict(title=f'HN {i}', url=f'https://example.com/hn/{i}', source='Hacker News', published=app.now()) for i in range(3)] +
                       [dict(title='Trends headline', url='https://example.com/trends', source='Google Trends discovery', published=app.now(), engagement=99999)])
            return {'hn': {'received': 3, 'added': 3}}
        import os
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch('app.ROOT', self.repo), patch('app.collect', side_effect=collect), patch('sys.argv', ['actions_runner.py', '--live']), patch('automation.authorization'), patch('automation.send', side_effect=['41', '42']) as send, patch('time.sleep'), patch('actions_runner.automation.cycle', wraps=automation.cycle) as cycle:
                # Advance the test clock between calls without making the suite wait.
                clock = app.now()
                def advancing_cycle(*args, **kwargs):
                    nonlocal clock
                    clock += 10
                    with patch('app.now', return_value=clock):
                        return original_cycle(*args, **kwargs)
                original_cycle = cycle._mock_wraps
                cycle.side_effect = advancing_cycle
                actions_runner.main()
                self.assertEqual(send.call_count, 2)
                self.assertTrue(all('HN ' in call.args[0] for call in send.call_args_list))
            app.DB = self.root / 'verify-live.db'
            GitState(self.checkout('verify-live'))
            with automation.ledger() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM publications WHERE state="posted"').fetchone()[0], 2)
        finally:
            os.chdir(old_cwd)

    def test_history_restored_in_fresh_run(self):
        state = GitState(self.repo, initialize=True)
        with automation.ledger() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)', ('a', app.now(), 'posted', '42'))
        state.checkpoint()
        app.DB = self.root / 'restored.db'
        GitState(self.checkout('next'))
        with automation.ledger() as c:
            self.assertEqual(c.execute('SELECT post_id FROM publications').fetchone()[0], '42')
    def test_stale_writer_cannot_overwrite_history(self):
        first = GitState(self.repo, initialize=True)
        app.DB = self.root / 'second.db'
        second = GitState(self.checkout('second'))
        with automation.ledger() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)', ('new', app.now(), 'posted', '99'))
        first.checkpoint()
        # A stale writer has different local state and must not remove the new post.
        with automation.ledger() as c:
            c.execute('DELETE FROM publications WHERE story_id="new"')
        with self.assertRaises(StateError):
            second.checkpoint()
    def test_missing_remote_fails_closed(self):
        self.git('-C', str(self.repo), 'remote', 'set-url', 'origin', str(self.root / 'missing.git'))
        with self.assertRaises(StateError):
            GitState(self.repo, initialize=True)
    def test_uncertain_attempt_can_be_reconciled_without_retry(self):
        state = GitState(self.repo, initialize=True)
        with automation.ledger() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,NULL)', ('a', app.now(), 'uncertain'))
        state.checkpoint()
        state.reconcile('a', '42')
        app.DB = self.root / 'resolved.db'
        GitState(self.checkout('resolved'))
        with automation.ledger() as c:
            row = c.execute('SELECT state,post_id FROM publications').fetchone()
            self.assertEqual(tuple(row), ('posted', '42'))

class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = app.DB
        app.DB = Path(self.temp.name) / 'stories.db'
        app.ingest([dict(title='Fresh story', url='https://example.com/a', source='Test', published=app.now())])
    def tearDown(self):
        app.DB = self.old_db
        self.temp.cleanup()
    def test_failed_checkpoint_prevents_public_write(self):
        with patch('automation.authorization'), patch('automation.send') as send:
            with self.assertRaises(StateError):
                automation.cycle(True, checkpoint=lambda: (_ for _ in ()).throw(StateError()), collection={})
            send.assert_not_called()
    def test_reservation_checkpoint_precedes_send(self):
        events = []
        def checkpoint():
            with automation.ledger() as c:
                events.append(c.execute('SELECT state FROM publications').fetchone()[0])
        def send(text):
            events.append('send')
            return '42'
        with patch('automation.authorization'), patch('automation.send', side_effect=send):
            automation.cycle(True, checkpoint=checkpoint, collection={})
        self.assertEqual(events, ['uncertain', 'send', 'posted'])
    def test_source_filter_excludes_existing_trends_stories(self):
        app.ingest([dict(title='Google candidate', url='https://example.com/google', source='Google Trends discovery', published=app.now(), engagement=99999),
                    dict(title='HN candidate', url='https://example.com/hn', source='Hacker News', published=app.now())])
        with patch('automation.authorization'), patch('automation.send', return_value='42') as send:
            automation.cycle(True, collection={}, allowed_sources=['Hacker News'])
            self.assertIn('HN candidate', send.call_args.args[0])
            self.assertNotIn('Google candidate', send.call_args.args[0])

    def test_daily_cap_honored(self):
        with automation.ledger() as c:
            c.executemany('INSERT INTO publications VALUES(?,?,?,NULL)', [(str(i),app.now()-60,'posted') for i in range(400)])
        with patch('automation.authorization'), patch('automation.send') as send:
            result = automation.cycle(True, collection={})
            self.assertIn('waiting', result['publishing'])
            send.assert_not_called()
    def test_ambiguous_send_pauses_future_new_stories(self):
        with patch('automation.authorization'), patch('automation.send', side_effect=TimeoutError):
            automation.cycle(True, collection={})
        app.ingest([dict(title='Another', url='https://example.com/b', source='Test', published=app.now())])
        with patch('automation.authorization'), patch('automation.send') as send, patch('app.now', return_value=app.now()+300):
            self.assertIn('paused', automation.cycle(True, collection={})['publishing'])
            send.assert_not_called()

if __name__ == '__main__':
    unittest.main()
