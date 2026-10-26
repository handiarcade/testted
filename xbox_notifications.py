"""Lanceur avec detection des transferts et page de message pour Edge Xbox."""
import os
import json
import time
import tkinter as tk
from tkinter import ttk
from urllib.parse import unquote, urlsplit

import xbox_app

BaseServer = xbox_app.ThreadingHTTPServer
MESSAGE_PAGE = b'''<!doctype html>
<html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Message pour Leo</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;
background:#101010;color:white;font:700 48px system-ui;text-align:center}
main{padding:40px;border:3px solid #107c10;border-radius:24px}</style>
<main id="message">En attente d'un telechargement...</main>
<script>
async function poll() {
  try {
    const response = await fetch('/status', {cache: 'no-store'});
    if (!response.ok) throw new Error('Serveur indisponible');
    const state = await response.json();
    document.getElementById('message').textContent = state.event > 0
      ? 'non non leo interdit' : "En attente d'un telechargement...";
  } catch (_) {
    document.getElementById('message').textContent = 'Connexion au PC interrompue';
  } finally {
    setTimeout(poll, 2000);
  }
}
poll();
</script></html>'''


class ObservedWriter:
    def __init__(self, stream, handler):
        self.stream = stream
        self.handler = handler

    def write(self, data):
        result = self.stream.write(data)
        handler = self.handler
        if data and handler._body_ready and not handler._download_reported:
            handler._download_reported = True
            if handler._transfer_kind and handler._response_status in (200, 206):
                filename = os.path.basename(unquote(urlsplit(handler.path).path))
                handler.server.download_callback(
                    handler.client_address[0], filename or '(sans nom)',
                    handler._transfer_kind,
                )
        return result

    def __getattr__(self, name):
        return getattr(self.stream, name)


def make_server_class(callback, force_source=False, source_provider=None,
                      status_provider=None):
    class NotificationServer(BaseServer):
        def __init__(self, address, original_handler):
            self.download_callback = callback

            class Handler(original_handler):
                def setup(self):
                    super().setup()
                    self._body_ready = False
                    self._download_reported = False
                    self._transfer_kind = None
                    self._response_status = None
                    self.wfile = ObservedWriter(self.wfile, self)

                def send_response(self, code, message=None):
                    self._response_status = code
                    super().send_response(code, message)

                def end_headers(self):
                    super().end_headers()
                    self._body_ready = True

                def do_GET(self, is_head=False):
                    self._body_ready = False
                    self._download_reported = False
                    self._transfer_kind = None
                    self._response_status = None
                    route = urlsplit(self.path).path
                    if route == '/status':
                        state = status_provider() if status_provider else {'event': 0}
                        payload = json.dumps(state).encode('utf-8')
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json; charset=utf-8')
                        self.send_header('Content-Length', str(len(payload)))
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.end_headers()
                        if not is_head:
                            self.wfile.write(payload)
                        return
                    if route == '/message':
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.send_header('Content-Length', str(len(MESSAGE_PAGE)))
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.end_headers()
                        if not is_head:
                            self.wfile.write(MESSAGE_PAGE)
                        return
                    if force_source and source_provider:
                        runtime, paused = source_provider()
                        if not paused and runtime.get('mode') == 'redirect':
                            # La console reste connectee au PC : pas de 302
                            # vers une source distante, ni de repli automatique.
                            self.serve_proxy(runtime['url'], self.headers.get('Range', ''),
                                             is_head=is_head)
                            return
                    super().do_GET(is_head=is_head)

                def serve_file(self, path, is_head=False):
                    self._transfer_kind = 'fichier local'
                    super().serve_file(path, is_head=is_head)

                def serve_proxy(self, url, rng, is_head=False):
                    self._transfer_kind = 'proxy'
                    super().serve_proxy(url, rng, is_head=is_head)

            super().__init__(address, Handler)

    return NotificationServer


class NotificationApp(xbox_app.XboxRedirectApp):
    def __init__(self, root):
        super().__init__(root)
        self.download_label = ttk.Label(
            root, text='Telechargement : aucun transfert detecte',
            foreground='gray', wraplength=640,
        )
        self.download_label.pack(before=self.log_area, padx=10, pady=5)
        self._recent_downloads = {}
        self._latest_download = {'event': 0}
        cfg = self.load_config()
        self.force_source = tk.BooleanVar(value=bool(cfg.get('force_source', True)))
        ttk.Checkbutton(
            root, text='Imposer ma source aux requetes HTTP (URL via proxy)',
            variable=self.force_source,
        ).pack(before=self.log_area, padx=10, pady=3)

    def save_config(self):
        cfg = self.load_config()
        cfg.update(mode=self.mode.get(), url=self.url_var.get(),
                   local_path=self.path_var.get(), log_all=self.log_all.get(),
                   force_source=self.force_source.get())
        try:
            temporary = xbox_app.CONFIG_FILE + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(cfg, stream, indent=2)
            os.replace(temporary, xbox_app.CONFIG_FILE)
        except OSError as exc:
            self.log(f'Enregistrement impossible : {exc}')

    def download_started(self, ip, filename, kind):
        self._post_ui(self._show_download, ip, filename, kind)

    def _show_download(self, ip, filename, kind):
        now = time.monotonic()
        key = (ip, filename, kind)
        last = self._recent_downloads.get(key)
        if last is not None and now - last < 5:
            return
        self._recent_downloads[key] = now
        if len(self._recent_downloads) > 128:
            self._recent_downloads.pop(next(iter(self._recent_downloads)))
        text = f'Telechargement detecte : {ip} - {filename} ({kind})'
        self.download_label.config(text=text, foreground='green')
        self.log(text)
        self._latest_download = {'event': self._latest_download['event'] + 1}

    def start(self):
        if self.active or self.starting or self.stopping:
            return
        force = bool(self.force_source.get())
        xbox_app.ThreadingHTTPServer = make_server_class(
            self.download_started, force_source=force,
            source_provider=lambda: (self.runtime, self.paused),
            status_provider=lambda: self._latest_download,
        )
        super().start()
        if self.starting:
            self.log(
                f'Page pour Edge sur Xbox : http://{self.get_local_ip()}/message'
            )
            self.log('Ouvrez cette page une fois dans Edge : le message se mettra a jour automatiquement.')
            if force:
                self.log('Source directe active : toute URL est proxifiee par le PC, sans redirection 302.')
                self.log('Cela ne force pas la console a accepter ou installer le fichier.')

    def _finish_stop(self):
        self.download_label.config(
            text='Telechargement : surveillance arretee', foreground='gray'
        )
        self._recent_downloads.clear()
        self._latest_download = {'event': 0}
        super()._finish_stop()


if __name__ == '__main__':
    root = tk.Tk()
    application = NotificationApp(root)
    root.mainloop()
