"""Interface complete : transferts HTTP/DNS et lancement via SmartGlass OpenXbox."""
import asyncio
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from xbox_notifications import NotificationApp
from xbox_smartglass import SmartglassClient
from xbox_devtools import DevPortalWindow


class SmartglassWindow(tk.Toplevel):
    def __init__(self, application):
        super().__init__(application.root)
        self.application = application
        self.title('SmartGlass - applications Xbox')
        self.geometry('720x450')
        self.consoles = []
        self.apps = []
        ttk.Label(self, text='Connexion au compte Microsoft associe a votre Xbox.',
                  wraplength=660).pack(anchor='w', padx=12, pady=8)
        ttk.Button(self, text='Connexion Microsoft / Charger les consoles',
                   command=self.connect).pack(anchor='w', padx=12)
        ttk.Label(self, text='Console :').pack(anchor='w', padx=12, pady=(8, 0))
        self.console_box = ttk.Combobox(self, state='readonly')
        self.console_box.pack(fill='x', padx=12)
        self.console_box.bind('<<ComboboxSelected>>', self.console_changed)
        ttk.Button(self, text='Charger les applications installees',
                   command=self.load_apps).pack(anchor='w', padx=12, pady=6)
        ttk.Label(self, text='Application :').pack(anchor='w', padx=12)
        self.app_box = ttk.Combobox(self, state='readonly')
        self.app_box.pack(fill='x', padx=12)
        self.app_box.bind('<<ComboboxSelected>>', self.app_changed)
        ttk.Button(self, text="Lancer l'application sur la Xbox",
                   command=self.launch).pack(anchor='w', padx=12, pady=8)
        ttk.Checkbutton(self, text="Lancer cette application au premier transfert apres Activer",
                        variable=application.sg_auto_launch).pack(anchor='w', padx=12)
        ttk.Label(self, textvariable=application.sg_status,
                  wraplength=680).pack(anchor='w', padx=12, pady=8)
        ttk.Label(self, text="Le lancement d'Edge n'ouvre pas automatiquement une URL.",
                  wraplength=680).pack(anchor='w', padx=12)
        ttk.Button(self, text='Dossiers / applications du mode developpeur',
                   command=application.open_devtools).pack(anchor='w', padx=12, pady=8)

    def connect(self):
        self.application.run_smartglass(
            'Connexion Microsoft / consoles...',
            lambda: self.application.smartglass.list_consoles(
                authenticate=True,
                on_auth_url=lambda _: self.application._post_ui(
                    self.application.sg_status.set,
                    'Terminez la connexion Microsoft dans le navigateur du PC.'),
            ), self.set_consoles,
        )

    def set_consoles(self, consoles):
        if not self.winfo_exists():
            return
        self.consoles = consoles
        self.console_box['values'] = [f"{item['name']} - {item['power_state']}" for item in consoles]
        self.apps = []
        self.app_box.set('')
        self.app_box['values'] = []
        self.application.sg_target = None
        if consoles:
            self.console_box.current(0)
            self.application.sg_status.set('Choisissez la console, puis chargez ses applications.')
        else:
            self.console_box.set('')
            self.application.sg_status.set('Aucune console associee trouvee pour ce compte.')

    def console_changed(self, _=None):
        self.apps = []
        self.app_box.set('')
        self.app_box['values'] = []
        self.application.sg_target = None

    def load_apps(self):
        index = self.console_box.current()
        if not 0 <= index < len(self.consoles):
            messagebox.showerror('SmartGlass', 'Connectez-vous et choisissez une console.', parent=self)
            return
        console = self.consoles[index]
        if not console['remote_enabled']:
            self.application.sg_status.set('Activez les fonctionnalites a distance dans les parametres Xbox.')
            return
        # Conserver l'identifiant : la selection peut changer pendant la requete.
        device_id = console['id']
        self.application.run_smartglass(
            'Chargement des applications...',
            lambda: self.application.smartglass.list_apps(device_id),
            lambda apps: self.set_apps(device_id, apps),
        )

    def set_apps(self, device_id, apps):
        if not self.winfo_exists():
            return
        index = self.console_box.current()
        if not 0 <= index < len(self.consoles) or self.consoles[index]['id'] != device_id:
            return
        self.apps = apps
        self.app_box['values'] = [f"{item['name']} ({item['id']})" for item in apps]
        self.application.sg_target = None
        if apps:
            self.app_box.current(0)
            self.app_changed()
            self.application.sg_status.set('Choisissez une application puis cliquez sur Lancer.')
        else:
            self.app_box.set('')
            self.application.sg_status.set("Aucune application avec identifiant Store exploitable n'a ete retournee.")

    def app_changed(self, _=None):
        device_index = self.console_box.current()
        app_index = self.app_box.current()
        if 0 <= device_index < len(self.consoles) and 0 <= app_index < len(self.apps):
            device = self.consoles[device_index]
            application = self.apps[app_index]
            self.application.sg_target = (device['id'], application['id'], application['name'])

    def launch(self):
        target = self.application.sg_target
        if not target:
            messagebox.showerror('SmartGlass', 'Selectionnez une console et une application.', parent=self)
            return
        self.application.launch_target(target)


