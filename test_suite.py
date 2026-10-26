"""Interface Tk reelle ; operations Xbox simulees, aucune commande vers une console."""
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import AsyncMock, patch

import xbox_app
from xbox_suite import XboxSuiteApp


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'Ecran Tk requis')
class SuiteGUI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = patch.object(xbox_app, 'CONFIG_FILE', str(Path(self.tmp.name) / 'config.json'))
        config.start()
        self.addCleanup(config.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.close_root)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.app = XboxSuiteApp(self.root)
        self.root.update()

    def close_root(self):
        self.app.closing = True
        for identifier in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(identifier)
        self.root.destroy()

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def select_app(self):
        self.app.open_smartglass()
        window = self.app.sg_window
        window.set_consoles([
            dict(id='xbox-1', name='Salon', power_state='On', remote_enabled=True),
            dict(id='xbox-2', name='Chambre', power_state='On', remote_enabled=True),
        ])
        window.set_apps('xbox-1', [dict(id='PRODUCT-1', name='Application')])
        self.root.update()
        return window

    def test_main_window_and_application_selection(self):
        window = self.select_app()
        self.assertTrue(window.winfo_exists())
        self.assertEqual(self.app.sg_target, ('xbox-1', 'PRODUCT-1', 'Application'))
        window.console_box.current(1)
        window.console_changed()
        window.set_apps('xbox-1', [dict(id='WRONG', name='Ancienne console')])
        self.assertIsNone(self.app.sg_target)
        self.assertEqual(window.apps, [])

    def test_manual_launch_uses_selection_and_pending_stays_unconfirmed(self):
        window = self.select_app()
        self.app.smartglass.launch = AsyncMock(return_value=dict(status='pending', operation_id='operation'))
        window.launch()
        deadline = time.monotonic() + 2
        while self.app.sg_busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app.sg_busy)
        self.app.smartglass.launch.assert_awaited_once_with('xbox-1', 'PRODUCT-1')
        self.assertIn('non confirme', self.app.sg_status.get())

    def test_auto_launch_once_for_multiple_ranges(self):
        self.app.active = True
        self.app._auto_target = ('xbox-1', 'PRODUCT-1', 'Application')
        with patch.object(self.app, 'launch_target', return_value=True) as launch:
            self.app._show_download('192.0.2.1', 'game.xvc', 'proxy')
            self.app._show_download('192.0.2.1', 'game.xvc', 'proxy')
            self.app._show_download('192.0.2.1', 'other.xvc', 'proxy')
        launch.assert_called_once_with(('xbox-1', 'PRODUCT-1', 'Application'))
        self.app.active = False

    def test_stale_automatic_callback_cannot_launch_after_restart(self):
        self.app.active = True
        old_generation = self.app._auto_generation
        self.app._auto_generation += 1
        with patch.object(self.app, 'launch_target') as launch:
            self.app._attempt_auto_launch(('xbox-1', 'OLD', 'Ancienne application'),
                                          time.monotonic() + 30, old_generation)
        launch.assert_not_called()
        self.app.active = False


if __name__ == '__main__':
    unittest.main()
