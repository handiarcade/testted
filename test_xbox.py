"""Tests reseau locaux ; aucun changement de pare-feu ni acces a Xbox Live."""
import http.client
import queue
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import xbox_app as app
import xbox_dns_spoof as dns


def question(name=b'assets1.xboxlive.com', qtype=1):
    wire = b''.join(bytes([len(x)]) + x for x in name.split(b'.')) + b'\x00'
    return b'\x12\x34\x01\x00\x00\x01' + b'\x00'*6 + wire + qtype.to_bytes(2,'big') + b'\x00\x01'


class DNS(unittest.TestCase):
    def test_matching(self):
        self.assertTrue(dns.matches_domain(b'ASSETS1.XBOXLIVE.COM',dns.XBOX_DOMAINS))
        self.assertTrue(dns.matches_domain(b'cdn.assets1.xboxlive.com',dns.XBOX_DOMAINS))
        self.assertFalse(dns.matches_domain(b'assets1.xboxlive.com.example.org',dns.XBOX_DOMAINS))
        self.assertFalse(dns.matches_domain(b'packages.xboxlive.com',dns.XBOX_DOMAINS))

    def test_a_and_aaaa(self):
        self.assertEqual(dns.build_response(question(), '192.0.2.1')[-4:],socket.inet_aton('192.0.2.1'))
        self.assertEqual(dns.build_response(question(qtype=28), '192.0.2.1')[6:8],b'\x00\x00')

    def test_malformed(self):
        for packet in (b'', b'\x00'*12, question()[:-1], question()[:12]+b'\xc0\x0c\x00\x01\x00\x01'):
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                dns.get_question(packet)

    def test_udp_survives_bad_packet(self):
        ready=queue.Queue(); stop=threading.Event()
        worker=threading.Thread(target=dns.run_dns,kwargs=dict(log=lambda _:None,
            stop_event=stop,local_ip='192.0.2.1',bind_ip='127.0.0.1',port=0,on_ready=ready.put))
        worker.start()
        try:
            port=ready.get(timeout=2)
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as client:
                client.settimeout(2)
                client.sendto(question()[:-1],('127.0.0.1',port))
                client.sendto(question(),('127.0.0.1',port))
                response,_=client.recvfrom(4096)
                self.assertEqual(response[-4:],socket.inet_aton('192.0.2.1'))
        finally:
            stop.set(); worker.join(3)
        self.assertFalse(worker.is_alive())


class HTTP(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'file.xvc'; self.path.write_bytes(b'0123456789')
        self.a=app.XboxRedirectApp.__new__(app.XboxRedirectApp)
        self.a.runtime=dict(mode='serve',url='',local_path=str(self.path))
        self.a.paused=False; self.a.server=None; self.a.http_ready=threading.Event()
        self.a.log_threadsafe=lambda _:None; self.a.console_request=lambda _:None
        with patch.object(app,'HTTP_PORT',0):
            self.worker=threading.Thread(target=self.a.run_server,daemon=True); self.worker.start()
            self.assertTrue(self.a.http_ready.wait(2))
        self.addCleanup(self.shutdown)
        self.port=self.a.server.server_address[1]

    def shutdown(self):
        self.a.server.shutdown(); self.worker.join(2); self.a.server.server_close()

    def request(self,method='GET',headers=None,path='/file.xvc'):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        try:
            conn.request(method,path,headers=headers or {}); r=conn.getresponse()
            return r.status,dict(r.getheaders()),r.read()
        finally:
            conn.close()

    def test_file_and_ranges(self):
        for headers,status,body in [({},200,b'0123456789'),({'Range':'bytes=2-5'},206,b'2345'),
            ({'Range':'bytes=-3'},206,b'789'),({'Range':'bytes=20-'},416,b''),
            ({'Range':'bytes=-0'},416,b''),({'Range':'bytes=0-1,4-5'},200,b'0123456789')]:
            with self.subTest(headers=headers):
                actual,h,data=self.request(headers=headers)
                self.assertEqual((actual,data),(status,body))
        self.assertEqual(self.request('HEAD',{'Range':'bytes=2-5'})[::2],(200,b''))

    def test_empty_file(self):
        self.path.write_bytes(b'')
        self.assertEqual(self.request()[::2],(200,b''))
        self.assertEqual(self.request(headers={'Range':'bytes=0-'})[0],416)

    def test_directory_and_symlink(self):
        self.a.runtime['local_path']=self.tmp.name
        (Path(self.tmp.name)/'file name.xvc').write_bytes(b'ok')
        self.assertEqual(self.request(path='/file%20name.xvc')[2],b'ok')
        with tempfile.TemporaryDirectory() as other:
            outside=Path(other)/'outside'; outside.write_bytes(b'secret')
            try: (Path(self.tmp.name)/'escape.xvc').symlink_to(outside)
            except OSError: self.skipTest('symlinks unavailable')
            self.assertEqual(self.request(path='/escape.xvc')[0],404)

    def test_redirect_and_pause(self):
        self.a.runtime.update(mode='redirect',url='https://example.org/file')
        status,h,body=self.request(); self.assertEqual(status,302)
        self.assertEqual(h['Location'],'https://example.org/file')
        self.a.paused=True; self.assertEqual(self.request()[0],503)

    def test_proxy_framing_range_and_error(self):
        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/error':
                    self.send_response(416); self.send_header('Content-Range','bytes */10')
                    self.end_headers(); return
                if self.headers.get('Range')=='bytes=2-5':
                    self.send_response(206); self.send_header('Content-Length','4')
                    self.send_header('Content-Range','bytes 2-5/10'); self.end_headers()
                    self.wfile.write(b'2345'); return
                self.send_response(200); self.end_headers(); self.wfile.write(b'no-length')
            def log_message(self,*args): pass
        source=ThreadingHTTPServer(('127.0.0.1',0),Source)
        thread=threading.Thread(target=source.serve_forever,daemon=True); thread.start()
        try:
            self.a.runtime.update(mode='proxy',url=f'http://127.0.0.1:{source.server_port}/file')
            status,h,data=self.request(); self.assertEqual((status,data),(200,b'no-length'))
            self.assertEqual(h['Connection'],'close')
            status,h,data=self.request(headers={'Range':'bytes=2-5'})
            self.assertEqual((status,data),(206,b'2345'))
            self.assertEqual(h['Content-Range'],'bytes 2-5/10')
            self.a.runtime['url']=f'http://127.0.0.1:{source.server_port}/error'
            status,h,data=self.request(); self.assertEqual(status,416)
            self.assertEqual(h['Content-Range'],'bytes */10')
        finally:
            source.shutdown(); thread.join(2); source.server_close()


class Firewall(unittest.TestCase):
    def test_cleanup_only_owned_rules(self):
        a=app.XboxRedirectApp.__new__(app.XboxRedirectApp)
        a.firewall_rules=['XboxDownloader-test-TCP-80']; a.log_threadsafe=lambda _:None
        with patch.object(app.subprocess,'run') as run:
            run.return_value.returncode=0; a.cleanup()
            command=run.call_args.args[0]
            self.assertEqual(command[:5],['netsh','advfirewall','firewall','delete','rule'])
            self.assertEqual(command[-1],'name=XboxDownloader-test-TCP-80')
            self.assertEqual(a.firewall_rules,[])

if __name__=='__main__': unittest.main()
