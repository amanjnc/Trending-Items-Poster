import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
import automation
import editor
import engagement
import events
import newsroom
import postcraft
import prepared
import scoring


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = app.DB
        app.DB = Path(self.tmp.name)/'test.db'
        newsroom.database().close()
        prepared.database().close()
        for key, value in dict(QUIET_HOURS_UTC='', SOURCE_REPLY=False, MIN_SCORE=60.0, MIN_GAP=2400,
                               BREAKING_GAP=480, DAILY_LIMIT=20).items():
            p = patch.object(prepared, key, value)
            p.start()
            self.addCleanup(p.stop)
        for key in ('GEMINI_API_KEY', 'GROQ_API_KEY'):
            p = patch.dict('os.environ', {key: ''})
            p.start()
            self.addCleanup(p.stop)
        self.settings = patch('newsroom.source_settings', return_value={
            'Telegram @ClashReport': {'priority': 1.4, 'category': 'geopolitics'},
            'Telegram @rtnews': {'priority': 1.0, 'category': 'geopolitics', 'state_media': True},
            'TechCrunch': {'priority': .9, 'category': 'tech'},
            'Google News Top': {'priority': 1.2, 'category': 'politics', 'aggregator': True}})
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def tearDown(self):
        app.DB = self.old
        self.tmp.cleanup()

    def add(self, text, number, source, age=0, views=0, media=None):
        url = 'https://example.com/%s' % number
        with newsroom.database() as c:
            c.execute('INSERT INTO stories(id,title,url,source,published,collected,score) VALUES(?,?,?,?,?,?,?)',
                      (str(number), text, url, source, app.now()-age, app.now(), 0))
            c.execute('INSERT INTO news_items VALUES(?,?,?,?,?)', (str(number), text, str(number), 1.0, views))
            c.execute('INSERT INTO observations VALUES(?,?,?)', (url, str(number), source))
            if media:
                c.execute('INSERT INTO story_media VALUES(?,?)', (str(number), media))
        return str(number)

    def queue(self):
        with prepared.database() as c:
            return [dict(r) for r in c.execute('SELECT * FROM prepared_posts ORDER BY position')]


HUANG = ['Nvidia CEO Jensen Huang says US chip export controls on China have failed',
         'Jensen Huang: China export curbs backfired, Nvidia boss says',
         'Nvidia chief Jensen Huang says export controls failed and handed China market to Huawei',
         'Jensen Huang warns US export controls on Nvidia chips to China have failed']


class ClusteringTests(unittest.TestCase):
    def test_same_event_from_several_outlets_is_one_event(self):
        stories = [dict(id=str(i), body=t, source=s, published=app.now()-i*600)
                   for i, (t, s) in enumerate(zip(HUANG, ['Google News Top / Reuters', 'Google News Tech / CNBC',
                                                         'Telegram @hitech', 'The Verge']))]
        clusters = events.cluster(stories)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(events.metrics(clusters[0], app.now())['outlets'], 4)
        self.assertIn('huang', clusters[0]['top_names'])

    def test_different_events_about_same_people_stay_separate(self):
        stories = [dict(id='1', body='Trump says he will meet Putin in Budapest next month', source='A', published=app.now()),
                   dict(id='2', body='Trump threatens Putin with new sanctions over Ukraine', source='B', published=app.now())]
        self.assertEqual(len(events.cluster(stories)), 2)

    def test_aggregator_outlets_counted_by_publisher(self):
        self.assertEqual(events.outlet('Google News Top / Reuters'), 'reuters')
        self.assertEqual(events.outlet('Google News World / Reuters'), events.outlet('Google News Top / Reuters'))


