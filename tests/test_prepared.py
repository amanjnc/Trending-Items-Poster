import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from tweetkit_x import TweetKit

import app
import automation
import newsroom
import postcraft
import prepared
import queue_worker
import selected_post

class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old=app.DB
        app.DB=Path(self.tmp.name)/'test.db'
        newsroom.database().close()
        prepared.database().close()
    def tearDown(self):
        app.DB=self.old
        self.tmp.cleanup()
    def add(self,text,number=1,age=0,media=None,source="Telegram @ClashReport"):
        url=f'https://t.me/ClashReport/{number}'
        identity=str(number)
        with newsroom.database() as c:
            c.execute('INSERT INTO stories(id,title,url,source,published,collected,score) VALUES(?,?,?,?,?,?,?)',(identity,text,url,source,app.now()-age,app.now(),0))
            c.execute('INSERT INTO news_items VALUES(?,?,?,?,?)',(identity,text,str(number),1.8,0))
            c.execute('INSERT INTO observations VALUES(?,?,?)',(url,identity,source))
            if media:
                c.execute('INSERT INTO story_media VALUES(?,?)',(identity,media))
        return identity
    def ready(self,identity,text='Prepared post text',position=0,expires=None,media=None,rank=50):
        with prepared.database() as c:
            c.execute('INSERT OR REPLACE INTO prepared_posts VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,text,media,'https://t.me/ClashReport/'+identity,'text_image' if media else 'text',rank,expires or app.now()+3600,position,'{}',app.now()))

class EntityTests(FixtureCase):
    def test_full_and_short_pezeshkian_names_use_confirmed_persian_profile(self):
        client=Mock(spec=TweetKit)
        client.user_profile.return_value=dict(ok=True,id='123',username='drpezeshkian',name='مسعود پزشکیان')
        texts=['Iranian President Masoud Pezeshkian:\nMuslims should unite.',
               'Iranian President Pezeshkian on the Mecca Alliance:\nThis is a step forward.']
        entities=postcraft.resolve_entities(texts,client=client)
        self.assertEqual(len(entities),1)
        for text in texts:
            result=postcraft.compose(dict(body=text,url='https://example.com'),entities)
            self.assertIn('@drpezeshkian',result)
            self.assertNotIn('Pezeshkian',result)
        self.assertEqual(postcraft.replace_mentions('Pezeshkian won a school prize.',entities),'Pezeshkian won a school prize.')
    def test_institution_names_are_confirmed_without_tagging_common_pronouns(self):
        client=Mock(spec=TweetKit)
        client.user_profile.return_value=dict(ok=True,id='123',username='WHO',name='World Health Organization (WHO)')
        entities=postcraft.resolve_entities(['World Health Organization announced new guidance.'],client=client)
        self.assertEqual(postcraft.replace_mentions('World Health Organization says who can apply.',entities),'@WHO says who can apply.')
    def test_canadas_carney_is_tagged_with_confirmed_account(self):
        body="Canada's Carney: We've got to take care of ourselves, and we always take care of each other."
        entities=postcraft.resolve_entities([body],client=self.profile_client())
        result=postcraft.compose({'body':body,'url':'https://example.com'},entities)
        self.assertEqual(result,"@MarkJCarney: We've got to take care of ourselves, and we always take care of each other.")
    def test_contextual_surname_does_not_leak_between_posts(self):
        client=self.profile_client()
        body='Carney says Canadian businesses need more trading options.'
        entities=postcraft.resolve_entities([body],client=client)
        self.assertIn('@MarkJCarney',postcraft.replace_mentions(body,entities))
        self.assertEqual(postcraft.replace_mentions('Carney won a local school prize.',entities),'Carney won a local school prize.')
        client.reset_mock()
        self.assertEqual(postcraft.resolve_entities(['Canada has new policies.','Carney won a local school prize.'],client=client),[])
        client.user_profile.assert_not_called()
    def test_legacy_failed_lookups_are_retried_after_sdk_fix(self):
        with prepared.database() as c:
            c.execute('INSERT INTO entity_cache VALUES(?,?,?)',('replit',json.dumps(dict(valid=False,names=[])),app.now()+21600))
        client=self.profile_client()
        self.assertEqual(postcraft.resolve_entities(['Replit'],client=client)[0]['handle'],'Replit')
        client.user_profile.assert_called_once_with('Replit')
    def test_installed_sdk_profile_method_and_response_are_used(self):
        # Execute the pinned SDK method; mock only its HTTP boundary, without credentials.
        client=TweetKit.__new__(TweetKit)
        client.timeout=8
        client._headers=Mock(return_value={})
        client._session=Mock()
        client._session.get.return_value.json.return_value={'data':{'user':{'result':{
            'rest_id':'123','core':{'screen_name':'MarkJCarney','name':'Mark Carney'},'legacy':{}}}}}
        entities=postcraft.resolve_entities(["Canada's Carney spoke today."],client=client)
        self.assertEqual(entities[0]['handle'],'MarkJCarney')
        self.assertIn('UserByScreenName',client._session.get.call_args.args[0])
        self.assertEqual(newsroom.state_get('identity_resolution')['markjcarney'],'verified')
    def profile_client(self):
        client=Mock(spec=TweetKit)
        lookup={'replit':('Replit','Replit'),'anthropicai':('AnthropicAI','Anthropic'),'amasad':('amasad','Amjad Masad'),'markwahlberg':('markwahlberg','Mark Wahlberg'),'markjcarney':('MarkJCarney','Mark Carney')}
        def profile(handle):
            username,name=lookup[handle.lower()]
            return dict(ok=True,id='123',username=username,name=name)
        client.user_profile.side_effect=profile
        return client
    def test_requested_company_and_named_speaker_example(self):
        client=self.profile_client()
        headline='Replit is no longer working with Anthropic, CEO says'
        context='Replit CEO Amjad Masad discussed the decision.'
        entities=postcraft.resolve_entities([headline,context],client=client)
        row={'body':headline,'url':'https://example.com'}
        text=postcraft.compose(row,entities,context)
        self.assertEqual(text,'@Replit is no longer working with @AnthropicAI, says @amasad.')
        self.assertNotIn('https://',text)
    def test_other_companies_ceos_use_source_named_people_not_assumptions(self):
        text='Microsoft is changing its policy, CEO says'
        entities=[dict(names=['Microsoft'],handle='Microsoft'),dict(names=['Satya Nadella'],handle='satyanadella')]
        final=postcraft.compose({'body':text,'url':'https://example.com'},entities,'Microsoft CEO Satya Nadella described the policy.')
        self.assertIn('says @satyanadella',final)
    def test_unconfirmed_speaker_is_not_invented(self):
        headline='Replit is no longer working with Anthropic, CEO says'
        entities=postcraft.resolve_entities([headline,'Amjad Masad'],client=self.profile_client())
        text=postcraft.compose({'body':headline,'url':'https://example.com'},entities,'The report does not name the speaker.')
        self.assertNotIn('@amasad',text)
        self.assertIn('CEO says',text)
    def test_person_replaced_without_strengthening_intent(self):
        text='Mark Wahlberg wants to take on the longevity-startup crowd with an affordable gym concept'
        entities=postcraft.resolve_entities([text],client=self.profile_client())
        final=postcraft.compose({'body':text,'url':'https://example.com'},entities)
        self.assertIn('@markwahlberg wants',final)
        self.assertNotIn('has launched',final)
    def test_lookup_failure_keeps_names(self):
        client=Mock(spec=TweetKit);client.user_profile.side_effect=RuntimeError('private HTTP details')
        entities=postcraft.resolve_entities(['Replit'],client=client)
        self.assertEqual(entities,[])
        self.assertEqual(postcraft.replace_mentions('Replit announced an update.',entities),'Replit announced an update.')
    def test_wrong_display_identity_is_not_accepted(self):
        client=Mock(spec=TweetKit);client.user_profile.return_value=dict(ok=True,id='1',username='Replit',name='Unrelated person')
        self.assertEqual(postcraft.resolve_entities(['Replit'],client=client),[])
    def test_valid_identity_is_cached(self):
        client=self.profile_client()
        first=postcraft.resolve_entities(['Replit'],client=client)
        second=postcraft.resolve_entities(['Replit'],client=client)
        self.assertEqual(first,second)
        self.assertEqual(client.user_profile.call_count,1)
    def test_unknown_name_is_not_guessed(self):
        client=self.profile_client()
        self.assertEqual(postcraft.resolve_entities(['Some Unknown Founder'],client=client),[])
        client.user_profile.assert_not_called()
    def test_already_tagged_entities_not_double_tagged(self):
        self.assertEqual(postcraft.replace_mentions('@Replit and Replit',[dict(names=['Replit'],handle='Replit')]),'@Replit and @Replit')
    def test_ambiguous_common_nouns_not_tagged(self):
        entities=[dict(names=['Apple'],handle='Apple'),dict(names=['Meta'],handle='Meta')]
        self.assertEqual(postcraft.replace_mentions('Apple pie research and Meta-analysis.',entities),'Apple pie research and Meta-analysis.')

class PreparationTests(FixtureCase):
    def test_one_malformed_story_does_not_abort_preparing_other_stories(self):
        self.add('A detailed news item with important context and information for readers.',1,source='A')
        self.add('Another detailed news item with important context and information for readers.',2,source='B')
        original=postcraft.compose
        def compose(row,*args,**kwargs):
            if row['id']=='1':raise ValueError('bad item')
            return original(row,*args,**kwargs)
        with patch('postcraft.compose',side_effect=compose):report=postcraft.prepare()
        self.assertEqual(report['prepared'],1)
        self.assertEqual(report['skipped_invalid_items'][0]['story_id'],'1')
        self.assertEqual(prepared.next_post()['story_id'],'2')
    def test_source_never_repeats_in_two_positions_either_side(self):
        for n in range(16):
            self.add('A detailed news update explains recent developments and important context for readers.',n+1,
                     source='Source '+str(n%4),media='https://cdn4.telesco.pe/image.jpg' if n%4==0 else None)
        report=postcraft.prepare()
        self.assertEqual(report['prepared'],16)
        with prepared.database() as c:
            sources=[r[0] for r in c.execute('SELECT s.source FROM prepared_posts q JOIN stories s ON s.id=q.story_id ORDER BY position')]
        for i,source in enumerate(sources):
            self.assertNotIn(source,sources[max(0,i-2):i])
            self.assertNotIn(source,sources[i+1:i+3])
    def test_insufficient_source_diversity_defers_items_without_relaxing_rule(self):
        for n,source in enumerate(['A','A','B','B']):
            self.add('A detailed news update explains recent developments and important context for readers.',n+1,source=source)
        report=postcraft.prepare()
        self.assertEqual(report['prepared'],2)
        self.assertEqual(report['deferred_for_source_spacing'],2)
    def test_order_starts_after_last_two_published_sources(self):
        for n,source in enumerate(['A','B','A','B','C']):
            identity=self.add('A detailed news update explains recent developments and important context for readers.',n+1,source=source)
            if n<2:
                with prepared.database() as c:
                    c.execute('INSERT INTO publications VALUES(?,?,?,?)',(identity,app.now()-300+n*100,'posted',str(n+100)))
        postcraft.prepare()
        self.assertEqual(prepared.next_post()['story_id'],'5')
    def test_source_paragraphs_have_one_blank_line_between_them(self):
        body="Canada's Carney:\nIf they're ready to move, we'll move.\n\nLook, life's too short to play around other countries' political timing.\nWe're ready to do a deal if the U.S. is going to negotiate in good faith."
        entities=[dict(names=["Canada's Carney"],handle='MarkJCarney')]
        final=postcraft.compose({'body':body,'url':'https://example.com'},entities)
        self.assertEqual(final,"@MarkJCarney:\n\nIf they're ready to move, we'll move.\n\nLook, life's too short to play around other countries' political timing.\n\nWe're ready to do a deal if the U.S. is going to negotiate in good faith.")
        self.assertNotIn('\n\n\n',final)
    def test_paragraphs_survive_both_sentence_and_word_length_trimming(self):
        for body in ('First paragraph ends here.\nSecond paragraph has essential details.\n'+('More details follow. '*30),
                     'First paragraph without a full stop\n'+('more details '*40)):
            result=postcraft.compose({'body':body,'url':'https://example.com'},[])
            self.assertIn('\n\n',result)
            self.assertLessEqual(postcraft.weight(result),280)
    def test_strict_four_format_order_overrides_freshness_and_diversity(self):
        # The oldest, lower-scored image still precedes the newest text-only item.
        base='A detailed research guide explains the future of battery technology and energy storage.'
        self.add(base+' Read the full report for details.',1,media='https://cdn4.telesco.pe/file/one.jpg',source='Source A')
        self.add(base,2,source="Source B")
        self.add(base+' Read the full report for details.',3,source='Source C')
        self.add(base,4,age=18*3600,media='https://cdn4.telesco.pe/file/four.jpg',source='Source D')
        postcraft.prepare()
        with prepared.database() as c:
            formats=[r[0] for r in c.execute('SELECT format FROM prepared_posts ORDER BY position')]
        self.assertEqual(formats,['text_image','text','text_image_link','text_link'])
    def test_country_abbreviation_stays_in_sentence(self):
        body='Canada trades more with the U.S. than elsewhere.'
        self.assertEqual(postcraft.compose({'body':body,'url':'https://example.com'},[]),body)
        long_body=body+' '+('Further details follow. '*30)
        result=postcraft.compose({'body':long_body,'url':'https://example.com'},[])
        self.assertIn(body,result)
        self.assertLessEqual(postcraft.weight(result),280)

    def test_prepared_posts_omit_external_links_by_default(self):
        self.add('A new research team announced findings that could improve batteries used in electric vehicles.')
        report=postcraft.prepare()
        self.assertEqual(report['prepared'],1)
        self.assertNotIn('http',prepared.next_post()['text'])
        self.assertIn('https://t.me/',prepared.next_post()['source_url'])
    def test_media_preference_for_equally_useful_items(self):
        text='A new research team announced findings that could improve batteries used in electric vehicles.'
        self.add(text,1)
        self.add(text,2,media='https://cdn4.cdn-telegram.org/file/photo.jpg')
        postcraft.prepare()
        self.assertEqual(prepared.next_post()['format'],'text_image')
    def test_link_format_is_ranked_below_equivalent_link_free_post(self):
        self.add('A new research report explains battery technology and its role in the future of electric vehicles.',1)
        self.add('A new research report explains battery technology. Read the full report for detailed results.',2,source='Source B')
        report=postcraft.prepare()
        formats={r['format']:r['rank'] for r in report['top5']}
        self.assertGreater(formats['text'],formats['text_link'])
    def test_time_bound_outage_expired_but_evergreen_not(self):
        now=app.now()
        outage=dict(body='A major network outage disrupted services.',published=now-7*3600)
        guide=dict(body='A research guide explaining battery chemistry.',published=now-18*3600)
        self.assertLess(postcraft.lifetime(outage)[0],now)
        self.assertGreater(postcraft.lifetime(guide)[0],now)
    def test_eighteen_hour_old_research_can_remain_but_one_day_old_cannot(self):
        self.add('A research guide explaining a new approach to energy storage for homes and businesses.',1,age=18*3600)
        self.add('A research guide explaining a different approach to energy storage for homes and businesses.',2,age=86400)
        self.assertEqual(postcraft.prepare()['prepared'],1)
        self.assertEqual(prepared.next_post()['story_id'],'1')
    def test_no_item_lives_beyond_one_day(self):
        row=dict(body='An evergreen research guide.',published=app.now())
        self.assertLessEqual(postcraft.lifetime(row)[0],row['published']+86400)
    def test_relative_deadline_expires_at_event_time(self):
        row=dict(body='Registration closes in 2 hours for the event.',published=app.now())
        self.assertEqual(postcraft.lifetime(row)[0],row['published']+7200)
    def test_expired_items_excluded_during_preparation(self):
        self.add('A major network outage disrupted services for customers in several countries around the world.',age=7*3600)
        self.assertEqual(postcraft.prepare()['prepared'],0)
    def test_urgency_bonus_is_capped_and_not_primary(self):
        self.add('A major network outage disrupted services for customers in several countries around the world.',age=5*3600)
        row=postcraft.prepare()['top5'][0]
        self.assertLessEqual(row['reasons']['urgency'],5)
        self.assertGreater(row['rank'],row['reasons']['urgency']*3)
    def test_deals_and_discounts_do_not_enter_prepared_queue(self):
        self.add('The latest streaming box just received its first discount for shoppers this weekend.')
        self.assertEqual(postcraft.prepare()['prepared'],0)
    def test_human_uncertainty_survives_editing(self):
        row=dict(body='The company reportedly wants to launch a new product, but the timeline is unconfirmed.',url='https://example.com')
        text=postcraft.compose(row,[])
        self.assertIn('reportedly wants',text)
        self.assertIn('unconfirmed',text)
    def test_length_budget_respects_mentions_and_unicode(self):
        row=dict(body=('A lengthy update with important context and details. '*30),url='https://example.com')
        self.assertLessEqual(postcraft.weight(postcraft.compose(row,[])),280)
        linked=postcraft.compose(row,[],include_link=True)
        self.assertLessEqual(postcraft.weight(linked.rsplit('\n',1)[0])+24,280)

class FastPublisherTests(FixtureCase):
    def test_watchdog_quarantines_abandoned_attempt_and_posts_different_item(self):
        first=self.add('An old attempt whose outcome is unknown and must never be repeated.',1,source='A')
        second=self.add('Another recent story with sufficient context and news detail for publishing.',2,source='B')
        self.ready(first,'Never resend this',position=0);self.ready(second,'Different safe item',position=1)
        with prepared.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,NULL)',(first,app.now()-121,'uncertain'))
        saves=Mock()
        with patch('automation.authorization'),patch('automation.send',return_value='123') as send:
            result=prepared.publish_one(saves,live=True)
        send.assert_called_once_with('Different safe item')
        self.assertEqual(result['post_id'],'123')
        self.assertEqual(prepared.database().execute('SELECT state FROM publications WHERE story_id=?',(first,)).fetchone()[0],'quarantined_uncertain')
        self.assertEqual(saves.call_count,3)
    def test_watchdog_leaves_recent_attempt_reserved(self):
        identity=self.add('A recent attempt which could still be in progress and must not be repeated.')
        with prepared.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,NULL)',(identity,app.now()-60,'uncertain'))
        self.assertEqual(prepared.recover_stale_attempts()['quarantined'],[])
        self.assertEqual(prepared.database().execute('SELECT state FROM publications').fetchone()[0],'uncertain')
    def test_three_failures_open_finite_circuit_and_old_errors_cannot_reopen_it(self):
        now=app.now()
        with prepared.database() as c:
            for n in range(3):
                c.execute('INSERT INTO publications VALUES(?,?,?,NULL)',(str(n),now-300+n,'media_failed'))
        self.assertTrue(prepared.recover_stale_attempts()['circuit_opened'])
        with prepared.database() as c:self.assertIn('cooldown',prepared.gate(c,400,0))
        with patch('app.now',return_value=now+1900):
            self.assertFalse(prepared.recover_stale_attempts()['circuit_opened'])
            with prepared.database() as c:self.assertIsNone(prepared.gate(c,400,0))
    def test_failed_upload_is_recorded_without_pausing_the_next_story(self):
        identity=self.add('A detailed news story with enough context for publishing to our readers.',1,source='A')
        next_id=self.add('Another detailed news story with enough context for publishing to our readers.',2,source='B')
        self.ready(identity,position=0);self.ready(next_id,position=1)
        with patch('automation.authorization'),patch('automation.send',side_effect=automation.MediaUploadFailed('upload failed')):
            report=prepared.publish_one(lambda:None,live=True)
        self.assertEqual(report['failure_category'],'upload_failed')
        with prepared.database() as c:
            self.assertEqual(c.execute('SELECT state FROM publications WHERE story_id=?',(identity,)).fetchone()[0],'media_failed')
            self.assertEqual(c.execute('SELECT text FROM publication_payloads WHERE story_id=?',(identity,)).fetchone()[0],'Prepared post text')
            self.assertIsNone(prepared.gate(c,400,0))
    def test_rate_limit_has_finite_backoff_and_never_repeats_rejected_item(self):
        identity=self.add('A detailed news story with enough context for publishing to our readers.')
        self.ready(identity)
        with patch('automation.authorization'),patch('automation.send',side_effect=automation.PostingRejected(429,'rate_limit')):
            report=prepared.publish_one(lambda:None,live=True)
        self.assertEqual(report['http_status'],429)
        with prepared.database() as c:
            self.assertIn('cooldown',prepared.gate(c,400,0))
            self.assertEqual(c.execute('SELECT state FROM publications').fetchone()[0],'rejected')
        self.assertIsNone(prepared.next_post())
    def test_quarantined_attempt_stays_reserved_without_stopping_other_posts(self):
        identity=self.add('An uncertain old story which must not be sent again during recovery.')
        self.ready(identity)
        with prepared.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,NULL)',(identity,app.now()-3600,'quarantined_uncertain'))
            self.assertIsNone(prepared.gate(c,400,0))
        self.assertIsNone(prepared.next_post())
    def test_explicit_reset_clears_pending_payloads_but_preserves_posting_history(self):
        for n in range(1,4):
            identity=self.add('A detailed news update explains recent developments with important context for readers.',n,
                              media='https://cdn4.telesco.pe/image.jpg')
            self.ready(identity)
        with prepared.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)',('2',app.now(),'posted','123'))
            c.execute('INSERT INTO publications VALUES(?,?,?,NULL)',('3',app.now(),'uncertain'))
        self.assertEqual(prepared.clear_pending(),1)
        with newsroom.database() as c:
            for table,column in (('prepared_posts','story_id'),('news_items','story_id'),('story_media','story_id'),('observations','story_id'),('stories','id')):
                self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table+' WHERE '+column+'="1"').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM publications').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM stories').fetchone()[0],2)
    def test_one_day_old_payload_is_deleted_even_when_legacy_expiry_allows_five_days(self):
        identity=self.add('A detailed old research guide with sufficient context and information for readers.',age=86400,
                          media='https://cdn4.telesco.pe/old.jpg')
        self.ready(identity,expires=app.now()+4*86400)
        saved=Mock()
        with patch('automation.authorization') as auth,patch('automation.send') as send:
            prepared.publish_one(saved,live=True)
        auth.assert_not_called();send.assert_not_called();saved.assert_called_once()
        with newsroom.database() as c:
            for table in ('prepared_posts','news_items','story_media'):
                self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table+' WHERE story_id=?',(identity,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT status FROM stories WHERE id=?',(identity,)).fetchone()[0],'expired')
    def test_expiring_bridge_does_not_allow_repeated_actual_source(self):
        old=self.add('A detailed previously published story about recent developments for readers.',1,source='A')
        repeat=self.add('A detailed fresh story from the same source about other developments for readers.',2,source='A')
        other=self.add('A detailed fresh story from a different source about recent developments for readers.',3,source='B')
        with prepared.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)',(old,app.now()-300,'posted','123'))
        self.ready(repeat,'Same source queued first',position=0)
        self.ready(other,'Different source queued next',position=1)
        with patch('automation.authorization'),patch('automation.send',return_value='456') as send:
            prepared.publish_one(lambda:None,live=True)
        send.assert_called_once_with('Different source queued next')
    def test_download_requires_video_signature_and_enforces_size_limit(self):
        with patch('urllib.request.build_opener') as opener:
            response=opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value=b'\x00\x00\x00\x18ftypmp42'+b'video payload'
            path=prepared.download_media('https://cdn4.telesco.pe/movie.mp4',self.tmp.name,video=True)
            self.assertEqual(path.suffix,'.mp4')
            response.read.assert_called_once_with(20_000_001)
            response.read.return_value=b'\xff\xd8\xffphoto is not a video'
            with self.assertRaisesRegex(ValueError,'video content'):
                prepared.download_media('https://cdn4.telesco.pe/movie.mp4',self.tmp.name,video=True)
            response.read.return_value=b'x'*20_000_001
            with self.assertRaisesRegex(ValueError,'too large'):
                prepared.download_media('https://cdn4.telesco.pe/movie.mp4',self.tmp.name,video=True)
    def test_success_removes_payloads_but_keeps_duplicate_and_reply_history(self):
        identity=self.add('A carefully sourced news item with substantial context and sufficient detail.',media='https://cdn4.telesco.pe/photo.jpg')
        self.ready(identity)
        with newsroom.database() as c:
            c.execute('INSERT INTO story_attachments VALUES(?,?,?)',(identity,'image','https://cdn4.telesco.pe/photo.jpg'))
        with patch('automation.authorization'),patch('automation.send',return_value='123'):
            prepared.publish_one(lambda:None,live=True)
        with newsroom.database() as c:
            for table in ('prepared_posts','story_media','story_attachments','news_items'):
                self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table+' WHERE story_id=?',(identity,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT post_id FROM publications WHERE story_id=?',(identity,)).fetchone()[0],'123')
            self.assertIsNotNone(c.execute('SELECT id FROM stories WHERE id=?',(identity,)).fetchone())
            self.assertIsNotNone(c.execute('SELECT url FROM observations WHERE story_id=?',(identity,)).fetchone())
    def test_uncertain_send_preserves_payload_for_reconciliation(self):
        identity=self.add('A carefully sourced news item with substantial context and sufficient detail.')
        self.ready(identity)
        with patch('automation.authorization'),patch('automation.send',side_effect=TimeoutError):
            prepared.publish_one(lambda:None,live=True)
        self.assertIsNotNone(prepared.database().execute('SELECT story_id FROM prepared_posts WHERE story_id=?',(identity,)).fetchone())
    def test_video_upload_and_selected_media_failure(self):
        identity=self.add('A carefully sourced news item with substantial context and sufficient detail.')
        self.ready(identity,media='https://cdn4.telesco.pe/movie.mp4')
        with prepared.database() as c:
            c.execute('UPDATE prepared_posts SET format="text_video" WHERE story_id=?',(identity,))
        with patch('prepared.download_media',side_effect=TimeoutError),patch('automation.authorization'),patch('automation.send') as send:
            result=prepared.publish_one(lambda:None,live=True,story_id=identity,require_media=True)
        self.assertFalse(result['ok']);send.assert_not_called()
        with patch('prepared.download_media',return_value=Path('/tmp/movie.mp4')) as download,patch('automation.authorization'),patch('automation.send',return_value='123'):
            result=prepared.publish_one(lambda:None,live=True,story_id=identity,require_media=True)
        self.assertTrue(result['video_uploaded']);self.assertFalse(result['image_uploaded'])
        download.assert_called_once()
        self.assertTrue(download.call_args.kwargs['video'])
    def test_publish_only_uses_saved_position_without_processing(self):
        first=self.add('The first carefully prepared news update has sufficient context for our readers.',1)
        second=self.add('The second carefully prepared news update has sufficient context for our readers.',2)
        self.ready(first,'First saved text',position=0,rank=10)
        self.ready(second,'Second saved text',position=1,rank=100)
        with patch('newsroom.collect') as collect,patch('postcraft.prepare') as prepare,patch('postcraft.resolve_entities') as identities,patch('time.sleep') as sleep,patch('automation.authorization'),patch('automation.send',return_value='999') as send:
            report=queue_worker.run(Mock(),mode='publish',live=True)
        send.assert_called_once_with('First saved text')
        collect.assert_not_called();prepare.assert_not_called();identities.assert_not_called();sleep.assert_not_called()
        self.assertEqual(report['publishing'],'posted')
    def test_expiry_checked_even_without_recollection(self):
        old=self.add('A time sensitive news update that should no longer be published to the account.',1)
        fresh=self.add('A fresh news update which is still appropriate to publish to the account.',2)
        self.ready(old,position=0,expires=app.now()-1)
        self.ready(fresh,'Fresh prepared text',position=1)
        with patch('automation.authorization'),patch('automation.send',return_value='123') as send:
            prepared.publish_one(lambda:None,live=True)
        send.assert_called_once_with('Fresh prepared text')
    def test_durable_reservation_precedes_send(self):
        identity=self.add('A freshly collected news update that is ready to be posted to our account.')
        self.ready(identity)
        events=[]
        with patch('automation.authorization'),patch('automation.send',side_effect=lambda text:events.append('send') or '123'):
            prepared.publish_one(lambda:events.append('save'),live=True)
        self.assertEqual(events,['save','send','save'])
    def test_failed_history_save_prevents_public_post(self):
        identity=self.add('A freshly collected news update that is ready to be posted to our account.')
        self.ready(identity)
        with patch('automation.authorization'),patch('automation.send') as send:
            with self.assertRaises(RuntimeError):
                prepared.publish_one(lambda:(_ for _ in ()).throw(RuntimeError()),live=True)
        send.assert_not_called()
    def test_recent_post_exits_without_any_image_network_or_sleep(self):
        identity=self.add('A freshly collected news update that is ready to be posted to our account.')
        self.ready(identity,media='https://cdn4.cdn-telegram.org/file/photo.jpg')
        with prepared.database() as c:
            c.execute('INSERT INTO publications VALUES(?,?,?,?)',('old',app.now()-30,'posted','99'))
        with patch('prepared.download_media') as media,patch('automation.send') as send,patch('time.sleep') as sleep:
            self.assertIn('not due',prepared.publish_one(lambda:None,live=True)['publishing'])
        media.assert_not_called();send.assert_not_called();sleep.assert_not_called()
    def test_image_failure_falls_back_before_any_public_attempt(self):
        identity=self.add('A freshly collected news update that is ready to be posted to our account.')
        self.ready(identity,media='https://cdn4.cdn-telegram.org/file/photo.jpg')
        with patch('prepared.download_media',side_effect=TimeoutError),patch('automation.authorization'),patch('automation.send',return_value='12') as send:
            report=prepared.publish_one(lambda:None,live=True)
        self.assertEqual(report['format'],'text')
        self.assertEqual(send.call_count,1)
    def test_media_hosts_restricted(self):
        for url in ('http://127.0.0.1/img','https://cdn-telegram.org.evil.example/img','https://cdn4.telesco.pe.evil.example/img','file:///tmp/image','https://user:pass@cdn-telegram.org/img'):
            self.assertFalse(prepared.media_allowed(url))
        self.assertTrue(prepared.media_allowed('https://cdn4.cdn-telegram.org/file/photo.jpg'))
        self.assertTrue(prepared.media_allowed('https://cdn4.telesco.pe/file/photo.jpg'))
    def test_uncertain_send_is_never_retried(self):
        identity=self.add('A freshly collected news update that is ready to be posted to our account.')
        self.ready(identity)
        with patch('automation.authorization'),patch('automation.send',side_effect=TimeoutError) as send:
            prepared.publish_one(lambda:None,live=True)
            prepared.publish_one(lambda:None,live=True)
        self.assertEqual(send.call_count,1)

class AutomaticSchedulerTests(FixtureCase):
    def test_editorial_failure_publishes_saved_queue_and_reports_warning(self):
        identity=self.add('A saved and verified story with sufficient context and detail for publishing.')
        self.ready(identity,'Saved draft survives editor failure')
        with patch('newsroom.collect',return_value={'source':{'added':0}}),patch('postcraft.prepare',side_effect=RuntimeError),patch('automation.authorization'),patch('automation.send',return_value='123') as send:
            result=queue_worker.run(Mock(),mode='auto',live=True)
        self.assertTrue(result['ok']);self.assertTrue(result['preparation']['reused_saved_queue'])
        self.assertEqual(result['component_errors'][0]['phase'],'preparation')
        send.assert_called_once_with('Saved draft survives editor failure')
    def test_collector_failure_does_not_prevent_publishing_existing_draft(self):
        identity=self.add('A saved and verified story with sufficient context and detail for publishing.')
        self.ready(identity,'Saved draft survives collector failure')
        with patch('newsroom.collect',side_effect=RuntimeError),patch('postcraft.prepare',return_value={'prepared':1}),patch('automation.authorization'),patch('automation.send',return_value='123') as send:
            result=queue_worker.run(Mock(),mode='auto',live=True)
        self.assertTrue(result['ok']);self.assertEqual(result['component_errors'][0]['phase'],'collection')
        send.assert_called_once_with('Saved draft survives collector failure')
    def test_database_failure_is_not_bypassed_to_publish_without_reliable_history(self):
        import sqlite3
        with patch('newsroom.collect',side_effect=sqlite3.DatabaseError),patch('automation.send') as send:
            with self.assertRaises(sqlite3.DatabaseError):queue_worker.run(Mock(),mode='auto',live=True)
        send.assert_not_called()
    def test_old_queue_version_forces_refresh_before_more_publishing(self):
        now=app.now()
        newsroom.state_set('automatic_collection',dict(bucket=int(now//7200),attempted=now,ok=True,preparation_version=1))
        self.assertEqual(queue_worker.automatic_mode()[0],'collect')
    def test_one_day_has_twelve_collections_and_276_publishing_ticks(self):
        start=1792195200//7200*7200
        modes=[]
        for tick in range(288):
            now=start+tick*300
            with patch('app.now',return_value=now):
                mode,bucket=queue_worker.automatic_mode()
                modes.append(mode)
                if mode=='collect':
                    newsroom.state_set('automatic_collection',dict(bucket=bucket,attempted=now,ok=True,preparation_version=queue_worker.PREPARATION_VERSION))
        self.assertEqual(modes.count('collect'),12)
        self.assertEqual(modes.count('publish'),276)
    def test_late_first_tick_collects_then_later_ticks_only_publish(self):
        now=1792195200//7200*7200+420
        with patch('app.now',return_value=now):
            self.assertEqual(queue_worker.automatic_mode()[0],'collect')
            newsroom.state_set('automatic_collection',dict(bucket=now//7200,attempted=now,ok=True,preparation_version=queue_worker.PREPARATION_VERSION))
        with patch('app.now',return_value=now+300),patch('newsroom.collect') as collect,patch('postcraft.prepare') as prepare,patch('time.sleep') as sleep,patch('prepared.publish_one',return_value={'publishing':'preview'}) as publish:
            result=queue_worker.run(Mock(),mode='auto')
        self.assertEqual(result['automatic_mode'],'publish')
        collect.assert_not_called();prepare.assert_not_called();sleep.assert_not_called();publish.assert_called_once()
    def test_source_outage_uses_bounded_retry_backoff(self):
        now=1792195200//7200*7200+60
        newsroom.state_set('automatic_collection',dict(bucket=now//7200,attempted=now,ok=False,preparation_version=queue_worker.PREPARATION_VERSION))
        with patch('app.now',return_value=now+300):
            self.assertEqual(queue_worker.automatic_mode()[0],'publish')
        with patch('app.now',return_value=now+1800):
            self.assertEqual(queue_worker.automatic_mode()[0],'collect')
    def test_successful_collection_marker_is_saved_before_posting(self):
        state=Mock()
        def checkpoint():
            self.assertTrue(newsroom.state_get('automatic_collection')['ok'])
        state.checkpoint.side_effect=checkpoint
        with patch('newsroom.collect',return_value={'source':{'added':1}}),patch('postcraft.prepare',return_value={'prepared':1}),patch('prepared.publish_one',return_value={'publishing':'preview'}):
            result=queue_worker.run(state,mode='auto')
        self.assertEqual(result['automatic_mode'],'collect')
        self.assertEqual(queue_worker.automatic_mode()[0],'publish')
        state.checkpoint.assert_called_once()


class ScheduleTests(unittest.TestCase):
    def test_collection_and_publish_slots_partition_five_minute_day(self):
        root=app.ROOT/'.github/workflows'
        def slots(file):
            result=set()
            for expr in re.findall(r"cron: '([^']+)'",(root/file).read_text()):
                minute,hour,*_=expr.split()
                mins=[int(v) for v in minute.split(',')]
                if hour=='*':hours=range(24)
                elif hour=='*/2':hours=range(0,24,2)
                elif hour=='1-23/2':hours=range(1,24,2)
                else:raise AssertionError('Unexpected schedule')
                result.update(h*60+m for h in hours for m in mins)
            return result
        collection,publishing=slots('queue.yml'),slots('publish.yml')
        self.assertEqual(len(collection),12)
        self.assertEqual(len(publishing),276)
        self.assertFalse(collection & publishing)
        all_slots=sorted(collection|publishing)
        self.assertEqual(set((all_slots[(i+1)%288]-v)%1440 for i,v in enumerate(all_slots)),{5})


class AdditionalRegressions(FixtureCase):
    def test_video_is_not_misclassified_as_its_photo_thumbnail(self):
        page=b'''<div class="tgme_widget_message" data-post="ClashReport/123"><a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/poster.jpg')"></a><div class="tgme_widget_message_video_wrap"><video poster="https://cdn4.telesco.pe/poster.jpg"><source src="https://cdn4.telesco.pe/movie.mp4"></video></div><div class="tgme_widget_message_text">A substantial news report with a video and sufficient detail.</div><time datetime="2026-10-17T10:00:00Z"></time></div>'''
        with patch('app.fetch',return_value=page):
            row=newsroom.telegram('ClashReport',pages=1,message_id='123')[0]
        self.assertEqual(row['attachment_kind'],'video')
        self.assertEqual(row['attachment_url'],'https://cdn4.telesco.pe/movie.mp4')
        self.assertIsNone(row['media_url'])
    def test_video_metadata_replaces_legacy_image_reference(self):
        body='A substantial news report with a video and sufficient detail for everyone.'
        row=dict(title=body,url='https://t.me/ClashReport/123',source='Telegram @ClashReport',published=app.now(),media_url='https://cdn4.telesco.pe/poster.jpg')
        newsroom.enqueue([row],1.8,[])
        row.update(media_url=None,attachment_kind='video',attachment_url='https://cdn4.telesco.pe/movie.mp4')
        newsroom.enqueue([row],1.8,[])
        postcraft.prepare()
        self.assertEqual(prepared.next_post()['format'],'text_video')
        self.assertEqual(prepared.next_post()['media_url'],'https://cdn4.telesco.pe/movie.mp4')
        self.assertEqual(newsroom.database().execute('SELECT COUNT(*) FROM story_media').fetchone()[0],0)
    def test_profile_cache_cannot_authorize_a_different_identity(self):
        with prepared.database() as c:
            c.execute('INSERT INTO entity_cache VALUES(?,?,?)',('replit',json.dumps({'valid':True,'names':['Replit']}),app.now()+3600))
        registry=Path(self.tmp.name)/'entities.json'
        registry.write_text(json.dumps({'entities':[dict(names=['Someone Else'],handle='Replit',profile_names=['Someone Else'])],'roles':[]}))
        with patch('postcraft.REGISTRY',registry):
            self.assertEqual(postcraft.resolve_entities(['Someone Else']),[])
    def test_financial_signs_and_currency_survive_cleanup(self):
        self.assertEqual(newsroom.clean_text('$5 billion was raised for a new project.'),'$5 billion was raised for a new project.')
        self.assertEqual(newsroom.clean_text('-20% was the recorded change in the index.'),'-20% was the recorded change in the index.')
    def test_explicit_dated_event_is_not_published_after_deadline(self):
        published=app.now()
        date=app.dt.datetime.fromtimestamp(published+3600,app.UTC).strftime('%Y-%m-%dT%H:%MZ')
        deadline=postcraft.lifetime(dict(body='The event starts at '+date,published=published))[0]
        self.assertLessEqual(deadline,published+3600)
    def test_source_image_extraction(self):
        page=b'''<div class="tgme_widget_message" data-post="ClashReport/123"><a class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.cdn-telegram.org/file/image.jpg')"></a><div class="tgme_widget_message_text">A substantial news report with a photo and sufficient detail.</div><time datetime="2026-10-17T10:00:00Z"></time></div>'''
        with patch('app.fetch',return_value=page):
            row=newsroom.telegram('ClashReport',pages=1)[0]
        self.assertEqual(row['media_url'],'https://cdn4.cdn-telegram.org/file/image.jpg')
    def test_internal_x_link_does_not_receive_outbound_penalty(self):
        self.add('An important research announcement has additional context in this post https://x.com/example/status/123',1)
        report=postcraft.prepare()
        self.assertEqual(report['top5'][0]['format'],'text')
        self.assertIn('https://x.com/example/status/123',report['top5'][0]['text'])
    def test_known_image_is_passed_to_tweetkit(self):
        identity=self.add('A freshly collected news update that is ready to be posted to our account.')
        self.ready(identity,media='https://cdn4.cdn-telegram.org/file/photo.jpg')
        with patch('automation.authorization'),patch('prepared.download_media',return_value=Path('/tmp/test-image.jpg')),patch('automation.send',return_value='123') as send:
            result=prepared.publish_one(lambda:None,live=True)
        self.assertTrue(result['image_uploaded'])
        self.assertEqual(send.call_args.kwargs['image_path'],Path('/tmp/test-image.jpg'))

class SelectedPostTests(FixtureCase):
    url='https://t.me/ClashReport/101477'
    def source_row(self,kind='none',attachment=None):
        return dict(title="Canada's Carney:\nIf they're ready to move, we'll move. We're not playing clever here.\nLook, life's too short to play around other countries' political timing.\nWe're ready to do a deal if the U.S. is going to negotiate in good faith.",
                    url=self.url,source='Telegram @ClashReport',published=app.now(),views=100,
                    media_url=None,attachment_kind=kind,attachment_url=attachment)
    def client(self):
        client=Mock(spec=TweetKit)
        client.user_profile.return_value=dict(ok=True,id='123',username='MarkJCarney',name='Mark Carney')
        return client
    def test_explicit_selection_sends_requested_quote_not_queue_head(self):
        wrong=self.add('A completely different queued story that must not be sent during this test.',source='Other Source')
        self.ready(wrong,'Wrong queue head')
        state=Mock()
        with patch('newsroom.telegram',return_value=[self.source_row()]) as source,patch('automation.authorization',return_value=self.client()),patch('automation.send',return_value='999') as send:
            report=selected_post.run(state,self.url,live=True)
        self.assertEqual(report['publication']['post_id'],'999')
        source.assert_called_once_with('ClashReport',pages=1,message_id='101477')
        text=send.call_args.args[0]
        self.assertTrue(text.startswith("@MarkJCarney:\n\nIf they're ready to move"))
        self.assertIn('\n\nLook,',text)
        self.assertEqual(prepared.next_post()['text'],'Wrong queue head')
        self.assertEqual(prepared.database().execute('SELECT COUNT(*) FROM publications').fetchone()[0],1)
    def test_preview_does_not_send_or_save_remote_state(self):
        state=Mock()
        with patch('newsroom.telegram',return_value=[self.source_row()]),patch('automation.authorization',return_value=self.client()),patch('automation.send') as send:
            report=selected_post.run(state,self.url)
        self.assertEqual(report['publication']['publishing'],'preview')
        send.assert_not_called();state.checkpoint.assert_not_called()
    def test_failed_identity_verification_does_not_send(self):
        client=self.client();client.user_profile.side_effect=TimeoutError
        with patch('newsroom.telegram',return_value=[self.source_row()]),patch('automation.authorization',return_value=client),patch('automation.send') as send:
            report=selected_post.run(Mock(),self.url,live=True)
        self.assertFalse(report['ok']);send.assert_not_called()
    def test_missing_video_does_not_send_its_poster_or_text_only(self):
        with patch('newsroom.telegram',return_value=[self.source_row('video')]),patch('automation.authorization',return_value=self.client()),patch('automation.send') as send:
            report=selected_post.run(Mock(),self.url,live=True)
        self.assertFalse(report['ok']);send.assert_not_called()
        self.assertEqual(report['attachment_kind'],'video')
    def test_already_posted_selection_cannot_be_repeated(self):
        newsroom.enqueue([self.source_row()],1.8,[])
        with newsroom.database() as c:
            identity=c.execute('SELECT story_id FROM observations WHERE url=?',(self.url,)).fetchone()[0]
            c.execute('INSERT INTO publications VALUES(?,?,?,?)',(identity,app.now(),'posted','123'))
        with patch('newsroom.telegram') as source,patch('automation.send') as send:
            report=selected_post.run(Mock(),self.url,live=True)
        self.assertTrue(report['ok']);source.assert_not_called();send.assert_not_called()
        self.assertEqual(report['post_id'],'123')
        with newsroom.database() as c:
            c.execute('UPDATE publications SET state="uncertain",post_id=NULL WHERE story_id=?',(identity,))
        with patch('newsroom.telegram') as source,patch('automation.send') as send:
            report=selected_post.run(Mock(),self.url,live=True)
        self.assertFalse(report['ok']);source.assert_not_called();send.assert_not_called()
    def test_unconfigured_sources_rejected_before_network(self):
        with patch('newsroom.telegram') as source:
            with self.assertRaises(ValueError):
                selected_post.run(Mock(),'https://t.me/NotConfigured/123',live=True)
        source.assert_not_called()


class DiscoveryTests(FixtureCase):
    def test_role_extraction_keeps_complete_names_and_prioritises_people(self):
        names=postcraft.entity_candidates(['Western Ukraine has new developments.',
            'Slovak Prime Minister Robert Fico says a decision is needed.',
            'ECB President Christine Lagarde announced new guidance.'])
        self.assertEqual(names[:2],['Robert Fico','Christine Lagarde'])
        self.assertNotIn('Slovak Prime Minister Robert',names)
        self.assertNotIn('ECB President Christine Lagarde',names)
    def test_companies_inside_sentences_and_full_institutions_are_candidates(self):
        names=postcraft.entity_candidates(['The startup ByteDance announced an update.',
                                          'European Central Bank will publish new guidance.'])
        self.assertIn('ByteDance',names)
        self.assertIn('European Central Bank',names)
    def test_structured_native_language_label_can_validate_display_name(self):
        def fetch(url):
            if 'wbsearchentities' in url:
                return json.dumps({'search':[dict(id='Q123',label='Ada Example')]}).encode()
            return json.dumps({'entities':{'Q123':{'labels':{'en':{'value':'Ada Example'},'fa':{'value':'نام نمونه'}},'claims':{'P2002':[{'mainsnak':{'datavalue':{'value':'AdaExample'}}}]}}}}).encode()
        with patch('app.fetch',side_effect=fetch):
            found=postcraft.discover_entities(['President Ada Example said the plan was ready.'])
        client=Mock(spec=TweetKit)
        client.user_profile.return_value=dict(ok=True,id='123',username='AdaExample',name='نام نمونه')
        self.assertEqual(postcraft.resolve_entities(['Ada Example'],found,client)[0]['handle'],'AdaExample')
    def wikidata_fetch(self,url):
        if 'wbsearchentities' in url:
            return json.dumps({'search':[{'id':'Q123','label':'Ada Example'}]}).encode()
        return json.dumps({'entities':{'Q123':{'labels':{'en':{'value':'Ada Example'}},'claims':{'P2002':[{'rank':'normal','mainsnak':{'datavalue':{'value':'AdaExample'}}}]}}}}).encode()
    def test_structured_discovery_still_requires_profile_validation(self):
        with patch('app.fetch',side_effect=self.wikidata_fetch):
            discovered=postcraft.discover_entities(['Ada Example announced a new research project.'])
        self.assertEqual(discovered[0]['handle'],'AdaExample')
        client=Mock(spec=TweetKit);client.user_profile.return_value=dict(ok=True,id='123',username='AdaExample',name='Ada Example')
        entities=postcraft.resolve_entities(['Ada Example'],discovered,client)
        self.assertEqual(postcraft.replace_mentions('Ada Example announced a project.',entities),'@AdaExample announced a project.')
    def test_ambiguous_search_is_not_used(self):
        result={'search':[dict(id='Q1',label='Ada Example'),dict(id='Q2',label='Ada Example')]}
        with patch('app.fetch',return_value=json.dumps(result).encode()) as fetch:
            self.assertEqual(postcraft.discover_entities(['Ada Example announced a project.']),[])
        self.assertEqual(fetch.call_count,1)
    def test_unknown_identity_result_is_cached(self):
        with patch('app.fetch',return_value=b'{"search": []}') as fetch:
            postcraft.discover_entities(['Ada Example announced a project.'])
            postcraft.discover_entities(['Ada Example announced a project.'])
        self.assertEqual(fetch.call_count,1)


if __name__=='__main__':
    unittest.main()
