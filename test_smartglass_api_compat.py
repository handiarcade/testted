"""Compatibilite des listes d'apps : vrai fournisseur OpenXbox, transport HTTP simule."""
import json
from pathlib import Path
import ssl
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError
from xbox.webapi.api.provider.smartglass import SmartglassProvider
from xbox.webapi.api.provider.smartglass.models import InstalledPackagesList
from xbox.webapi.authentication.models import OAuth2TokenResponse
from xbox.webapi.common.exceptions import AuthenticationException

import xbox_smartglass as sg


PRODUCT_ID = '9NBLGGH4R315'
PRIVATE_VALUE = 'PRIVATE_TEST_VALUE_MUST_NOT_APPEAR'


def minimal_payload():
    return {'status': {'errorCode': 'OK'}, 'result': [
        {'oneStoreProductId': PRODUCT_ID, 'name': 'Application test',
         'version': None, 'sizeInBytes': None, 'installTime': None}
    ]}


def complete_payload():
    return {'status': {'errorCode': 'OK'}, 'result': [{
        'oneStoreProductId': PRODUCT_ID, 'name': 'Application test',
        'titleId': 123, 'isGame': False, 'contentType': 'Application',
        'instanceId': 'instance-1', 'storageDeviceId': 'storage-1',
        'uniqueId': 'unique-1', 'version': 1, 'sizeInBytes': 1234,
        'installTime': '2026-01-01T00:00:00Z',
    }]}


class FakeOAuth:
    def model_dump_json(self):
        return '{"fixture":true}'

    @classmethod
    def model_validate_json(cls, value):
        json.loads(value)
        return cls()


