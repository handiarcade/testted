"""Acces au portail officiel de la Xbox, disponible en mode developpeur."""
import ipaddress
import re
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit
import webbrowser


class DevPortalError(ValueError):
    """Erreur de saisie ou d'ouverture du navigateur, sans identifiants."""


def portal_url(address, port='11443'):
    text = str(address).strip()
    if not text or any(character.isspace() for character in text) or '\\' in text:
        raise DevPortalError("Saisissez l'adresse IP ou le nom reseau de la Xbox affiche dans Dev Home.")
    # Une adresse IPv6 nue doit etre encadree avant l'analyse de l'URL.
    try:
        direct_ip = ipaddress.ip_address(text)
    except ValueError:
        direct_ip = None
    if isinstance(direct_ip, ipaddress.IPv6Address):
        text = '[' + text + ']'
    try:
        parsed = urlsplit(text if '://' in text else 'https://' + text)
        if (parsed.scheme.lower() != 'https' or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or not parsed.hostname):
            raise ValueError('Adresse de portail invalide')
        host = parsed.hostname
        number = parsed.port if parsed.port is not None else int(port)
        if not 1 <= number <= 65535:
            raise ValueError('Port invalide')
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode('idna').decode('ascii').lower()
            if (len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                         for label in host.rstrip('.').split('.'))):
                raise ValueError('Nom reseau invalide')
        else:
            if '%' in host:
                raise ValueError('Adresse IPv6 avec zone non prise en charge')
            host = f'[{ip.compressed}]' if isinstance(ip, ipaddress.IPv6Address) else str(ip)
        return f'https://{host}:{number}/'
    except (ValueError, UnicodeError):
        raise DevPortalError("Adresse invalide : utilisez une IP, un nom reseau ou l'URL HTTPS du portail, sans identifiants ni sous-chemin.") from None


def open_portal(address, port='11443', browser=None):
    url = portal_url(address, port)
    try:
        opened = (browser or webbrowser.open)(url)
    except Exception:
        raise DevPortalError("Impossible d'ouvrir le navigateur du PC. Verifiez son navigateur par defaut.") from None
    if not opened:
        raise DevPortalError("Le navigateur du PC n'a pas pu etre ouvert.")
    return url


class DevPortalWindow(tk.Toplevel):
    def __init__(self, application):
        super().__init__(application.root)
        self.application = application
        self.title('Xbox - dossiers et applications du mode developpeur')
        self.geometry('760x460')
        self.address = tk.StringVar(master=self)
        self.port = tk.StringVar(master=self, value='11443')
        self.status = tk.StringVar(master=self, value='Console en mode developpeur et portail active requis.')
        ttk.Label(self, text='Dossiers et applications du mode developpeur',
                  font=('Segoe UI', 12, 'bold')).pack(anchor='w', padx=14, pady=12)
        ttk.Label(self, text=(
            'Sur la Xbox : passez en mode developpeur, puis ouvrez Dev Home > Remote Access Settings. '
            'Activez Xbox Device Portal et configurez ses identifiants. '
            'Le changement de mode redemarre la console.'), wraplength=720).pack(anchor='w', padx=14)
        ttk.Label(self, text='IP, nom reseau ou URL HTTPS de la Xbox affiche dans Dev Home :').pack(
            anchor='w', padx=14, pady=(12, 2))
        ttk.Entry(self, textvariable=self.address).pack(fill='x', padx=14)
        row = ttk.Frame(self)
        row.pack(fill='x', padx=14, pady=8)
        ttk.Label(row, text='Port HTTPS :').pack(side='left')
        ttk.Entry(row, textvariable=self.port, width=8).pack(side='left', padx=6)
        ttk.Button(self, text='Acceder aux donnees des applications dev',
                   command=lambda: self.open('files')).pack(anchor='w', padx=14, pady=5)
        ttk.Button(self, text='Gerer / lancer les applications dev dans le portail',
                   command=lambda: self.open('apps')).pack(anchor='w', padx=14, pady=5)
        ttk.Label(self, textvariable=self.status, wraplength=720).pack(anchor='w', padx=14, pady=10)
        ttk.Label(self, text=(
            'En mode normal, SmartGlass ne fournit pas ces dossiers et ne lance pas les applications dev. '
            "Le portail explore les donnees des applications sideloadees, pas les dossiers proteges des jeux du Store."),
            wraplength=720).pack(anchor='w', padx=14)
        ttk.Label(self, text=(
            'La connexion au portail et la confiance de son certificat se gerent dans le navigateur. '
            'Le programme ne stocke pas les identifiants du portail.'), wraplength=720).pack(anchor='w', padx=14, pady=8)

    def open(self, destination):
        try:
            url = open_portal(self.address.get(), self.port.get())
        except DevPortalError as error:
            messagebox.showerror('Xbox Device Portal', str(error), parent=self)
            return
        if destination == 'files':
            text = "Dans le portail ouvert : File Explorer, puis choisissez le paquet de votre application."
        else:
            text = "Dans le portail ouvert : My games & apps, puis Actions > Launch pour l'application dev."
        self.status.set(text)
        self.application.log(f'Portail dev demande dans le navigateur : {url}')
