import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import app
import automation
import newsroom
import queue_worker
import engagement

TEXT = 'A new satellite launched today to improve communications across remote regions around the world.'

class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = app.DB
        app.DB = Path(self.tmp.name)/'queue.db'
        newsroom.database().close()
    def tearDown(self):
        app.DB = self.old
        self.tmp.cleanup()
    def add(self, text=TEXT, source='Telegram @ClashReport', url='https://t.me/ClashReport/1', priority=1.8, **kw):
        row = dict(title=text,source=source,url=url,published=app.now(),**kw)
        return newsroom.enqueue([row],priority,['ClashReport','bricsnews'])

class EditorialTests(DatabaseCase):
    def test_news_preserved_while_tags_and_subscription_footer_removed(self):
        text = TEXT+'\n@ClashReport\n#Breaking #News\nSubscribe to our channel for updates'
        self.assertEqual(newsroom.clean_text(text,['ClashReport']),TEXT)
    def test_plain_source_footer_removed(self):
        self.assertEqual(newsroom.clean_text(TEXT+'\nBRICS News',['bricsnews']),TEXT)
    def test_inline_channel_tag_removed_without_losing_news(self):
        self.assertEqual(newsroom.clean_text(TEXT+' @ClashReport',['ClashReport']),TEXT)
    def test_inline_subscription_is_removed_without_removing_news(self):
        self.assertEqual(newsroom.clean_text(TEXT+' Subscribe to our channel for more.'),TEXT)
    def test_topic_hashtag_keeps_meaning(self):
        self.assertEqual(newsroom.clean_text('A new #AI model released today.'),'A new AI model released today.')
    def test_pure_promotions_are_not_rewritten_as_news(self):
        self.assertEqual(self.add('Sponsored post: Earn guaranteed returns using our new investment platform today.')['rejected'],1)
        self.assertEqual(newsroom.queue(),[])
    def test_similar_story_across_sources_is_queued_once(self):
        self.assertEqual(self.add()['added'],1)
        self.assertEqual(self.add(source='Telegram @bricsnews',url='https://t.me/bricsnews/2',priority=1)['duplicates'],1)
        self.assertEqual(len(newsroom.queue()),1)
        self.assertEqual(newsroom.queue()[0]['coverage'],2)
    def test_near_duplicate_punctuation_and_word_order(self):
        self.add()
        self.add('A new satellite launched today, improving communications across remote regions around the world.',url='https://t.me/bricsnews/3',priority=1)
        self.assertEqual(len(newsroom.queue()),1)
    def test_changed_figures_are_distinct_updates(self):
        self.add('Officials say 3 satellites launched today to improve communications across remote regions around the world.')
        self.add('Officials say 4 satellites launched today to improve communications across remote regions around the world.',url='https://t.me/ClashReport/2')
        self.assertEqual(len(newsroom.queue()),2)
    def test_preferred_source_replaces_unposted_duplicate(self):
        self.add(source='Telegram @bricsnews',url='https://t.me/bricsnews/1',priority=1)
        self.add()
        self.assertEqual(len(newsroom.queue()),1)
        self.assertEqual(newsroom.queue()[0]['source'],'Telegram @ClashReport')
    def test_preferred_source_does_not_repost_published_story(self):
        self.add(source='Telegram @bricsnews',url='https://t.me/bricsnews/1',priority=1)
        row=newsroom.queue()[0]
        with newsroom.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)',(row['id'],app.now(),'posted','99'))
        self.add()
        self.assertEqual(newsroom.queue(),[])
    def test_clashreport_rank_boost_is_not_absolute_priority(self):
        self.add()
        self.add('A new research team discovered a technique to reduce energy consumption in data centers worldwide.',source='The Verge',url='https://www.theverge.com/article',priority=1)
        self.assertEqual(newsroom.queue()[0]['source'],'Telegram @ClashReport')
    def test_render_keeps_complete_sentence_and_link(self):
        text=TEXT+' '+('Additional explanatory context '*30)+'.'
        self.add(text)
        row=newsroom.queue()[0]
        rendered=newsroom.render(row)
        self.assertEqual(rendered,TEXT+'\n'+row['url'])
    def test_freshness_and_future_rejection(self):
        for stamp in (app.now()-6*86400,app.now()+3600):
            row=dict(title=TEXT,source='Telegram @ClashReport',url='https://t.me/ClashReport/1',published=stamp)
            self.assertEqual(newsroom.enqueue([row],1,[])['rejected'],1)
    def test_original_posted_titles_also_deduplicate(self):
        app.ingest([dict(title=TEXT,url='https://example.com/original',source='Old',published=app.now())])
        with automation.ledger() as c:
            row=c.execute('SELECT id FROM stories').fetchone()
            c.execute('INSERT INTO publications VALUES(?,?,?,?)',(row[0],app.now(),'posted','9'))
        self.add()
        self.assertEqual(newsroom.queue(),[])