class InstalledAppsCompatibility(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = sg.SmartglassClient(Path(self.directory.name) / 'tokens.json')
        self.client._save_tokens(FakeOAuth())
        self.requests = []
        self.documents = [minimal_payload()]
        self.status_code = 200

        def handle(request):
            index = len(self.requests)
            self.requests.append(request)
            document = self.documents[min(index, len(self.documents) - 1)]
            if isinstance(document, bytes):
                return httpx.Response(self.status_code, content=document, request=request)
            return httpx.Response(self.status_code, json=document, request=request)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(self.http.aclose)
        self.provider = SmartglassProvider(NS(session=self.http))

        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        def manager(*args):
            return NS(oauth=None, refresh_tokens=AsyncMock())

        library = NS(SignedSession=Session, AuthenticationManager=manager,
            OAuth2TokenResponse=FakeOAuth,
            XboxLiveClient=lambda manager: NS(smartglass=self.provider),
            CLIENT_ID='public-test-client', CLIENT_SECRET='', REDIRECT_URI='http://localhost:8080/auth/callback')
        self.patch_library = patch.object(sg, '_library', return_value=library)
        self.patch_library.start()
        self.addCleanup(self.patch_library.stop)

    async def test_nullable_or_missing_sdk_metadata_does_not_block_launchable_ids(self):
        with self.assertRaises(ValidationError):
            InstalledPackagesList.model_validate(self.documents[0])
        self.assertEqual(await self.client.list_apps('console-1'),
                         [{'id': PRODUCT_ID, 'name': 'Application test'}])
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertEqual(request.url.path, '/lists/installedApps')
            self.assertEqual(request.url.params.get('deviceId'), 'console-1')

    async def test_valid_sdk_contract_uses_single_request_without_fallback(self):
        self.documents = [complete_payload()]
        InstalledPackagesList.model_validate(self.documents[0])
        self.assertEqual(await self.client.list_apps('console-1'),
                         [{'id': PRODUCT_ID, 'name': 'Application test'}])
        self.assertEqual(len(self.requests), 1)

    async def test_fallback_does_not_invent_store_ids_from_other_identifiers(self):
        self.documents[0]['result'] += [
            {'titleId': 999, 'instanceId': 'not-a-store-product', 'name': 'Sans identifiant Store'},
            {'oneStoreProductId': 12345, 'name': 'Identifiant du mauvais type'},
            {'oneStoreProductId': '  ', 'name': 'Identifiant vide'},
            {'oneStoreProductId': 'SECOND_STORE_ID', 'name': None},
        ]
        result = await self.client.list_apps('console-1')
        self.assertEqual(result, [
            {'id': PRODUCT_ID, 'name': 'Application test'},
            {'id': 'SECOND_STORE_ID', 'name': 'SECOND_STORE_ID'},
        ])

    async def test_http_403_is_not_retried_as_a_schema_failure(self):
        self.status_code = 403
        self.documents = [{'error': PRIVATE_VALUE}]
        with self.assertRaises(sg.SmartglassError) as caught:
            await self.client.list_apps('console-1')
        self.assertEqual(len(self.requests), 1)
        message = str(caught.exception)
        self.assertIn('HTTP 403', message)
        self.assertIn('chargement des applications', message)
        self.assertNotIn(PRIVATE_VALUE, message)

    async def test_raw_fallback_still_requires_success_status(self):
        self.documents[0]['status']['errorCode'] = 'RemoteManagementDisabled'
        with self.assertRaisesRegex(sg.SmartglassError, 'RemoteManagementDisabled'):
            await self.client.list_apps('console-1')
        self.assertEqual(len(self.requests), 2)

    async def test_raw_fallback_rejects_malformed_root_and_result(self):
        invalid_documents = [
            [],
            {'status': {'errorCode': 'OK'}, 'result': None},
            {'status': {'errorCode': 'OK'}, 'result': {'oneStoreProductId': PRODUCT_ID}},
            {'status': {'errorCode': 'OK'}, 'result': [PRIVATE_VALUE]},
            {'result': []},
        ]
        for invalid in invalid_documents:
            with self.subTest(document_type=type(invalid).__name__):
                self.requests.clear()
                self.documents = [minimal_payload(), invalid]
                with self.assertRaises(sg.SmartglassError) as caught:
                    await self.client.list_apps('console-1')
                self.assertEqual(len(self.requests), 2)
                self.assertNotIn(PRIVATE_VALUE, str(caught.exception))

    async def test_unreadable_json_after_schema_retry_has_sanitized_stage(self):
        self.documents = [minimal_payload(), (PRIVATE_VALUE + ' not JSON').encode()]
        with self.assertRaises(sg.SmartglassError) as caught:
            await self.client.list_apps('console-1')
        self.assertEqual(len(self.requests), 2)
        message = str(caught.exception)
        self.assertIn('chargement des applications', message)
        self.assertIn('JSON illisible', message)
        self.assertNotIn(PRIVATE_VALUE, message)

    async def test_empty_valid_result_is_an_empty_app_list(self):
        self.documents = [{'status': {'errorCode': 'OK'}, 'result': []}]
        self.assertEqual(await self.client.list_apps('console-1'), [])
        self.assertEqual(len(self.requests), 1)


class DiagnosticSanitization(unittest.TestCase):
    def test_validation_fields_are_visible_without_input_tokens(self):
        payload = {
            'token_type': 'Bearer', 'expires_in': PRIVATE_VALUE,
            'scope': 'test', 'access_token': PRIVATE_VALUE,
            'refresh_token': PRIVATE_VALUE, 'user_id': 'fixture-user',
        }
        with self.assertRaises(ValidationError) as caught:
            OAuth2TokenResponse.model_validate(payload)
        message = sg.describe_service_error(caught.exception, 'connexion Microsoft/Xbox')
        self.assertIn('expires_in', message)
        self.assertIn('format de reponse incompatible', message)
        self.assertNotIn(PRIVATE_VALUE, message)
        self.assertNotIn('input_value', message)

    def test_xsts_authentication_refusal_is_not_a_network_diagnosis(self):
        message = sg.describe_service_error(AuthenticationException(), 'connexion Microsoft/Xbox')
        self.assertIn('XSTS refusee', message)
        self.assertNotIn('fonctions distantes', message)

    def test_proxy_and_timeout_error_values_are_not_echoed(self):
        for error, label in [
            (httpx.ProxyError(PRIVATE_VALUE), 'proxy'),
            (httpx.ReadTimeout(PRIVATE_VALUE), 'delai'),
            (httpx.ConnectError(PRIVATE_VALUE), 'requete reseau'),
            (ValueError(PRIVATE_VALUE), 'compatibilite'),
        ]:
            with self.subTest(error=type(error).__name__):
                message = sg.describe_service_error(error, 'chargement des applications')
                self.assertIn(label, message)
                self.assertNotIn(PRIVATE_VALUE, message)

    def test_tls_verification_failure_is_identified_without_printing_causes(self):
        error = httpx.ConnectError(PRIVATE_VALUE)
        error.__cause__ = ssl.SSLCertVerificationError(PRIVATE_VALUE)
        message = sg.describe_service_error(error, 'chargement des applications')
        self.assertIn('certificat TLS', message)
        self.assertNotIn(PRIVATE_VALUE, message)


if __name__ == '__main__':
    unittest.main()
