"""
Appli PC pour servir / rediriger / proxifier les telechargements Xbox.

TROIS modes :
  1. Rediriger (302) vers une URL
     -> simple, mais fragile pour un gros fichier : une fois la console
        renvoyee ailleurs, tu n'as plus la main (HTTPS, Range non gere... ca casse).
  2. Servir un fichier / dossier local (avec Range / 206)
     -> ideal si tu as deja le .xvc / .msix sur ce PC.
  3. Proxy depuis une URL (telecharge ET re-sert avec Range)  <-- LE mode costaud
     -> ton PC va chercher le fichier a l'URL (HTTP *ou* HTTPS) et le re-sert
        a la console en HTTP, en transmettant les requetes Range. La console
        ne parle qu'a ton PC. Gros fichiers, source HTTPS, reprise : OK.

Points cles de fiabilite :
  - Le serveur HTTP ecoute DIRECTEMENT sur le port 80 (plus de portproxy fragile).
  - Le DNS spoof tourne dans le meme processus (thread) -> un seul .exe.
  - La config (mode, URL, dossier) est sauvee dans config.json a cote de l'exe.

MSIX / AppX (ajout du 2026-10-26) :
  - Les formats .msix / .msixbundle / .appx / .appxbundle / .appinstaller sont
    maintenant proposes dans le selecteur de fichier et recoivent leur type MIME
    officiel (utile si la cible verifie le Content-Type, ex. App Installer Windows).
  - .xvc / .msixvc restent servis en application/octet-stream (comportement Xbox
    inchange). Un .msix se sert comme n'importe quel fichier : mode 2 (fichier ou
    dossier local) ou mode 3 (proxy depuis une URL). Le support du Range / 206
    gere deja les gros paquets et la reprise.
"""
import queue
import sys
import uuid
from urllib.parse import urlsplit, unquote
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import subprocess
import os
import json
import ctypes
import errno
import socket
import mimetypes
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime

import xbox_dns_spoof