class SourceTests(unittest.TestCase):
    def test_public_telegram_extracts_body_time_views_and_permalink(self):
        page='''<div class="tgme_widget_message" data-post="ClashReport/123"><div class="tgme_widget_message_text">A useful news report<br>@ClashReport</div><time datetime="2026-10-17T10:00:00+00:00"></time><span class="tgme_widget_message_views">12.4K</span></div>'''
        with patch('app.fetch',return_value=page.encode()):
            rows=newsroom.telegram('ClashReport',pages=1)
        self.assertEqual(rows[0]['views'],12400)
        self.assertEqual(rows[0]['url'],'https://t.me/ClashReport/123')
        self.assertIn('\n',rows[0]['title'])
    def test_unavailable_public_channel_is_reported_as_failure(self):
        with patch('app.fetch',return_value=b'<html>Channel unavailable</html>'):
            with self.assertRaises(ValueError):
                newsroom.telegram('missing')
    def test_verge_atom_feed(self):
        feed=b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A new discovery</title><published>2026-10-17T10:00:00Z</published><link rel="alternate" href="https://www.theverge.com/article"/></entry></feed>'''
        with patch('app.fetch',return_value=feed):
            rows=newsroom.verge('https://www.theverge.com/rss/index.xml')
        self.assertEqual(rows[0]['source'],'The Verge')
        self.assertEqual(rows[0]['url'],'https://www.theverge.com/article')
    def test_config_contains_all_requested_channels_and_preference(self):
        cfg=newsroom.config()
        channels={s['channel'].lower():s['priority'] for s in cfg['telegram']}
        self.assertEqual(set(channels),set('bricsnews hitech hiaimediaen cointelegraph001 cointelegraph nytimes watcherguru rtnews clashreport ddgeopolitics'.split()))
        self.assertGreater(channels['clashreport'],channels['bricsnews'])

class SchedulingTests(DatabaseCase):
    def test_dense_and_sparse_queue_pacing(self):
        self.assertEqual(newsroom.adaptive_interval(70,7200),120)
        self.assertEqual(newsroom.adaptive_interval(40,7200),180)
        self.assertEqual(newsroom.adaptive_interval(20,7200),240)
    def test_collection_job_prepares_then_publishes_only_one_without_sleeping(self):
        def collect():
            self.add(url='https://t.me/ClashReport/1')
            self.add('Researchers developed new batteries that could reduce charging times for electric vehicles across Europe.',url='https://t.me/ClashReport/2')
            return {'Telegram @ClashReport':{'added':2}}
        with patch('newsroom.collect',side_effect=collect) as collector,patch('automation.authorization'),patch('automation.send',return_value='1') as send,patch('time.sleep') as sleep:
            report=queue_worker.run(Mock(),mode='collect',live=True)
        collector.assert_called_once()
        send.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(report['preparation']['prepared'],2)
        self.assertEqual(report['publication']['publishing'],'posted')
    def test_reservation_saved_before_send_and_restored_interval_enforced(self):
        self.add()
        events=[]
        with patch('automation.authorization'),patch('automation.send',side_effect=lambda text:events.append('send') or '1'):
            result=newsroom.post_one(lambda:events.append('save'),live=True,next_interval=180)
        self.assertEqual(result['publishing'],'posted')
        self.assertEqual(events,['save','send','save'])
        self.assertGreater(newsroom.state_get('next_post_at'),app.now()+170)
    def test_remote_save_failure_never_sends(self):
        self.add()
        with patch('automation.authorization'),patch('automation.send') as send:
            with self.assertRaises(RuntimeError):
                newsroom.post_one(lambda:(_ for _ in ()).throw(RuntimeError()),live=True)
            send.assert_not_called()
    def test_uncertain_post_pauses_new_story(self):
        self.add()
        with patch('automation.authorization'),patch('automation.send',side_effect=TimeoutError):
            self.assertIn('paused',newsroom.post_one(lambda:None,live=True)['publishing'])
        self.add('Researchers developed new batteries that could reduce charging times for electric vehicles across Europe.',url='https://t.me/ClashReport/2')
        with patch('automation.authorization'),patch('automation.send') as send:
            self.assertIn('paused',newsroom.post_one(lambda:None,live=True)['publishing'])
            send.assert_not_called()
    def test_failed_collection_is_not_reported_as_green(self):
        with patch('newsroom.collect',return_value={'Telegram @ClashReport':{'error':'Unavailable'}}):
            report=queue_worker.run(Mock(),live=False)
        self.assertFalse(report['ok'])
    def test_publish_job_never_collects_rewrites_or_sleeps(self):
        import postcraft
        self.add()
        postcraft.prepare()
        with patch('newsroom.collect') as collect,patch('postcraft.prepare') as prepare,patch('time.sleep') as sleep:
            report=queue_worker.run(Mock(),mode='publish',live=False)
        collect.assert_not_called()
        prepare.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(report['publishing'],'preview')

class ReplyTests(DatabaseCase):
    def parent(self):
        with newsroom.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)',('a',app.now(),'posted','99'))
    def test_all_direct_replies_liked_once_including_criticism(self):
        self.parent()
        client=Mock()
        client.whoami.return_value={'user_id':'self'}
        client.like.return_value={'ok':True}
        rows=[dict(id='10',author_id='reader',text='I disagree with your reporting.'),dict(id='11',author_id='self',text='Own comment')]
        with patch('automation.authorization',return_value=client),patch('engagement.direct_replies',return_value=rows):
            engagement.scan(lambda:None,auto_like=True)
            engagement.scan(lambda:None,auto_like=True)
        client.like.assert_called_once_with('10')
    def test_review_only_does_not_like(self):
        self.parent()
        client=Mock();client.whoami.return_value={'user_id':'self'}
        with patch('automation.authorization',return_value=client),patch('engagement.direct_replies',return_value=[dict(id='10',author_id='other',text='Thanks')]):
            engagement.scan(lambda:None,auto_like=False)
        client.like.assert_not_called()
    def test_ambiguous_like_not_repeated(self):
        self.parent()
        client=Mock();client.whoami.return_value={'user_id':'self'};client.like.side_effect=TimeoutError
        with patch('automation.authorization',return_value=client),patch('engagement.direct_replies',return_value=[dict(id='10',author_id='other',text='Thanks')]):
            engagement.scan(lambda:None,auto_like=True)
            engagement.scan(lambda:None,auto_like=True)
        self.assertEqual(client.like.call_count,1)
    def test_failed_history_save_prevents_like(self):
        self.parent()
        client=Mock();client.whoami.return_value={'user_id':'self'}
        with patch('automation.authorization',return_value=client),patch('engagement.direct_replies',return_value=[dict(id='10',author_id='other',text='Comment')]):
            with self.assertRaises(RuntimeError):
                engagement.scan(lambda:(_ for _ in ()).throw(RuntimeError()),auto_like=True)
        client.like.assert_not_called()
    def test_unknown_identity_never_queues_own_or_other_likes(self):
        self.parent()
        client=Mock();client.whoami.return_value={'user_id':None}
        with patch('automation.authorization',return_value=client),patch('engagement.direct_replies',return_value=[dict(id='10',author_id='other',text='Comment')]):
            report=engagement.scan(lambda:None,auto_like=True)
        client.like.assert_not_called()
        self.assertFalse(report['identity_ready'])
        self.assertEqual(report['pending_likes'],0)
    def test_direct_parent_confirmation_excludes_nested_and_unknown(self):
        client=Mock()
        client._session=None
        client.get_replies.return_value=[dict(id='1',in_reply_to_status_id_str='99'),dict(id='2',in_reply_to_status_id_str='1'),dict(id='3')]
        self.assertEqual([r['id'] for r in engagement.direct_replies(client,'99')],['1'])
    def test_pinned_tweetkit_response_hook_recovers_parent_ids(self):
        client=Mock();client._session.hooks={'response':[]}
        def fetch(post_id,limit):
            response=Mock();response.url='https://x.com/i/api/graphql/id/TweetDetail'
            response.json.return_value={'data':[{'legacy':{'id_str':'10','in_reply_to_status_id_str':'99'}}]}
            for hook in client._session.hooks['response']:
                hook(response)
            return [dict(id='10',text='Comment'),dict(id='11',text='Other thread')]
        client.get_replies.side_effect=fetch
        self.assertEqual([r['id'] for r in engagement.direct_replies(client,'99')],['10'])
        self.assertEqual(client._session.hooks['response'],[])

if __name__=='__main__':
    unittest.main()
