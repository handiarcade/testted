import threading
import unittest
from unittest.mock import patch
import http.client
import xbox_app
import xbox_notifications as notifications
import test_xbox


class Notifications(test_xbox.HTTP):
    def setUp(self):
        self.events = []
        self.status = {'event': 0}
        self.event_condition = threading.Condition()
        def record(*event):
            with self.event_condition:
                self.events.append(event)
                self.status = {'event': self.status['event'] + 1}
                self.event_condition.notify_all()
        server_class = notifications.make_server_class(record, status_provider=lambda: self.status)
        self.server_patch = patch.object(xbox_app, 'ThreadingHTTPServer', server_class)
        self.server_patch.start()
        self.addCleanup(self.server_patch.stop)
        super().setUp()

    def wait_events(self, count):
        with self.event_condition:
            self.assertTrue(self.event_condition.wait_for(lambda: len(self.events) >= count, timeout=2))

    def test_transfer_notice_and_exclusions(self):
        self.request('HEAD')
        self.request(headers={'Range': 'bytes=20-'})
        self.assertEqual(self.events, [])
        self.request(headers={'Range': 'bytes=2-5'})
        self.wait_events(1)
        self.assertEqual(self.events, [('127.0.0.1', 'file.xvc', 'fichier local')])

    def test_message_page_and_head(self):
        status, headers, body = self.request(path='/message')
        self.assertEqual(status, 200)
        self.assertIn(b'non non leo interdit', body)
        self.assertIn('text/html', headers['Content-Type'])
        self.assertEqual(self.events, [])
        self.assertEqual(self.request('HEAD', path='/message')[2], b'')
        self.assertEqual(self.events, [])

    def test_status_tracks_real_transfers_only(self):
        import json
        status, headers, body = self.request(path='/status')
        self.assertEqual((status, json.loads(body)), (200, {'event': 0}))
        self.assertEqual(self.events, [])
        self.request('HEAD')
        self.request(headers={'Range': 'bytes=20-'})
        self.assertEqual(json.loads(self.request(path='/status')[2]), {'event': 0})
        self.request()
        self.wait_events(1)
        status, headers, body = self.request(path='/status')
        self.assertEqual(json.loads(body), {'event': 1})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(b'file.xvc', body)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.request('HEAD', path='/status')[2], b'')

    def test_persistent_connection_no_false_notice(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            for target in ('/file.xvc', '/message', '/file.xvc'):
                connection.request('GET', target)
                response = connection.getresponse()
                response.read()
            self.wait_events(2)
            self.assertEqual(len(self.events), 2)
        finally:
            connection.close()

    def test_proxy_download_notice(self):
        super().test_proxy_framing_range_and_error()
        self.wait_events(2)
        self.assertEqual(len(self.events), 2)
        self.assertTrue(all(event[2] == 'proxy' for event in self.events))


if __name__ == '__main__':
    unittest.main()
