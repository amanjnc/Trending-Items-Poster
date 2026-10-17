import os
import unittest
from unittest.mock import patch
import app
import automation
import test_app

class AutomationTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    story = test_app.AppTests.story
    def test_dry_run_never_posts(self):
        app.ingest([self.story()])
        with patch('app.collect', return_value={}), patch('automation.send') as send:
            self.assertIn('dry run', automation.cycle()['publishing'])
            send.assert_not_called()
    def test_published_story_not_repeated(self):
        app.ingest([self.story()])
        with patch('app.collect', return_value={}), patch('automation.authorization'), patch('automation.send', return_value='123') as send:
            self.assertEqual(automation.cycle(True)['post_id'], '123')
            automation.cycle(True)
            self.assertEqual(send.call_count, 1)
    def test_uncertain_attempt_not_retried(self):
        app.ingest([self.story()])
        with patch('app.collect', return_value={}), patch('automation.authorization'), patch('automation.send', side_effect=TimeoutError) as send:
            self.assertIn('uncertain', automation.cycle(True)['publishing'])
            automation.cycle(True)
            self.assertEqual(send.call_count, 1)
    def test_credential_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Missing credentials'):
                automation.authorization()
    def test_dismissed_and_stale_not_posted(self):
        app.ingest([self.story()])
        app.approve(app.stories()[0]['id'], 'dismissed')
        with patch('app.collect', return_value={}):
            self.assertEqual(automation.cycle()['publishing'], 'no fresh candidates')

class TweetKitAdapterTests(unittest.TestCase):
    def test_upload_failure_never_calls_tweet_creation(self):
        from unittest.mock import Mock
        from tweetkit_x import TweetKit
        client=Mock(spec=TweetKit)
        client.upload_media.side_effect=TimeoutError('private response details')
        with patch('automation.authorization',return_value=client):
            with self.assertRaises(automation.MediaUploadFailed):
                automation.send('A headline',image_path='/tmp/movie.mp4')
        client.create_tweet.assert_not_called();client.post.assert_not_called()
    def test_media_upload_then_creation_use_pinned_sdk_methods(self):
        from unittest.mock import Mock
        from tweetkit_x import TweetKit
        client=Mock(spec=TweetKit)
        client.upload_media.return_value='123'
        client.create_tweet.return_value={'ok':True,'id':'456'}
        with patch('automation.authorization',return_value=client):
            self.assertEqual(automation.send('A headline',image_path='/tmp/movie.mp4'),'456')
        client.upload_media.assert_called_once_with('/tmp/movie.mp4')
        client.create_tweet.assert_called_once_with('A headline',['123'])
    def test_rejection_metadata_does_not_expose_raw_response(self):
        from unittest.mock import Mock
        client=Mock();client.post.return_value={'ok':False,'status':401,'error':'private token information'}
        with patch('automation.authorization',return_value=client):
            with self.assertRaises(automation.PostingRejected) as error:
                automation.send('A headline')
        self.assertEqual(error.exception.category,'account_access')
        self.assertNotIn('private token',str(error.exception))
    def test_adapter_checks_success_without_logging_response(self):
        import sys
        import types
        from unittest.mock import Mock
        client = Mock()
        client.post.return_value = {'ok': True, 'id': '456'}
        fake = types.ModuleType('tweetkit_x')
        fake.TweetKit = Mock(return_value=client)
        with patch.dict(sys.modules, {'tweetkit_x': fake}), patch.dict(os.environ, {'TWEETKIT_COOKIE': 'dummy-test-value'}):
            self.assertEqual(automation.send('A headline'), '456')
            client.post.assert_called_once_with('A headline')
            client.post.return_value = {'ok': False, 'error': 'private response details'}
            with self.assertRaisesRegex(RuntimeError, 'did not confirm') as error:
                automation.send('Another')
            self.assertNotIn('private response', str(error.exception))

if __name__ == '__main__':
    unittest.main()
