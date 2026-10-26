"""Refus de liaison HTTP : diagnostic exact, aucun repli ni arret d'un tiers."""
import errno
import socket
import threading
import unittest
from unittest.mock import patch

import xbox_app


class HTTPStartupTests(unittest.TestCase):
    def application(self):
        app = xbox_app.XboxRedirectApp.__new__(xbox_app.XboxRedirectApp)
        app.server = None
        app.http_ready = threading.Event()
        app.messages = []
        app.log_threadsafe = app.messages.append
        return app

    def test_windows_access_denied_keeps_original_error(self):
        error = PermissionError(errno.EACCES, 'Acces socket refuse par Windows')
        error.winerror = 10013
        app = self.application()
        with patch.object(xbox_app, 'ThreadingHTTPServer', side_effect=error):
            app.run_server()
        self.assertFalse(app.http_ready.is_set())
        self.assertIsNone(app.server)
        text = '\n'.join(app.messages)
        self.assertIn('Acces socket refuse par Windows', text)
        self.assertIn('10013', text)
        self.assertIn('exclusion TCP', text)
        self.assertIn('Diagnostiquer_Port80.bat', text)
        self.assertNotIn('lance l\'appli en ADMINISTRATEUR', text)

    def test_address_in_use_is_distinguished_from_permissions(self):
        error = OSError(errno.EADDRINUSE, 'Address already in use')
        text = xbox_app.describe_http_bind_error(80, error)
        self.assertIn('deja occupe', text)
        self.assertNotIn('Acces refuse', text)

    def test_existing_listener_is_preserved_when_startup_fails(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as existing:
            existing.bind(('127.0.0.1', 0))
            existing.listen(2)
            port = existing.getsockname()[1]
            app = self.application()
            with patch.object(xbox_app, 'HTTP_PORT', port):
                app.run_server()
            self.assertFalse(app.http_ready.is_set())
            self.assertIn('deja occupe', '\n'.join(app.messages))
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                connection, _ = existing.accept()
                connection.close()
            self.assertIsNone(app.server)


if __name__ == '__main__':
    unittest.main()