class ScoringTests(unittest.TestCase):
    def metrics(self, outlets=1, recent=1, age=.2, views=0):
        return dict(outlets=outlets, outlets_last_hour=recent, age_hours=age, views=views, related=0)

    def test_spreading_political_story_beats_stale_single_source_tech_image(self):
        hot, category, _ = scoring.score('Senate votes to end government shutdown as Trump signs bill', self.metrics(5, 4, .3))
        stale, _, _ = scoring.score('A new research guide explains battery chemistry in phones', self.metrics(1, 0, 9), media=True)
        self.assertEqual(category, 'politics')
        self.assertGreater(hot, stale+50)

    def test_crossover_tech_needs_heat_or_political_angle(self):
        quiet, category, _ = scoring.score('Startup launches a new note taking app for teams', self.metrics(1, 1, .2))
        political, _, _ = scoring.score(HUANG[0], self.metrics(1, 1, .2))
        viral, _, _ = scoring.score(HUANG[0], self.metrics(4, 3, .3))
        self.assertLess(quiet, 40)
        self.assertGreater(political, quiet+20)
        self.assertGreater(viral, political+30)

    def test_filler_and_fatigue_penalised(self):
        base, _, _ = scoring.score('Trump fires FBI director in shock move', self.metrics())
        filler, _, _ = scoring.score('Opinion: Trump fires FBI director in shock move, a podcast', self.metrics())
        tired, _, parts = scoring.score('Trump fires FBI director in shock move', self.metrics(),
                                        names=['trump', 'fbi'], recent_posts=[({'trump', 'fbi'}, .5)])
        self.assertLess(filler, base-20)
        self.assertLess(tired, base)
        self.assertLess(parts['fatigue'], 0)

    def test_trend_match_boosts_and_breaking_lane(self):
        m = self.metrics(6, 4, .4)
        plain, cat, _ = scoring.score('Israel and Hamas agree ceasefire deal in Gaza, hostages to be released', m)
        trending, _, _ = scoring.score('Israel and Hamas agree ceasefire deal in Gaza, hostages to be released', m,
                                       trend_terms={'gaza ceasefire': 500000})
        self.assertGreater(trending, plain)
        self.assertTrue(scoring.breaking(trending, m, cat))
        self.assertFalse(scoring.breaking(trending, self.metrics(1, 1, .4), cat))


class EditorValidationTests(unittest.TestCase):
    SOURCES = ['Nvidia CEO Jensen Huang said US export controls "have failed" and cost Nvidia $5 billion in China sales.']

    def check(self, text):
        return editor.validate(dict(post=True, text=text), self.SOURCES)

    def test_grounded_post_accepted(self):
        self.assertEqual(self.check('Jensen Huang says US chip export controls "have failed."\n\nNvidia lost $5 billion in China sales.'), (True, 'ok'))

    def test_invented_number_quote_link_and_length_rejected(self):
        self.assertEqual(self.check('Nvidia lost $7 billion in China.')[1], 'number not in sources')
        self.assertEqual(self.check('Huang: "this was the biggest mistake ever made"')[1], 'quote not in sources')
        self.assertEqual(self.check('Read more at https://example.com')[1], 'contains link')
        self.assertEqual(self.check('x'*300)[1], 'too long')
        self.assertEqual(self.check('@nvidia lost sales')[1], 'contains handle')

    def test_edit_retries_once_then_falls_back_to_groq(self):
        bad = json.dumps(dict(post=True, newsworthiness=8, talkability=9, text='Nvidia lost $9 billion.'))
        good = json.dumps(dict(post=True, newsworthiness=8, talkability=9, text='Huang: export controls "have failed."', reply=''))
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'k', 'GROQ_API_KEY': 'k'}), \
                patch('editor.gemini', side_effect=[bad, bad]) as gemini, patch('editor.groq', return_value=good) as groq:
            result = editor.edit([('reuters', self.SOURCES[0])])
        self.assertEqual(gemini.call_count, 2)
        self.assertIn('REJECTED', gemini.call_args_list[1].args[0])
        groq.assert_called_once()
        self.assertEqual(result['provider'], 'groq')
        self.assertEqual(editor.adjustment(result), 3*3+4*4)

    def test_no_key_means_unavailable(self):
        with patch.dict('os.environ', {'GEMINI_API_KEY': '', 'GROQ_API_KEY': ''}):
            with self.assertRaises(editor.EditorUnavailable):
                editor.edit([('a', 'b')])