class XboxSuiteApp(NotificationApp):
    def __init__(self, root):
        super().__init__(root)
        self.smartglass = SmartglassClient()
        self.sg_status = tk.StringVar(master=root, value='SmartGlass : non connecte')
        self.sg_auto_launch = tk.BooleanVar(master=root, value=False)
        self.sg_target = None
        self.sg_busy = False
        self.sg_window = None
        self.dev_window = None
        self._auto_target = None
        self._auto_sent = False
        self._auto_generation = 0
        ttk.Button(root, text='SmartGlass : lancer une application Xbox',
                   command=self.open_smartglass).pack(before=self.log_area, padx=10, pady=5)

    def open_smartglass(self):
        if self.sg_window is not None and self.sg_window.winfo_exists():
            self.sg_window.lift()
        else:
            self.sg_window = SmartglassWindow(self)

    def open_devtools(self):
        if self.dev_window is not None and self.dev_window.winfo_exists():
            self.dev_window.lift()
        else:
            self.dev_window = DevPortalWindow(self)

    def run_smartglass(self, label, operation, on_result):
        if self.sg_busy:
            self.log('Une operation SmartGlass est deja en cours.')
            return False
        self.sg_busy = True
        self.sg_status.set(label)

        def worker():
            try:
                result = asyncio.run(operation())
            except Exception as exc:
                # Les messages du backend sont controles et ne contiennent pas de jetons.
                self._post_ui(self.smartglass_failed, exc)
            else:
                self._post_ui(self.smartglass_finished, on_result, result)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def smartglass_failed(self, error):
        from xbox_smartglass import SmartglassError
        self.sg_busy = False
        detail = str(error) if isinstance(error, SmartglassError) else type(error).__name__
        self.sg_status.set(f'SmartGlass : {detail}')
        self.log(f'SmartGlass : {detail}')

    def smartglass_finished(self, callback, result):
        self.sg_busy = False
        callback(result)

    def launch_target(self, target):
        device_id, product_id, name = target
        return self.run_smartglass(
            f'Demande de lancement : {name}...',
            lambda: self.smartglass.launch(device_id, product_id),
            lambda result: self.show_launch_result(name, result),
        )

    def show_launch_result(self, name, result):
        if result['status'] == 'succeeded':
            text = f'SmartGlass : lancement confirme pour {name}.'
        else:
            text = f'SmartGlass : commande envoyee pour {name}, lancement non confirme.'
        self.sg_status.set(text)
        self.log(text)

    def start(self):
        if self.active or self.starting or self.stopping:
            return
        if self.sg_auto_launch.get() and not self.sg_target:
            self.open_smartglass()
            self.log("Choisissez l'application SmartGlass avant d'activer le lancement automatique.")
            return
        self._auto_target = self.sg_target if self.sg_auto_launch.get() else None
        self._auto_sent = False
        self._auto_generation += 1
        super().start()

    def stop(self):
        self._auto_generation += 1
        self._auto_target = None
        super().stop()

    def _show_download(self, ip, filename, kind):
        previous = self._latest_download['event']
        super()._show_download(ip, filename, kind)
        if self._latest_download['event'] != previous and self._auto_target and not self._auto_sent:
            self._auto_sent = True
            self._attempt_auto_launch(self._auto_target, time.monotonic() + 30,
                                      self._auto_generation)

    def _attempt_auto_launch(self, target, deadline, generation):
        if generation != self._auto_generation or not (self.active or self.starting):
            return
        if self.sg_busy:
            if time.monotonic() < deadline:
                self.root.after(500, self._attempt_auto_launch, target, deadline, generation)
            else:
                self.log('SmartGlass occupe : lancement automatique non envoye.')
            return
        self.launch_target(target)


def main():
    root = tk.Tk()
    application = XboxSuiteApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
