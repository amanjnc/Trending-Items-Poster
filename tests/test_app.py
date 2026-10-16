import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch
import app

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev = app.DB
        app.DB = Path(self.tmp.name) / 'test.db'
    def tearDown(self):
        app.DB = self.prev
        self.tmp.cleanup()
    def story(self, **kw):
        return dict(title='A science discovery', url='https://example.com/news?utm_source=rss',
                    source='Test', published=app.now(), **kw)
    def test_dedup_and_review_survive_collection(self):
        self.assertEqual(app.ingest([self.story()]), 1)
        s = app.stories()[0]
        app.approve(s['id'], 'approved')
        self.assertEqual(app.ingest([self.story()]), 0)
        self.assertEqual(app.stories()[0]['status'], 'approved')
        self.assertIn('https://example.com/news', app.export())
    def test_reject_stale_future_and_invalid_link(self):
        s = self.story()
        for changes in ({'published': app.now()-49*3600}, {'published': app.now()+7200}, {'url': 'javascript:alert(1)'}):
            self.assertEqual(app.ingest([s | changes]), 0)
    def test_partial_collection_records_failure(self):
        with patch.dict(app.SOURCES, {'good': lambda: [self.story()], 'bad': lambda: (_ for _ in ()).throw(ValueError())}):
            r = app.collect(['good', 'bad'])
        self.assertEqual(r['good']['added'], 1)
        self.assertEqual(r['bad']['error'], 'ValueError')
    def test_hn_adapter(self):
        def response(url):
            return json.dumps([123] if 'topstories' in url else dict(title='News', url='https://example.com', time=app.now(), type='story', score=8)).encode()
        with patch('app.fetch', side_effect=response):
            self.assertEqual(app.hn()[0]['engagement'], 8)
    def test_trends_adapter(self):
        xml = b'''<rss xmlns:ht="https://trends.google.com/trending/rss"><channel><item><title>Topic</title><pubDate>Fri, 16 Oct 2026 10:00:00 GMT</pubDate><ht:news_item><ht:news_item_title>Headline</ht:news_item_title><ht:news_item_url>https://example.com/article</ht:news_item_url></ht:news_item></item></channel></rss>'''
        with patch('app.fetch', return_value=xml):
            rows = app.trends()
        self.assertEqual(rows[0]['title'], 'Headline')
        self.assertEqual(rows[0]['url'], 'https://example.com/article')
    def test_gdelt_adapter(self):
        data = {'articles': [{'title': 'News', 'url': 'https://example.com/a', 'seendate': '20261016T100000Z', 'domain': 'example.com'}]}
        with patch('app.fetch', return_value=json.dumps(data).encode()):
            rows = app.gdelt()
        self.assertEqual(rows[0]['source'], 'GDELT / example.com')
        self.assertGreater(rows[0]['published'], 0)
    def test_http_dashboard_and_health(self):
        app.ingest([self.story() | {'title': '<script>alert(1)</script>'}])
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            with urllib.request.urlopen(base+'/health') as r:
                self.assertEqual(json.load(r)['stories'], 1)
            with urllib.request.urlopen(base) as r:
                page = r.read().decode()
                self.assertIn('&lt;script&gt;', page)
                self.assertNotIn('<script>', page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__ == '__main__':
    unittest.main()