class PreparationTests(Case):
    def test_spreading_story_tops_queue_over_many_single_source_items(self):
        for i in range(8):
            self.add('Local official opens a new community library branch in district number %d this week' % i, 100+i, 'Telegram @ClashReport', age=1800)
        for i, (text, source) in enumerate(zip(HUANG, ['Google News Top / Reuters', 'Google News Top / CNBC', 'TechCrunch', 'Google News Top / Bloomberg'])):
            self.add(text, i+1, source, age=900-i*200)
        report = postcraft.prepare()
        top = self.queue()[0]
        self.assertIn('Huang', top['text'])
        self.assertEqual(json.loads(top['reasons'])['outlets'], 4)
        self.assertLessEqual(report['prepared'], postcraft.TOP_EVENTS)

    def test_ai_text_used_without_links_and_decline_drops_event(self):
        a = self.add('Russia launches massive drone attack on Kyiv, at least 3 killed, officials say', 1, 'Telegram @ClashReport', media='https://cdn4.telesco.pe/a.jpg')
        self.add('Local bakery wins regional prize for its sourdough bread recipe this weekend', 2, 'Google News Top / Daily')
        def fake(reports, already=(), update_of=None):
            if 'Kyiv' in reports[0][1]:
                return dict(post=True, newsworthiness=9, talkability=8, text='🚨 Russia hits Kyiv with a massive drone attack.\n\nAt least 3 killed, officials say.',
                            reply='Officials reported the toll.', reason='major', provider='gemini')
            return dict(post=False, newsworthiness=2, talkability=2, text='', reply='', reason='minor', provider='gemini')
        with patch('editor.available', return_value=True), patch('editor.edit', side_effect=fake):
            report = postcraft.prepare()
        rows = self.queue()
        self.assertEqual([r['story_id'] for r in rows], [a])
        self.assertTrue(rows[0]['text'].startswith('🚨 Russia hits Kyiv'))
        self.assertEqual(rows[0]['format'], 'text_image')
        self.assertEqual(json.loads(rows[0]['reasons'])['reply'], 'Officials reported the toll.')
        self.assertEqual(len(report['ai']['declined']), 1)
        # Second tick reuses the cached verdicts instead of spending AI calls.
        with patch('editor.available', return_value=True), patch('editor.edit') as edit:
            report = postcraft.prepare()
        edit.assert_not_called()
        self.assertEqual(report['ai']['cached'], 2)

    def test_posted_event_is_not_reposted_from_another_outlet(self):
        first = self.add(HUANG[0], 1, 'Google News Top / Reuters', age=3600)
        with prepared.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)', (first, app.now()-1800, 'posted', '555'))
        self.add(HUANG[1], 2, 'Google News Top / CNBC', age=600)
        postcraft.prepare()
        self.assertEqual(self.queue(), [])

    def test_material_update_becomes_quote_of_original_post(self):
        first = self.add('Israel strikes Beirut suburbs after rocket fire from Lebanon', 1, 'Google News Top / Reuters', age=7200)
        with prepared.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)', (first, app.now()-5400, 'posted', '777'))
            c.execute('INSERT INTO publication_payloads VALUES(?,?,?,?)', (first, 'Israel strikes Beirut suburbs.', 'text', app.now()-5400))
        self.add("Israeli airstrikes on Beirut's southern suburbs kill 12 after rockets launched from Lebanon", 2, 'Google News Top / AP', age=600)
        self.add('Israel strikes on Beirut suburbs killed 12, Lebanon health ministry says after rocket fire', 3, 'Telegram @ClashReport', age=300)
        verdict = dict(post=True, newsworthiness=8, talkability=7, text='Update: Israeli strikes on Beirut suburbs killed 12, Lebanon says.',
                       reply='', reason='new toll', provider='gemini')
        with patch('editor.available', return_value=True), patch('editor.edit', return_value=verdict) as edit:
            postcraft.prepare()
        self.assertIn('Israel strikes Beirut suburbs.', edit.call_args.args[2])
        reasons = json.loads(self.queue()[0]['reasons'])
        self.assertEqual(reasons['quote_of'], '777')


