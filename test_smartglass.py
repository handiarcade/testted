"""Tests locaux et API simulee ; aucun compte Microsoft reel n'est utilise."""
import asyncio
import http.client
import json
import os
from pathlib import Path
import queue
import socket
import stat
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import xbox_smartglass as sg


def response(**kwargs):
    return NS(status=NS(error_code="OK"), **kwargs)


class FakeOAuth:
    def model_dump_json(self):
        return json.dumps({"test_token": "local-test-only"})

    @classmethod
    def model_validate_json(cls, text):
        json.loads(text)
        return cls()


class BackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'data' / 'tokens.json'
        self.client = sg.SmartglassClient(self.path)
        self.client._save_tokens(FakeOAuth())
        self.provider = NS(
            get_console_list=AsyncMock(return_value=response(result=[])),
            get_installed_apps=AsyncMock(return_value=response(result=[])),
            launch_app=AsyncMock(return_value=response(op_id='operation-1')),
            get_op_status=AsyncMock(return_value=response(op_status_list=[])),
        )
        self.sessions = []
        self.managers = []
        owner = self
        class Session:
            def __init__(self):
                self.closed = False
                owner.sessions.append(self)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                self.closed = True
        def manager(session, client_id, secret, redirect):
            value = NS(oauth=None, refresh_tokens=AsyncMock())
            owner.managers.append(value)
            return value
        self.lib = NS(SignedSession=Session, AuthenticationManager=manager,
            OAuth2TokenResponse=FakeOAuth, XboxLiveClient=lambda manager: NS(smartglass=self.provider),
            CLIENT_ID='test-public-client', CLIENT_SECRET='', REDIRECT_URI='http://localhost:8080/auth/callback')
        self.patcher = patch.object(sg, '_library', return_value=self.lib)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_console_and_apps_use_actual_identifiers(self):
        self.provider.get_console_list.return_value = response(result=[NS(
            id='console-42', name='Salon', remote_management_enabled=True, power_state='On')])
        consoles = await self.client.list_consoles()
        self.assertEqual(consoles, [dict(id='console-42', name='Salon', remote_enabled=True, power_state='On')])
        self.provider.get_installed_apps.return_value = response(result=[
            NS(one_store_product_id='PRODUCT42', name='Application'),
            NS(one_store_product_id=None, name='Non lancable'),
        ])
        self.assertEqual(await self.client.list_apps('console-42'), [dict(id='PRODUCT42', name='Application')])
        self.provider.get_installed_apps.assert_awaited_once_with('console-42')
        self.assertEqual(len(self.sessions), 2)
        self.assertTrue(all(session.closed for session in self.sessions))
        self.assertTrue(all(manager.refresh_tokens.await_count == 1 for manager in self.managers))

    async def test_empty_http_success_with_api_error_is_not_success(self):
        self.provider.get_console_list.return_value = NS(status=NS(error_code='RemoteManagementDisabled'), result=[])
        with self.assertRaisesRegex(sg.SmartglassError, 'RemoteManagementDisabled'):
            await self.client.list_consoles()
        self.assertTrue(self.sessions[0].closed)

    async def test_launch_waits_for_confirmed_operation(self):
        self.provider.get_op_status.side_effect = [
            response(op_status_list=[NS(op_id='operation-1', operation_status='Pending', succeeded=False)]),
            response(op_status_list=[NS(op_id='operation-1', operation_status='Succeeded', succeeded=True)]),
        ]
        self.client.POLL_INTERVAL = 0
        self.assertEqual(await self.client.launch('console-42', 'PRODUCT42'),
                         dict(status='succeeded', operation_id='operation-1'))
        self.provider.launch_app.assert_awaited_once_with('console-42', 'PRODUCT42')
        self.assertEqual(self.provider.get_op_status.await_count, 2)

    async def test_launch_pending_is_not_reported_as_success(self):
        self.client.OPERATION_TIMEOUT = 0.025
        self.client.POLL_INTERVAL = 0.01
        result = await self.client.launch('console-42', 'PRODUCT42')
        self.assertEqual(result['status'], 'pending')

    async def test_console_rejection_is_failure(self):
        self.provider.get_op_status.return_value = response(op_status_list=[NS(
            op_id='operation-1', operation_status='OffConsoleError', succeeded=False)])
        with self.assertRaisesRegex(sg.SmartglassError, 'OffConsoleError'):
            await self.client.launch('console-42', 'PRODUCT42')

    async def test_wrong_operation_cannot_confirm_launch(self):
        self.provider.get_op_status.return_value = response(op_status_list=[NS(
            op_id='unrelated-operation', operation_status='Succeeded', succeeded=True)])
        self.client.OPERATION_TIMEOUT = 0.02
        self.assertEqual((await self.client.launch('console-42', 'PRODUCT42'))['status'], 'pending')

    async def test_no_saved_login_does_not_silently_authenticate_launch(self):
        self.path.unlink()
        with self.assertRaisesRegex(sg.SmartglassError, 'Connectez'):
            await self.client.launch('console-42', 'PRODUCT42')
        self.provider.launch_app.assert_not_awaited()

    async def test_initial_login_is_saved(self):
        self.path.unlink()
        async def login(manager, callback):
            manager.oauth = FakeOAuth()
        with patch.object(self.client, '_interactive_login', side_effect=login) as interactive:
            await self.client.list_consoles(authenticate=True)
            interactive.assert_awaited_once()
        self.assertTrue(self.path.exists())

    async def test_network_error_is_sanitized(self):
        self.provider.get_console_list.side_effect = RuntimeError('secret-from-untrusted-url')
        with self.assertRaises(sg.SmartglassError) as caught:
            await self.client.list_consoles()
        self.assertNotIn('secret-from-untrusted-url', str(caught.exception))

    async def test_corrupt_tokens_need_reconnect_for_noninteractive_operation(self):
        self.path.write_text('not-json')
        with self.assertRaisesRegex(sg.SmartglassError, 'illisible'):
            await self.client.list_apps('console-42')

    def test_atomic_token_file_and_private_mode(self):
        self.client._save_tokens(FakeOAuth())
        self.assertEqual(json.loads(self.path.read_text()), {'test_token': 'local-test-only'})
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.results = queue.Queue(maxsize=1)
        self.server = sg._CallbackServer(('127.0.0.1', 0), sg._callback_handler('state-42', self.results))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(2)

    def request(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            connection.request('GET', path)
            result = connection.getresponse()
            return result.status, result.read()
        finally:
            connection.close()

    def test_state_is_required_and_code_is_not_echoed(self):
        self.assertEqual(self.request('/auth/callback?state=wrong&code=private-code')[0], 400)
        self.assertTrue(self.results.empty())
        status, body = self.request('/auth/callback?state=state-42&code=private-code')
        self.assertEqual(status, 200)
        self.assertNotIn(b'private-code', body)
        self.assertEqual(self.results.get_nowait(), 'private-code')

    def test_wrong_path_and_duplicate_state_are_rejected(self):
        self.assertEqual(self.request('/wrong?state=state-42&code=code')[0], 400)
        self.assertEqual(self.request('/auth/callback?state=state-42&state=wrong&code=code')[0], 400)
        self.assertTrue(self.results.empty())

    def test_login_refusal_is_signalled_without_server_message(self):
        self.assertEqual(self.request('/auth/callback?state=state-42&error=access_denied&error_description=secret')[0], 400)
        result = self.results.get_nowait()
        self.assertIsInstance(result, sg.SmartglassError)
        self.assertNotIn('secret', str(result))


class AuthLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_callback_server_closes_after_success(self):
        client = sg.SmartglassClient(Path('/tmp/not-used-smartglass-test.json'))
        actual_server = sg._CallbackServer
        servers = []
        def make_server(address, handler):
            self.assertEqual(address, ('127.0.0.1', 8080))
            server = actual_server(('127.0.0.1', 0), handler)
            servers.append(server)
            return server
        def browser(url):
            state = parse_qs(urlsplit(url).query)['state'][0]
            connection = http.client.HTTPConnection('127.0.0.1', servers[0].server_port, timeout=2)
            try:
                connection.request('GET', f'/auth/callback?state={state}&code=test-code')
                result = connection.getresponse(); result.read()
                return result.status == 200
            finally:
                connection.close()
        manager = NS(generate_authorization_url=lambda state: 'https://example.invalid/?state=' + state,
                     request_tokens=AsyncMock())
        urls = []
        with patch.object(sg, '_CallbackServer', side_effect=make_server), patch.object(sg.webbrowser, 'open', side_effect=browser):
            await client._interactive_login(manager, urls.append)
        manager.request_tokens.assert_awaited_once_with('test-code')
        self.assertEqual(len(urls), 1)
        self.assertEqual(servers[0].socket.fileno(), -1)

    async def test_browser_failure_is_immediate_and_closes_callback_server(self):
        client = sg.SmartglassClient(Path('/tmp/not-used-smartglass-test.json'))
        actual_server = sg._CallbackServer
        servers = []
        def make_server(address, handler):
            server = actual_server(('127.0.0.1', 0), handler)
            servers.append(server)
            return server
        manager = NS(generate_authorization_url=lambda state: 'https://example.invalid/?state=' + state,
                     request_tokens=AsyncMock())
        with patch.object(sg, '_CallbackServer', side_effect=make_server), patch.object(sg.webbrowser, 'open', return_value=False):
            with self.assertRaisesRegex(sg.SmartglassError, "ouvrir le navigateur"):
                await asyncio.wait_for(client._interactive_login(manager, None), timeout=3)
        self.assertEqual(servers[0].socket.fileno(), -1)
        manager.request_tokens.assert_not_awaited()

    async def test_timeout_closes_callback_server(self):
        client = sg.SmartglassClient(Path('/tmp/not-used-smartglass-test.json'))
        client.LOGIN_TIMEOUT = 0.02
        actual_server = sg._CallbackServer
        servers = []
        def make_server(address, handler):
            server = actual_server(('127.0.0.1', 0), handler)
            servers.append(server)
            return server
        manager = NS(generate_authorization_url=lambda state: 'https://example.invalid/?state=' + state,
                     request_tokens=AsyncMock())
        with patch.object(sg, '_CallbackServer', side_effect=make_server), patch.object(sg.webbrowser, 'open', return_value=True):
            with self.assertRaisesRegex(sg.SmartglassError, 'expiree'):
                await client._interactive_login(manager, None)
        self.assertEqual(servers[0].socket.fileno(), -1)
        manager.request_tokens.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
