import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
import automation
import newsroom
import postcraft
import prepared
import queue_worker

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
    def add(self,text,number=1,age=0,media=None):
        url=f'https://t.me/ClashReport/{number}'
        identity=str(number)
        with newsroom.database() as c:
            c.execute('INSERT INTO stories(id,title,url,source,published,collected,score) VALUES(?,?,?,?,?,?,?)',(identity,text,url,'Telegram @ClashReport',app.now()-age,app.now(),0))
            c.execute('INSERT INTO news_items VALUES(?,?,?,?,?)',(identity,text,str(number),1.8,0))
            c.execute('INSERT INTO observations VALUES(?,?,?)',(url,identity,'Telegram @ClashReport'))
            if media:
                c.execute('INSERT INTO story_media VALUES(?,?)',(identity,media))
        return identity
    def ready(self,identity,text='Prepared post text',position=0,expires=None,media=None,rank=50):
        with prepared.database() as c:
            c.execute('INSERT OR REPLACE INTO prepared_posts VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,text,media,'https://t.me/ClashReport/'+identity,'text_image' if media else 'text',rank,expires or app.now()+3600,position,'{}',app.now()))

class EntityTests(FixtureCase):
    def profile_client(self):
        client=Mock()
        lookup={'replit':('Replit','Replit'),'anthropicai':('AnthropicAI','Anthropic'),'amasad':('amasad','Amjad Masad'),'markwahlberg':('markwahlberg','Mark Wahlberg')}
        def profile(handle):
            username,name=lookup[handle.lower()]
            return dict(ok=True,id='123',username=username,name=name)
        client.get_user_profile.side_effect=profile
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
        client=Mock();client.get_user_profile.side_effect=RuntimeError('private HTTP details')
        entities=postcraft.resolve_entities(['Replit'],client=client)
        self.assertEqual(entities,[])
        self.assertEqual(postcraft.replace_mentions('Replit announced an update.',entities),'Replit announced an update.')
    def test_wrong_display_identity_is_not_accepted(self):
        client=Mock();client.get_user_profile.return_value=dict(ok=True,id='1',username='Replit',name='Unrelated person')
        self.assertEqual(postcraft.resolve_entities(['Replit'],client=client),[])
    def test_valid_identity_is_cached(self):
        client=self.profile_client()
        first=postcraft.resolve_entities(['Replit'],client=client)
        second=postcraft.resolve_entities(['Replit'],client=client)
        self.assertEqual(first,second)
        self.assertEqual(client.get_user_profile.call_count,1)
    def test_unknown_name_is_not_guessed(self):
        client=self.profile_client()
        self.assertEqual(postcraft.resolve_entities(['Some Unknown Founder'],client=client),[])
        client.get_user_profile.assert_not_called()
    def test_already_tagged_entities_not_double_tagged(self):
        self.assertEqual(postcraft.replace_mentions('@Replit and Replit',[dict(names=['Replit'],handle='Replit')]),'@Replit and @Replit')
    def test_ambiguous_common_nouns_not_tagged(self):
        entities=[dict(names=['Apple'],handle='Apple'),dict(names=['Meta'],handle='Meta')]
        self.assertEqual(postcraft.replace_mentions('Apple pie research and Meta-analysis.',entities),'Apple pie research and Meta-analysis.')

class PreparationTests(FixtureCase):
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
        self.add('A new research report explains battery technology. Read the full report for detailed results.',2)
        report=postcraft.prepare()
        formats={r['format']:r['rank'] for r in report['top5']}
        self.assertGreater(formats['text'],formats['text_link'])
    def test_time_bound_outage_expired_but_evergreen_not(self):
        now=app.now()
        outage=dict(body='A major network outage disrupted services.',published=now-7*3600)
        guide=dict(body='A research guide explaining battery chemistry.',published=now-3*86400)
        self.assertLess(postcraft.lifetime(outage)[0],now)
        self.assertGreater(postcraft.lifetime(guide)[0],now)
    def test_three_day_old_research_can_remain_but_six_day_old_item_cannot(self):
        self.add('A research guide explaining a new approach to energy storage for homes and businesses.',1,age=3*86400)
        self.add('A research guide explaining a different approach to energy storage for homes and businesses.',2,age=6*86400)
        self.assertEqual(postcraft.prepare()['prepared'],1)
        self.assertEqual(prepared.next_post()['story_id'],'1')
    def test_no_item_lives_beyond_five_days(self):
        row=dict(body='An evergreen research guide.',published=app.now())
        self.assertLessEqual(postcraft.lifetime(row)[0],row['published']+5*86400)
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
        for url in ('http://127.0.0.1/img','https://cdn-telegram.org.evil.example/img','file:///tmp/image','https://user:pass@cdn-telegram.org/img'):
            self.assertFalse(prepared.media_allowed(url))
        self.assertTrue(prepared.media_allowed('https://cdn4.cdn-telegram.org/file/photo.jpg'))
    def test_uncertain_send_is_never_retried(self):
        identity=self.add('A freshly collected news update that is ready to be posted to our account.')
        self.ready(identity)
        with patch('automation.authorization'),patch('automation.send',side_effect=TimeoutError) as send:
            prepared.publish_one(lambda:None,live=True)
            prepared.publish_one(lambda:None,live=True)
        self.assertEqual(send.call_count,1)

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

class DiscoveryTests(FixtureCase):
    def wikidata_fetch(self,url):
        if 'wbsearchentities' in url:
            return json.dumps({'search':[{'id':'Q123','label':'Ada Example'}]}).encode()
        return json.dumps({'entities':{'Q123':{'labels':{'en':{'value':'Ada Example'}},'claims':{'P2002':[{'rank':'normal','mainsnak':{'datavalue':{'value':'AdaExample'}}}]}}}}).encode()
    def test_structured_discovery_still_requires_profile_validation(self):
        with patch('app.fetch',side_effect=self.wikidata_fetch):
            discovered=postcraft.discover_entities(['Ada Example announced a new research project.'])
        self.assertEqual(discovered[0]['handle'],'AdaExample')
        client=Mock();client.get_user_profile.return_value=dict(ok=True,id='123',username='AdaExample',name='Ada Example')
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