class PacingTests(Case):
    def ready(self, identity, rank, reasons=None, text='Prepared'):
        with prepared.database() as c:
            c.execute('INSERT OR REPLACE INTO prepared_posts VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (identity, text, None, 'https://example.com/'+identity, 'text', rank, app.now()+3600, 0, json.dumps(reasons or {}), app.now()))

    def posted(self, n, ago):
        with prepared.database() as c:
            for i in range(n):
                c.execute('INSERT INTO publications VALUES(?,?,?,?)', ('p%d' % i, app.now()-ago-i, 'posted', str(i)))

    def test_bar_blocks_weak_items_and_rises_with_volume(self):
        self.ready(self.add('A weak story with little interest for most readers at all today', 1, 'Telegram @ClashReport'), 50)
        self.assertIn('quality bar', prepared.publish_one(lambda: None, live=True)['publishing'])
        self.assertIn('below quality bar', prepared.publish_one(lambda: None)['publishing'])
        base = prepared.score_bar()
        self.posted(14, 7200)
        self.assertEqual(prepared.score_bar(), base+12)
        with patch.object(prepared, 'QUIET_HOURS_UTC', '0-24'):
            self.assertEqual(prepared.score_bar(), base+27)

    def test_normal_gap_and_breaking_fast_lane(self):
        self.posted(1, 900)
        self.ready(self.add('Senate votes to end the government shutdown after weeks of talks in Washington', 1, 'Google News Top / AP'), 90)
        with patch('automation.authorization'), patch('automation.send', return_value='1') as send:
            self.assertIn('not due', prepared.publish_one(lambda: None, live=True)['publishing'])
            send.assert_not_called()
            self.ready('1', 95, {'breaking': True})
            self.assertEqual(prepared.publish_one(lambda: None, live=True)['publishing'], 'posted')

    def test_daily_limit_and_source_reply_and_quote(self):
        self.ready(self.add('Senate votes to end the government shutdown after weeks of talks in Washington', 1, 'Google News Top / AP'), 90,
                   {'quote_of': '777', 'reply': 'The bill now goes to the House.', 'category': 'politics', 'source_url': 'https://apnews.com/x'})
        with patch.object(prepared, 'SOURCE_REPLY', True), patch('automation.authorization'), \
                patch('automation.send', return_value='42') as send, patch('automation.reply', return_value='43') as reply:
            result = prepared.publish_one(lambda: None, live=True)
        send.assert_called_once_with('Prepared', quote_of='777')
        reply.assert_called_once_with('The bill now goes to the House.\n\nSource: https://apnews.com/x', '42')
        self.assertEqual(result['source_reply_id'], '43')
        with prepared.database() as c:
            self.assertEqual(c.execute('SELECT category FROM post_meta').fetchone()[0], 'politics')
        with prepared.database() as c:
            self.assertIn('daily limit', prepared.gate(c, 1, 0))

    def test_source_reply_fits_x_limit(self):
        text = prepared.source_reply_text('word '*100, 'https://example.com/'+'a'*200)
        self.assertTrue(text.endswith('Source: https://example.com/'+'a'*200))
        self.assertLessEqual(sum(1 for _ in text.split('Source:')[0])+8+23, 280)

    def test_reply_helper_never_raises(self):
        with patch('automation.authorization', side_effect=ValueError):
            self.assertIsNone(automation.reply('x', '1'))
        client = Mock()
        client.create_tweet.return_value = {'ok': True, 'id': '9'}
        with patch('automation.authorization', return_value=client):
            self.assertEqual(automation.reply('x', '1'), '9')
        client.create_tweet.assert_called_once_with('x', None, reply_to='1')


class LearningTests(Case):
    def test_weights_follow_engagement_within_bounds(self):
        with prepared.database() as c:
            for i in range(12):
                category = 'geopolitics' if i < 6 else 'tech'
                c.execute('INSERT INTO post_meta VALUES(?,?,?,?,?,?,?)', (str(i), 'x%d' % i, category, 'text', 70, app.now()-86400, 1))
                c.execute('INSERT INTO post_metrics VALUES(?,?,?,?,?,?)', ('x%d' % i, 100 if i < 6 else 2, 10 if i < 6 else 0, 20 if i < 6 else 0, None, app.now()))
        report = engagement.learn()
        weights = newsroom.state_get('learned_weights')['category']
        self.assertGreater(weights['geopolitics'], 1)
        self.assertLess(weights['tech'], 1)
        self.assertTrue(all(.7 <= w <= 1.4 for w in weights.values()))
        self.assertEqual(report['best'][0]['score'], 100+30+80)

    def test_track_stores_metrics(self):
        with prepared.database() as c:
            c.execute('INSERT INTO post_meta VALUES(?,?,?,?,?,?,?)', ('1', '99', 'politics', 'text', 70, app.now()-3600, 1))
        client = Mock()
        client.get_tweet.return_value = {'ok': True, 'likes': 5, 'retweets': 2, 'replies': 3}
        self.assertEqual(engagement.track(client)['checked'], 1)
        with prepared.database() as c:
            self.assertEqual(tuple(c.execute('SELECT likes,reposts,replies FROM post_metrics').fetchone()), (5, 2, 3))


class FeedTests(Case):
    def test_google_news_publisher_and_related_coverage(self):
        feed = b'''<rss><channel><item><title>Senate passes bill to end shutdown - Reuters</title><link>https://news.google.com/rss/articles/abc</link>
        <pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate><source url="https://www.reuters.com">Reuters</source>
        <description>&lt;ol&gt;&lt;li&gt;a&lt;/li&gt;&lt;li&gt;b&lt;/li&gt;&lt;li&gt;c&lt;/li&gt;&lt;/ol&gt;</description></item></channel></rss>'''
        with patch('app.fetch', return_value=feed):
            rows = newsroom.rss('https://news.google.com/rss', 'Google News Top', aggregator=True, summary=True)
        self.assertEqual(rows[0]['title'], 'Senate passes bill to end shutdown')
        self.assertEqual(rows[0]['source'], 'Google News Top / Reuters')
        self.assertEqual(rows[0]['related'], 3)

    def test_summary_added_for_rich_feeds(self):
        feed = b'''<rss><channel><item><title>Ceasefire agreed</title><link>https://bbc.co.uk/x</link><pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate>
        <description>Both sides agreed to halt fighting from midnight, mediators said.</description></item></channel></rss>'''
        with patch('app.fetch', return_value=feed):
            rows = newsroom.rss('https://bbc.co.uk/rss', 'BBC World', summary=True)
        self.assertEqual(rows[0]['title'], 'Ceasefire agreed\nBoth sides agreed to halt fighting from midnight, mediators said.')

    def test_config_sources_have_categories(self):
        cfg = newsroom.config()
        self.assertTrue(all(s.get('category') for s in cfg['telegram']+cfg['rss']))
        self.assertLessEqual(cfg['daily_limit'], 30)


if __name__ == '__main__':
    unittest.main()


class ProviderDiscoveryTests(unittest.TestCase):
    def setUp(self):
        editor._gemini_state.update(model=None, thinking=None, down=False)
        editor._groq_state.update(model=None)
        self.addCleanup(lambda: (editor._gemini_state.update(model=None, thinking=None, down=False), editor._groq_state.update(model=None)))

    def test_gemini_falls_back_on_thinking_option_and_retired_model(self):
        ok = {'candidates': [{'content': {'parts': [{'text': 'thinking...', 'thought': True}, {'text': '{"post": false}'}]}}]}
        models = {'models': [{'name': 'models/gemini-9-flash', 'supportedGenerationMethods': ['generateContent']},
                             {'name': 'models/gemini-9-flash-image', 'supportedGenerationMethods': ['generateContent']}]}
        calls = []
        def request(url, headers, body=None, timeout=75):
            calls.append((url, body and body['generationConfig'].get('thinkingConfig')))
            if url.endswith('/models?pageSize=200'):
                return models
            if 'gemini-flash-latest' in url:
                raise editor.HTTPFailure(404, 'HTTP 404 not found')
            if body['generationConfig'].get('thinkingConfig') == {'thinkingLevel': 'low'}:
                raise editor.HTTPFailure(400, 'HTTP 400 thinking level unsupported')
            return ok
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}), patch('editor._request', side_effect=request):
            self.assertEqual(editor.gemini('x'), '{"post": false}')
            self.assertEqual(editor._gemini_state, {'model': 'gemini-9-flash', 'thinking': {'thinkingBudget': 0}, 'down': False})
            calls.clear()
            editor.gemini('y')
        self.assertEqual(len(calls), 1)  # the working model/option is remembered

    def test_gemini_timeout_skips_gemini_for_rest_of_run(self):
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'k'}), patch('editor._request', side_effect=editor.EditorUnavailable('TimeoutError')) as request:
            for _ in range(3):
                with self.assertRaises(editor.EditorUnavailable):
                    editor.gemini('x')
        self.assertEqual(request.call_count, 1)

    def test_groq_picks_current_model_after_404(self):
        def request(url, headers, body=None, timeout=75):
            if url.endswith('/models'):
                return {'data': [{'id': 'whisper-large-v3'}, {'id': 'openai/gpt-oss-120b'}]}
            if body['model'] == editor.GROQ_MODEL:
                raise editor.HTTPFailure(404, 'HTTP 404 model decommissioned')
            return {'choices': [{'message': {'content': '{"post": false}'}}]}
        with patch.dict('os.environ', {'GROQ_API_KEY': 'k'}), patch('editor._request', side_effect=request):
            self.assertEqual(editor.groq('x'), '{"post": false}')
        self.assertEqual(editor._groq_state['model'], 'openai/gpt-oss-120b')

    def test_provider_errors_retry_after_thirty_minutes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(app, 'DB', Path(tmp.name)/'t.db'):
            postcraft.save_edit('k', {'error': 'timeout'})
            self.assertEqual(postcraft.cached_edit('k'), {'error': 'timeout'})
            with patch('app.now', return_value=app.now()+1900):
                self.assertIsNone(postcraft.cached_edit('k'))
