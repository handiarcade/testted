"""Ouverture du portail developpeur : aucune connexion a une vraie console."""
import unittest
from unittest.mock import Mock
from urllib.parse import urlsplit

from xbox_devtools import DevPortalError, open_portal, portal_url


PRIVATE_VALUE = 'PRIVATE_PASSWORD_MUST_NOT_APPEAR'


class PortalAddressTests(unittest.TestCase):
    def assert_portal(self, address, expected_host, expected_port, port='11443'):
        url = portal_url(address, port)
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.hostname, expected_host)
        self.assertEqual(parsed.port, expected_port)
        self.assertIn(parsed.path, ('', '/'))
        self.assertFalse(parsed.query)
        self.assertFalse(parsed.fragment)
        self.assertIsNone(parsed.username)
        self.assertIsNone(parsed.password)
        return url

    def test_ipv4_hostname_and_configured_port(self):
        self.assert_portal('192.168.1.50', '192.168.1.50', 11443)
        self.assert_portal('xbox.local', 'xbox.local', 11444, port='11444')

    def test_explicit_https_root_and_host_ports_are_preserved(self):
        self.assert_portal('https://xbox.local:12345/', 'xbox.local', 12345)
        self.assert_portal('xbox.local:12345', 'xbox.local', 12345)
        self.assert_portal('https://xbox.local/', 'xbox.local', 11443)

    def test_ipv6_addresses_are_bracketed_for_the_browser(self):
        for address in ('fd00::42', '[fd00::42]', 'https://[fd00::42]:11443/'):
            with self.subTest(address=address):
                url = self.assert_portal(address, 'fd00::42', 11443)
                self.assertIn('[fd00::42]', url)

    def test_non_https_urls_and_non_root_resources_are_rejected(self):
        addresses = [
            '', 'http://xbox.local', 'ftp://xbox.local',
            'javascript:alert(1)', 'file:///C:/Windows',
            'https://', 'https:///missing-host',
            'https://xbox.local/api/filesystem/apps/files',
            'https://xbox.local/%2e%2e',
            'https://xbox.local?token=' + PRIVATE_VALUE,
            'https://xbox.local#' + PRIVATE_VALUE,
        ]
        for address in addresses:
            with self.subTest(address=address), self.assertRaises(DevPortalError) as caught:
                portal_url(address)
            self.assertNotIn(PRIVATE_VALUE, str(caught.exception))

    def test_credentials_are_refused_without_echoing_them(self):
        for address in (
            f'https://user:{PRIVATE_VALUE}@xbox.local:11443/',
            f'user:{PRIVATE_VALUE}@xbox.local',
            'https://user@xbox.local/',
        ):
            with self.subTest(address=address), self.assertRaises(DevPortalError) as caught:
                portal_url(address)
            self.assertNotIn(PRIVATE_VALUE, str(caught.exception))

    def test_invalid_ports_are_rejected(self):
        for port in ('0', '-1', '65536', 'not-a-port', ''):
            with self.subTest(port=port), self.assertRaises(DevPortalError):
                portal_url('xbox.local', port)
        for address in ('https://xbox.local:0/', 'https://xbox.local:65536/',
                        'https://xbox.local:not-a-port/'):
            with self.subTest(address=address), self.assertRaises(DevPortalError):
                portal_url(address)


class PortalOpeningTests(unittest.TestCase):
    def test_opens_validated_https_url_only(self):
        browser = Mock(return_value=True)
        result = open_portal('192.168.1.50', browser=browser)
        browser.assert_called_once_with(result)
        parsed = urlsplit(result)
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.port),
                         ('https', '192.168.1.50', 11443))

    def test_invalid_input_is_rejected_before_browser_action(self):
        browser = Mock(return_value=True)
        with self.assertRaises(DevPortalError):
            open_portal(f'https://user:{PRIVATE_VALUE}@xbox.local', browser=browser)
        browser.assert_not_called()

    def test_browser_failure_remains_controlled_and_does_not_echo_errors(self):
        for browser in (Mock(return_value=False), Mock(side_effect=OSError(PRIVATE_VALUE))):
            with self.subTest(browser=browser), self.assertRaises(DevPortalError) as caught:
                open_portal('192.168.1.50', browser=browser)
            self.assertNotIn(PRIVATE_VALUE, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