# Chemins
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
LOG_FILE = os.path.join(BASE_DIR, "xbox_requests.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

CHUNK = 64 * 1024
HTTP_PORT = 80          # le serveur ecoute DIRECTEMENT sur le 80
UPSTREAM_TIMEOUT = 30   # secondes, pour le mode proxy

# Types MIME des paquets.
# Cible = Xbox : on sert TOUT en application/octet-stream, exactement comme le
# CDN Microsoft. La console se fie au contenu (et au .xvc/.msixvc qu'elle a
# demande), pas a l'en-tete Content-Type ; octet-stream evite toute surprise.
# (Si un jour tu sers un .msix a une machine Windows via App Installer, ce sont
#  plutot les types application/msix, application/msixbundle... qu'il faudrait.)
for _ext in (".msix", ".msixbundle", ".appx", ".appxbundle",
             ".appinstaller", ".xvc", ".msixvc"):
    mimetypes.add_type("application/octet-stream", _ext)


def parse_sni(data):
    """Extrait le SNI (nom de domaine) d'un ClientHello TLS. None si absent.
    Sert au diagnostic : savoir pour quel domaine la console ouvre du HTTPS."""
    try:
        if len(data) < 43 or data[0] != 0x16:   # 0x16 = TLS handshake
            return None
        idx = 5                       # saute l'en-tete d'enregistrement TLS
        if data[idx] != 0x01:         # 0x01 = ClientHello
            return None
        idx += 4                      # type(1) + length(3)
        idx += 2                      # version du client
        idx += 32                     # random
        sid_len = data[idx]; idx += 1 + sid_len
        cs_len = int.from_bytes(data[idx:idx + 2], "big"); idx += 2 + cs_len
        comp_len = data[idx]; idx += 1 + comp_len
        ext_total = int.from_bytes(data[idx:idx + 2], "big"); idx += 2
        end = idx + ext_total
        while idx + 4 <= end:
            etype = int.from_bytes(data[idx:idx + 2], "big")
            elen = int.from_bytes(data[idx + 2:idx + 4], "big")
            idx += 4
            if etype == 0x0000:       # extension server_name (SNI)
                p = idx + 2 + 1       # server_name_list len(2) + type host_name(1)
                nlen = int.from_bytes(data[p:p + 2], "big"); p += 2
                return data[p:p + nlen].decode(errors="ignore")
            idx += elen
    except Exception:
        return None
    return None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def describe_http_bind_error(port, error):
    """Conserver l'erreur OS et distinguer conflit de port et refus d'acces."""
    winerror = getattr(error, "winerror", None)
    code = winerror if winerror is not None else getattr(error, "errno", None)
    details = f"Port HTTP {port} indisponible : {error}"
    if code is not None:
        details += f" [code socket {code}]"
    if code in (10048, errno.EADDRINUSE):
        reason = "Le port est deja occupe par une autre application ou un service."
    elif code == 10013:
        reason = ("Windows refuse la liaison au port : reservation/exclusion TCP, "
                  "service utilisant le port en exclusivite ou restriction de securite. "
                  "Les droits administrateur ne suffisent pas toujours.")
    elif isinstance(error, PermissionError) or code in (errno.EACCES, errno.EPERM):
        reason = "Acces refuse : verifier les droits, les reservations et les restrictions reseau."
    else:
        reason = "Verifier la disponibilite et les restrictions du port sur ce PC."
    return f"{details}\n{reason}\nLancez Diagnostiquer_Port80.bat et consultez diagnostic_port80.txt."


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Plusieurs connexions en parallele (la console en ouvre plusieurs)."""
    daemon_threads = True
    allow_reuse_address = True


class XboxRedirectApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Xbox Downloader - redirect / serve / proxy")
        self.root.geometry("680x620")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.server = None
        self.server_thread = None
        self.dns_thread = None
        self.dns_stop = None
        self.https_thread = None
        self.https_stop = None
        self.active = False
        self.paused = True
        self.starting = False
        self.stopping = False
        self.close_requested = False
        self.closing = False
        self.ui_queue = queue.Queue(maxsize=2000)
        self.firewall_rules = []
        self.http_ready = threading.Event()
        self.dns_ready = threading.Event()
        self.runtime = {}
        self.root.after(50, self._drain_ui)

        cfg = self.load_config()

        # --- Mode ---
        self.mode = tk.StringVar(value=cfg.get("mode", "proxy"))
        mode_frame = ttk.LabelFrame(root, text="Mode")
        mode_frame.pack(fill=tk.X, padx=10, pady=6)
        ttk.Radiobutton(mode_frame, text="1. Rediriger (302) vers une URL",
                        variable=self.mode, value="redirect",
                        command=self.refresh_mode).pack(anchor="w", padx=8, pady=2)
        ttk.Radiobutton(mode_frame, text="2. Servir un fichier / dossier local (Range)",
                        variable=self.mode, value="serve",
                        command=self.refresh_mode).pack(anchor="w", padx=8, pady=2)
        ttk.Radiobutton(mode_frame, text="3. Proxy depuis une URL (telecharge + re-sert, gros fichiers) *",
                        variable=self.mode, value="proxy",
                        command=self.refresh_mode).pack(anchor="w", padx=8, pady=2)

        # --- URL (modes redirect / proxy) ---
        self.url_frame = ttk.Frame(root)
        ttk.Label(self.url_frame, text="URL source :").pack(anchor="w")
        self.url_var = tk.StringVar(value=cfg.get("url", "https://example.com/fichier.xvc"))
        ttk.Entry(self.url_frame, textvariable=self.url_var, width=88).pack(fill=tk.X)

        # --- Dossier/fichier (mode serve) ---
        self.serve_frame = ttk.Frame(root)
        ttk.Label(self.serve_frame, text="Fichier ou dossier a servir :").pack(anchor="w")
        row = ttk.Frame(self.serve_frame)
        row.pack(fill=tk.X)
        self.path_var = tk.StringVar(value=cfg.get("local_path", ""))
        ttk.Entry(row, textvariable=self.path_var, width=70).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Fichier...", command=self.pick_file).pack(side=tk.LEFT, padx=3)
        ttk.Button(row, text="Dossier...", command=self.pick_dir).pack(side=tk.LEFT)
        ttk.Label(self.serve_frame,
                  text="Dossier : le fichier dont le nom correspond a la requete est servi.\n"
                       "Fichier seul : servi pour n'importe quelle requete.",
                  foreground="gray").pack(anchor="w", pady=2)

        # --- Boutons ---
        btn_frame = ttk.Frame(root)
        btn_frame.pack(pady=8)
        self.toggle_btn = ttk.Button(btn_frame, text="Activer", command=self.toggle)
        self.toggle_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Nettoyer / arreter", command=self.stop).pack(side=tk.LEFT, padx=5)

        self.log_all = tk.BooleanVar(value=cfg.get("log_all", False))
        ttk.Checkbutton(btn_frame, text="Logger toutes les requetes DNS",
                        variable=self.log_all).pack(side=tk.LEFT, padx=10)

        self.status_label = ttk.Label(root, text="Etat : ARRETE", foreground="red")
        self.status_label.pack(pady=3)
        self.console_label = ttk.Label(root, text="Console : recherche...", foreground="gray")
        self.console_label.pack(pady=2)

        ttk.Label(root, text="Logs :").pack()
        self.log_area = scrolledtext.ScrolledText(root, height=16, state="disabled")
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.refresh_mode()
        self.log("Pret. Choisis un mode, puis clique Activer.")
        self.log("Sur la Xbox : DNS manuel = IP de ce PC")
        if os.name == "nt" and not is_admin():
            self.log("Lancez l'application en administrateur pour configurer le pare-feu Windows.")

    # ---------- config ----------
    def load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_config(self):
        data = {
            "mode": self.mode.get(),
            "url": self.url_var.get(),
            "local_path": self.path_var.get(),
            "log_all": self.log_all.get(),
        }
        try:
            temp = CONFIG_FILE + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp, CONFIG_FILE)
        except Exception as e:
            self.log(f"Impossible de sauver la config : {e}")

    # ---------- UI helpers ----------
    def refresh_mode(self):
        self.url_frame.pack_forget()
        self.serve_frame.pack_forget()
        if self.mode.get() in ("redirect", "proxy"):
            self.url_frame.pack(fill=tk.X, padx=10, pady=4)
        else:
            self.serve_frame.pack(fill=tk.X, padx=10, pady=4)

    def pick_file(self):
        p = filedialog.askopenfilename(
            title="Fichier a servir",
            filetypes=[("Paquets / contenu",
                        "*.msix *.msixbundle *.msixvc *.appxbundle *.appx "
                        "*.appinstaller *.xvc *.zip *.bin"),
                       ("MSIX / AppX", "*.msix *.msixbundle *.appx *.appxbundle *.appinstaller"),
                       ("Tous", "*.*")])
        if p:
            self.path_var.set(p)

    def pick_dir(self):
        p = filedialog.askdirectory(title="Dossier a servir")
        if p:
            self.path_var.set(p)

    def log(self, msg):
        self.log_area.config(state="normal")
        self.log_area.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self.log_area.see(tk.END)
        self.log_area.config(state="disabled")

    def log_threadsafe(self, msg):
        self._post_ui(self.log, msg)

    def _post_ui(self, callback, *args):
        if not self.closing:
            try:
                self.ui_queue.put_nowait((callback, args))
            except queue.Full:
                pass

    def _drain_ui(self):
        for _ in range(100):
            try:
                callback, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            callback(*args)
        if not self.closing:
            self.root.after(50, self._drain_ui)

    # ---------- affichage console ----------
    def _set_console(self, text, color):
        self.console_label.config(text=text, foreground=color)

    def console_found(self, ip):
        """Appele depuis le thread DNS des que la console nous interroge."""
        self._post_ui(self._set_console, f"Client DNS : {ip}", "green")

    def console_request(self, ip):
        """Appele depuis le serveur HTTP a chaque requete de la console."""
        now = datetime.now().strftime('%H:%M:%S')
        self._post_ui(self._set_console, f"Client {ip} - requete {now}", "blue")

    def get_local_ip(self):
        return xbox_dns_spoof.get_local_ip()

    # ---------- start/stop ----------
    def toggle(self):
        if not self.active:
            self.start()
        else:
            self.stop()

    def start(self):
        if self.active or self.starting or self.stopping:
            return
        mode = self.mode.get()
        if mode in ("redirect", "proxy"):
            url = self.url_var.get().strip()
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or any(c in url for c in "\r\n"):
                messagebox.showerror("Erreur", "L'URL doit commencer par http:// ou https://")
                return
        else:
            p = self.path_var.get().strip()
            if not p or not os.path.exists(p):
                messagebox.showerror("Erreur", "Choisis un fichier ou un dossier existant.")
                return

        if os.name == "nt" and not is_admin():
            messagebox.showwarning(
                "Droits insuffisants",
                "Relancez l'application en administrateur pour creer ses regles de pare-feu Windows."
            )

        self.save_config()
        self.log("Activation...")

        self.runtime = {"mode": mode, "url": self.url_var.get().strip(),
                        "local_path": self.path_var.get().strip()}
        if not self._configure_firewall():
            return
        self.http_ready.clear()
        self.dns_ready.clear()
        self.starting = True
        self.toggle_btn.config(state="disabled")
        self.status_label.config(text="Etat : DEMARRAGE", foreground="orange")

        self.dns_stop = threading.Event()
        self.dns_thread = threading.Thread(
            target=xbox_dns_spoof.run_dns,
            kwargs={"log": self.log_threadsafe, "stop_event": self.dns_stop,
                    "log_all": self.log_all.get(), "on_client": self.console_found,
                    "on_ready": lambda port: self.dns_ready.set()},
            daemon=True,
        )
        self.dns_thread.start()

        self.paused = False
        self.server_thread = threading.Thread(target=self.run_server, daemon=True)
        self.server_thread.start()

        # Sonde HTTPS (diagnostic) : voir si la console tente le 443.
        self.https_stop = threading.Event()
        self.https_thread = threading.Thread(target=self.run_https_probe, daemon=True)
        self.https_thread.start()

        self.root.after(50, self._check_startup)

    def _check_startup(self):
        if not self.starting:
            return
        if (self.http_ready.is_set() and self.dns_ready.is_set()
                and self.server_thread.is_alive() and self.dns_thread.is_alive()):
            self.starting = False
            self.active = True
            self.toggle_btn.config(text="Arreter", state="normal")
            self.status_label.config(text="Etat : ACTIF", foreground="green")
            self.log(f"Mode {self.runtime['mode']} - IP du PC : {self.get_local_ip()}")
        elif not self.server_thread.is_alive() or not self.dns_thread.is_alive():
            self.log("Echec du demarrage : voir les erreurs ci-dessus.")
            self.stop()
        else:
            self.root.after(50, self._check_startup)

    def _configure_firewall(self):
        if os.name != "nt":
            return True
        token = uuid.uuid4().hex
        for protocol, port in (("TCP", 80), ("UDP", 53), ("TCP", 443)):
            name = f"XboxDownloader-{token}-{protocol}-{port}"
            try:
                result = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "add", "rule",
                     f"name={name}", "dir=in", "action=allow", f"protocol={protocol}",
                     f"localport={port}", "remoteip=LocalSubnet", "profile=private"],
                    capture_output=True, timeout=10)
                if result.returncode:
                    raise OSError("netsh a refuse la regle")
                self.firewall_rules.append(name)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.log(f"Configuration du pare-feu impossible : {exc}")
                self.cleanup()
                return False
        return True

    def stop(self):
        if self.stopping:
            return
        self.stopping = True
        self.starting = False
        self.paused = True
        self.active = False
        self.toggle_btn.config(state="disabled")
        self.status_label.config(text="Etat : ARRET EN COURS", foreground="orange")
        for event in (self.dns_stop, self.https_stop):
            if event:
                event.set()
        threading.Thread(target=self._stop_services, daemon=True).start()

    def _stop_services(self):
        # Attendre la fin du bind avant de recuperer le serveur a fermer.
        while self.server_thread and self.server_thread.is_alive() and not self.http_ready.is_set():
            self.http_ready.wait(0.05)
        server = self.server
        if server:
            server.shutdown()
            server.server_close()
        for worker in (self.server_thread, self.dns_thread, self.https_thread):
            if worker:
                worker.join()
        self.server = None
        self.cleanup()
        self._post_ui(self._finish_stop)

    def _finish_stop(self):
        self.stopping = False
        self.toggle_btn.config(text="Activer", state="normal")
        self.status_label.config(text="Etat : ARRETE", foreground="red")
        self.console_label.config(text="Console : recherche...", foreground="gray")
        if self.close_requested:
            self.closing = True
            self.root.destroy()

    def cleanup(self):
        # Ne touche ni aux portproxy ni aux regles appartenant a un autre logiciel.
        remaining = []
        for name in self.firewall_rules:
            try:
                result = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"],
                    capture_output=True, timeout=10)
                if result.returncode:
                    remaining.append(name)
            except (OSError, subprocess.TimeoutExpired):
                remaining.append(name)
        self.firewall_rules = remaining
        if remaining:
            self.log_threadsafe("Certaines regles de cette session n'ont pas pu etre supprimees.")

    # ---------- resolution fichier local ----------
    def resolve_local_file(self, req_path):
        p = self.runtime["local_path"]
        if os.path.isfile(p):
            return p
        if os.path.isdir(p):
            name = os.path.basename(unquote(urlsplit(req_path).path).rstrip("/"))
            if not name:
                return None
            candidate = os.path.join(p, name)
            root = os.path.realpath(p)
            candidate = os.path.realpath(candidate)
            if os.path.commonpath((root, candidate)) == root and os.path.isfile(candidate):
                return candidate
        return None

    # ---------- serveur HTTP ----------
    def run_server(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(UPSTREAM_TIMEOUT)

            def _note_client(self):
                ip = self.client_address[0]
                app.console_request(ip)
                return ip

            def do_GET(self, is_head=False):
                ip = self._note_client()
                rng = self.headers.get("Range", "")
                app.log_threadsafe(f"{self.command} {ip} : {self.path}"
                                   + (f"  [Range: {rng}]" if rng else ""))

                if app.paused:
                    self.send_response(503)
                    self.send_header("Content-Length", "6")
                    self.end_headers()
                    if not is_head:
                        self.wfile.write(b"Paused")
                    return

                mode = app.runtime["mode"]
                if mode == "redirect":
                    url = app.runtime["url"]
                    self.send_response(302)
                    self.send_header("Location", url)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    app.log_threadsafe(f"  -> 302 {url}")
                    return

                if mode == "proxy":
                    self.serve_proxy(app.runtime["url"], rng, is_head=is_head)
                    return

                # mode serve
                target = app.resolve_local_file(self.path)
                if not target:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    app.log_threadsafe("  -> 404 (aucun fichier local correspondant)")
                    return
                self.serve_file(target, is_head=is_head)

            def do_HEAD(self):
                self.do_GET(is_head=True)

            # ---- mode SERVE : fichier local avec Range ----
            def serve_file(self, path, is_head=False):
                try:
                    fsize = os.path.getsize(path)
                except OSError:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                start, end, status = 0, fsize - 1, 200
                rng = self.headers.get("Range")
                if not is_head and rng and rng.startswith("bytes=") and "," not in rng:
                    try:
                        part = rng.split("=", 1)[1].split(",")[0].strip()
                        s, _, e = part.partition("-")
                        if s == "":
                            length = int(e)
                            start = max(0, fsize - length)
                            end = fsize - 1
                        else:
                            start = int(s)
                            end = int(e) if e else fsize - 1
                        end = min(end, fsize - 1)
                        if start < 0 or start > end or start >= fsize:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{fsize}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        status = 206
                    except Exception:
                        start, end, status = 0, fsize - 1, 200

                length = end - start + 1
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
                self.end_headers()
                app.log_threadsafe(f"  -> {status} {os.path.basename(path)} "
                                   f"({start}-{end}/{fsize})")
                if is_head:
                    return
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        buf = f.read(min(CHUNK, remaining))
                        if not buf:
                            break
                        try:
                            self.wfile.write(buf)
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        remaining -= len(buf)

            # ---- mode PROXY : telecharge depuis l'URL et re-sert ----
            def serve_proxy(self, url, rng, is_head=False):
                # On transmet le Range de la console telle quelle a la source.
                req_headers = {"User-Agent": "Xbox/Downloader"}
                if rng:
                    req_headers["Range"] = rng
                method = "HEAD" if is_head else "GET"
                req = urllib.request.Request(url, headers=req_headers, method=method)
                try:
                    up = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
                except urllib.error.HTTPError as e:
                    # la source renvoie une erreur (403, 404, 416...) : on la repercute
                    app.log_threadsafe(f"  -> source {e.code} ({e.reason})")
                    self.send_response(e.code)
                    if e.headers.get("Content-Range"):
                        self.send_header("Content-Range", e.headers["Content-Range"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    e.close()
                    return
                except Exception as e:
                    app.log_threadsafe(f"  -> erreur source : {e}")
                    self.send_response(502)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                with up:
                    status = up.status  # 200 (complet) ou 206 (partiel)
                    h = up.headers
                    clen = h.get("Content-Length")
                    crange = h.get("Content-Range")
                    ctype = h.get("Content-Type", "application/octet-stream")

                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    if h.get("Accept-Ranges"):
                        self.send_header("Accept-Ranges", h["Accept-Ranges"])
                    # Fermer aussi en cas de source tronquee : aucun message
                    # suivant ne doit etre interprete comme la suite du fichier.
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    if clen is not None:
                        self.send_header("Content-Length", clen)
                    if crange:
                        self.send_header("Content-Range", crange)
                    self.end_headers()
                    app.log_threadsafe(f"  -> proxy {status}"
                                       + (f" {crange}" if crange else "")
                                       + (f" ({clen} o)" if clen else ""))
                    if is_head:
                        return
                    while True:
                        try:
                            buf = up.read(CHUNK)
                        except Exception as exc:
                            self.close_connection = True
                            app.log_threadsafe(f"Flux source interrompu : {exc}")
                            break
                        if not buf:
                            break
                        try:
                            self.wfile.write(buf)
                        except (BrokenPipeError, ConnectionResetError):
                            break

            def log_message(self, fmt, *args):
                pass

        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
            self.log_threadsafe(f"Serveur HTTP en ecoute sur le port {HTTP_PORT}.")
            self.http_ready.set()
            self.server.serve_forever()
        except OSError as e:
            self.log_threadsafe(describe_http_bind_error(HTTP_PORT, e))
        except Exception as e:
            self.log_threadsafe(f"Erreur serveur HTTP : {e}")
        finally:
            if self.server:
                self.server.server_close()

    # ---------- sonde HTTPS (diagnostic) ----------
    def run_https_probe(self):
        """Ecoute sur le 443 et logue qui s'y connecte + le domaine (SNI).
        Ce n'est PAS un vrai serveur HTTPS : on lit le ClientHello, on logue,
        on ferme. But : si ca se declenche au moment du telechargement, c'est
        que la console passe en HTTPS -> d'ou l'absence de GET sur le port 80."""
        stop_event = self.https_stop
        if stop_event is None:
            return
        srv = None
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", 443))
            srv.listen(8)
            srv.settimeout(1.0)
        except Exception as e:
            if srv is not None:
                srv.close()
            self.log_threadsafe(f"Sonde 443 non demarree : {e} "
                                f"(un autre logiciel occupe le 443 ?)")
            return
        self.log_threadsafe("Sonde HTTPS en ecoute sur le port 443 (diagnostic).")
        while not stop_event.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(2.0)
                data = conn.recv(2048)
                sni = parse_sni(data)
                if sni:
                    self.log_threadsafe(
                        f"!! HTTPS (443) depuis {addr[0]} -> {sni}  "
                        f"=> la console telecharge en HTTPS, rien n'arrivera sur le 80")
                    self._post_ui(self._set_console,
                                  f"Client {addr[0]} - tentative HTTPS 443 (voir logs)", "orange")
                else:
                    self.log_threadsafe(
                        f"!! HTTPS (443) depuis {addr[0]} (TLS, SNI illisible)")
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            srv.close()
        except Exception:
            pass
        self.log_threadsafe("Sonde 443 arretee.")

    def on_close(self):
        self.save_config()
        self.close_requested = True
        if self.active or self.starting or self.stopping:
            self.stop()
        else:
            self.cleanup()
            self.closing = True
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = XboxRedirectApp(root)
    root.mainloop()
