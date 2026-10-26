"""Source choisie imposee aux requetes HTTP, sans promesse d'installation Xbox."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

import test_xbox
import xbox_app
import xbox_notifications


@contextmanager
def local_source():
    requests = []

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond(send_body=True)

        def do_HEAD(self):
            self.respond(send_body=False)

        def respond(self, send_body):
            requests.append((self.command, self.path, self.headers.get('Range')))
            if self.path == '/error':
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */10')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return

            data = b'0123456789'
            partial = self.command == 'GET' and self.headers.get('Range') == 'bytes=2-5'
            self.send_response(206 if partial else 200)
            if partial:
                data = b'2345'
                self.send_header('Content-Range', 'bytes 2-5/10')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            if send_body:
                self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Source)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        worker.join(2)
        server.server_close()


class _HTTPFixture(unittest.TestCase):
    force_source = True
    request = test_xbox.HTTP.request
    shutdown = test_xbox.HTTP.shutdown

    def setUp(self):
        server = xbox_notifications.make_server_class(
            lambda *args: None, force_source=self.force_source,
            source_provider=lambda: (self.a.runtime, self.a.paused)
        )
        server_patch = patch.object(xbox_app, 'ThreadingHTTPServer', server)
        server_patch.start()
        self.addCleanup(server_patch.stop)
        test_xbox.HTTP.setUp(self)


class ForcedSourceTests(_HTTPFixture):
    def test_link_is_proxied_without_302(self):
        with local_source() as (source, requests):
            self.a.runtime.update(mode='redirect', url=source + '/chosen.xvc')
            status, headers, body = self.request(path='/unrelated-request.xvc')
            self.assertEqual((status, body), (200, b'0123456789'))
            self.assertNotIn('Location', headers)
            self.assertEqual(requests, [('GET', '/chosen.xvc', None)])

    def test_range_is_preserved_for_selected_link(self):
        with local_source() as (source, requests):
            self.a.runtime.update(mode='redirect', url=source + '/chosen.xvc')
            status, headers, body = self.request(headers={'Range': 'bytes=2-5'})
            self.assertEqual((status, body), (206, b'2345'))
            self.assertEqual(headers['Content-Range'], 'bytes 2-5/10')
            self.assertNotIn('Location', headers)
            self.assertEqual(requests, [('GET', '/chosen.xvc', 'bytes=2-5')])

    def test_head_has_no_body_and_uses_selected_link(self):
        with local_source() as (source, requests):
            self.a.runtime.update(mode='redirect', url=source + '/chosen.xvc')
            status, headers, body = self.request('HEAD')
            self.assertEqual((status, body), (200, b''))
            self.assertEqual(headers['Content-Length'], '10')
            self.assertEqual(requests, [('HEAD', '/chosen.xvc', None)])

    def test_single_local_file_answers_arbitrary_request_path(self):
        status, headers, body = self.request(path='/not-the-selected-name.bin?query=1')
        self.assertEqual((status, body), (200, b'0123456789'))
        self.assertNotIn('Location', headers)

    def test_upstream_error_is_preserved_without_fallback(self):
        with local_source() as (source, requests):
            self.a.runtime.update(mode='redirect', url=source + '/error')
            status, headers, body = self.request(path='/another-original.xvc')
            self.assertEqual((status, body), (416, b''))
            self.assertEqual(headers['Content-Range'], 'bytes */10')
            self.assertNotIn('Location', headers)
            self.assertEqual(requests, [('GET', '/error', None)])

    def test_manual_message_stays_html_and_does_not_fetch_source(self):
        with local_source() as (source, requests):
            self.a.runtime.update(mode='redirect', url=source + '/chosen.xvc')
            status, headers, body = self.request(path='/message')
            self.assertEqual(status, 200)
            self.assertIn('text/html', headers['Content-Type'])
            self.assertIn(b'non non leo interdit', body)
            self.assertEqual(requests, [])
            self.assertEqual(self.request('HEAD', path='/message')[2], b'')
            self.assertEqual(requests, [])

    def test_pause_still_blocks_transfers(self):
        with local_source() as (source, requests):
            self.a.runtime.update(mode='redirect', url=source + '/chosen.xvc')
            self.a.paused = True
            status, headers, body = self.request()
            self.assertEqual(status, 503)
            self.assertEqual(requests, [])


class OptionalForcedSourceTests(_HTTPFixture):
    force_source = False

    def test_disabled_preserves_original_redirect(self):
        self.a.runtime.update(mode='redirect', url='https://example.org/chosen.xvc')
        status, headers, body = self.request()
        self.assertEqual((status, body), (302, b''))
        self.assertEqual(headers['Location'], 'https://example.org/chosen.xvc')


if __name__ == '__main__':
    unittest.main()
