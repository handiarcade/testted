"""Controle des applications installees via OpenXbox WebAPI / SmartGlass.

Aucune connexion n'est tentee a l'import. Les jetons restent dans les donnees
locales de l'utilisateur, hors du dossier de l'application.
"""
import asyncio
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import webbrowser


class SmartglassError(RuntimeError):
    """Erreur affichable sans URL OAuth ni jeton."""


def describe_service_error(error, stage):
    """Classer une panne sans reproduire le contenu des reponses ou des jetons."""
    import httpx
    from pydantic import ValidationError
    from xbox.webapi.common.exceptions import AuthenticationException

    prefix = f"SmartGlass / {stage} : "
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        message = prefix + f"le service Microsoft/Xbox a repondu HTTP {status}."
        if status in (400, 401):
            message += " Reconnectez votre compte Microsoft."
        elif status == 403:
            message += " Acces refuse par le service."
        elif status == 429:
            message += " Trop de requetes ; reessayez plus tard."
        return message
    if isinstance(error, AuthenticationException):
        return prefix + "authentification Xbox/XSTS refusee. Reconnectez votre compte Microsoft."
    if isinstance(error, ValidationError):
        # Le texte complet d'une ValidationError peut contenir access_token.
        issues = error.errors(include_input=False, include_url=False, include_context=False)
        fields = []
        for issue in issues[:8]:
            location = issue.get('loc', ())
            field = str(location[-1]) if location else '(racine)'
            fields.append(field if field.isidentifier() and len(field) <= 48 else '(champ)')
        return prefix + "format de reponse incompatible avec OpenXbox (champs : " + ', '.join(fields) + ")."
    if isinstance(error, json.JSONDecodeError):
        return prefix + "le service a retourne une reponse JSON illisible."
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return prefix + "verification du certificat TLS refusee ; verifier la date du PC et les certificats Windows."
        current = current.__cause__ or current.__context__
    if isinstance(error, httpx.ProxyError):
        return prefix + "connexion au proxy impossible (ProxyError)."
    if isinstance(error, httpx.TimeoutException):
        return prefix + "delai de connexion ou de reponse depasse (" + type(error).__name__ + ")."
    if isinstance(error, httpx.RequestError):
        return prefix + "requete reseau echouee (" + type(error).__name__ + ")."
    return prefix + "erreur locale ou de compatibilite (" + type(error).__name__ + ")."


def _default_token_path():
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "XboxDownloader" / "smartglass_tokens.json"


def _library():
    try:
        from xbox.webapi.api.client import XboxLiveClient
        from xbox.webapi.authentication.manager import AuthenticationManager
        from xbox.webapi.authentication.models import OAuth2TokenResponse
        from xbox.webapi.common.signed_session import SignedSession
        from xbox.webapi.scripts import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI
    except ImportError as exc:
        raise SmartglassError(
            "SmartGlass indisponible : installez xbox-webapi==2.1.0 avec le lanceur."
        ) from exc
    except OSError as exc:
        raise SmartglassError("Le dossier de connexion local OpenXbox est inaccessible.") from exc
    return SimpleNamespace(
        XboxLiveClient=XboxLiveClient, AuthenticationManager=AuthenticationManager,
        OAuth2TokenResponse=OAuth2TokenResponse, SignedSession=SignedSession,
        CLIENT_ID=CLIENT_ID, CLIENT_SECRET=CLIENT_SECRET, REDIRECT_URI=REDIRECT_URI,
    )


def _callback_handler(expected_state, results):
    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            # La ligne de requete contient le code OAuth : ne jamais la journaliser.
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            states = query.get("state", [])
            valid_state = len(states) == 1 and hmac.compare_digest(
                states[0].encode("utf-8"), expected_state.encode("utf-8")
            )
            code = query.get("code", [])
            if parsed.path != "/auth/callback" or not valid_state:
                self._reply(400, b"Callback refuse. Revenez a l'application.")
                return
            if query.get("error"):
                self._put(SmartglassError("Connexion Microsoft annulee ou refusee."))
                self._reply(400, b"Connexion annulee ou refusee.")
                return
            if len(code) != 1 or not code[0]:
                self._reply(400, b"Code de connexion absent.")
                return
            self._put(code[0])
            self._reply(200, b"Connexion recue. Vous pouvez fermer cet onglet.")

        def _put(self, result):
            try:
                results.put_nowait(result)
            except queue.Full:
                pass

        def _reply(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    return CallbackHandler


class _CallbackServer(HTTPServer):
    allow_reuse_address = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address


class SmartglassClient:
    LOGIN_TIMEOUT = 120
    OPERATION_TIMEOUT = 15
    POLL_INTERVAL = 0.75

    def __init__(self, token_path=None):
        self.token_path = Path(token_path) if token_path is not None else _default_token_path()

    async def _interactive_login(self, manager, on_auth_url):
        state = secrets.token_urlsafe(32)
        results = queue.Queue(maxsize=1)
        try:
            server = _CallbackServer(("127.0.0.1", 8080), _callback_handler(state, results))
        except OSError as exc:
            raise SmartglassError(
                "Le port local 8080 est occupe. Fermez l'autre connexion Microsoft puis reessayez."
            ) from exc
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = manager.generate_authorization_url(state=state)
            if on_auth_url:
                on_auth_url(url)
            opened = await asyncio.to_thread(webbrowser.open, url)
            if not opened:
                raise SmartglassError(
                    "Impossible d'ouvrir le navigateur du PC. Configurez un navigateur par defaut puis recommencez."
                )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.LOGIN_TIMEOUT
            while True:
                try:
                    result = results.get_nowait()
                    break
                except queue.Empty:
                    if loop.time() >= deadline:
                        raise SmartglassError("Connexion Microsoft expiree. Recommencez la connexion.")
                    await asyncio.sleep(0.1)
            if isinstance(result, SmartglassError):
                raise result
            await manager.request_tokens(result)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=3)

    def _save_tokens(self, oauth):
        if oauth is None:
            return
        temporary = None
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".smartglass-", suffix=".tmp", dir=self.token_path.parent
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                stream.write(oauth.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.token_path)
        except OSError as exc:
            raise SmartglassError("Impossible d'enregistrer la connexion dans vos donnees locales.") from exc
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _check_status(response):
        if isinstance(response, dict):
            status = response.get("status")
            code = status.get("errorCode") if isinstance(status, dict) else None
        else:
            status = getattr(response, "status", None)
            code = getattr(status, "error_code", None)
        if code != "OK":
            safe_code = code if isinstance(code, str) and code.isalnum() and len(code) <= 80 else "Unknown"
            raise SmartglassError(f"Le service Xbox a refuse l'operation : {safe_code}.")

    async def _execute(self, operation, authenticate=False, on_auth_url=None,
                       stage="requete Xbox"):
        phase = "initialisation OpenXbox"
        try:
            lib = _library()
            async with lib.SignedSession() as session:
                manager = lib.AuthenticationManager(
                    session, lib.CLIENT_ID, lib.CLIENT_SECRET, lib.REDIRECT_URI
                )
                have_tokens = False
                phase = "lecture de la connexion locale"
                if self.token_path.exists():
                    try:
                        if self.token_path.stat().st_size > 256 * 1024:
                            raise ValueError("Token file too large")
                        manager.oauth = lib.OAuth2TokenResponse.model_validate_json(
                            self.token_path.read_text(encoding="utf-8")
                        )
                        have_tokens = True
                    except (ValueError, OSError):
                        if not authenticate:
                            raise SmartglassError("Connexion locale illisible. Reconnectez le compte Microsoft.")
                if have_tokens:
                    phase = "renouvellement de la connexion Microsoft/Xbox"
                    try:
                        await manager.refresh_tokens()
                    except Exception as exc:
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        if not authenticate or status not in (400, 401):
                            raise
                        phase = "connexion Microsoft/Xbox"
                        await self._interactive_login(manager, on_auth_url)
                elif authenticate:
                    phase = "connexion Microsoft/Xbox"
                    await self._interactive_login(manager, on_auth_url)
                else:
                    raise SmartglassError("Connectez d'abord votre compte Microsoft avec le bouton Connexion.")
                self._save_tokens(manager.oauth)
                phase = "preparation du client Xbox"
                client = lib.XboxLiveClient(manager)
                try:
                    phase = stage
                    return await operation(client.smartglass)
                finally:
                    # Les requetes du fournisseur peuvent renouveler les jetons.
                    self._save_tokens(manager.oauth)
        except SmartglassError:
            raise
        except Exception as exc:
            raise SmartglassError(describe_service_error(exc, phase)) from exc

    async def list_consoles(self, authenticate=True, on_auth_url=None):
        async def operation(provider):
            response = await provider.get_console_list()
            self._check_status(response)
            return [
                {"id": console.id, "name": console.name,
                 "remote_enabled": bool(console.remote_management_enabled),
                 "power_state": getattr(console.power_state, "value", console.power_state)}
                for console in response.result
            ]
        return await self._execute(operation, authenticate, on_auth_url,
                                   stage="liste des consoles")

    async def list_apps(self, device_id):
        if not isinstance(device_id, str) or not device_id.strip():
            raise SmartglassError("Selectionnez une console.")
        async def operation(provider):
            from pydantic import ValidationError
            try:
                response = await provider.get_installed_apps(device_id)
            except ValidationError:
                # Le modele SDK exige aussi des metadonnees d'installation dont
                # nous n'avons pas besoin pour lancer une application.
                response = await provider._fetch_list("installedApps", {"deviceId": device_id})
                payload = response.json()
                if not isinstance(payload, dict):
                    raise SmartglassError("Liste des applications : reponse JSON de format inattendu.")
                self._check_status(payload)
                packages = payload.get("result")
                if not isinstance(packages, list) or any(not isinstance(item, dict) for item in packages):
                    raise SmartglassError("Liste des applications : champ result invalide.")
                apps = []
                for package in packages:
                    product_id = package.get("oneStoreProductId")
                    if not isinstance(product_id, str) or not product_id.strip():
                        continue
                    name = package.get("name")
                    apps.append({"id": product_id,
                                 "name": name if isinstance(name, str) and name else product_id})
                return apps
            self._check_status(response)
            return [
                {"id": package.one_store_product_id,
                 "name": package.name or package.one_store_product_id}
                for package in response.result if package.one_store_product_id
            ]
        return await self._execute(operation, stage="chargement des applications")

    async def launch(self, device_id, product_id):
        if not isinstance(device_id, str) or not device_id.strip():
            raise SmartglassError("Selectionnez une console.")
        if not isinstance(product_id, str) or not product_id.strip():
            raise SmartglassError("Selectionnez une application installee.")
        async def operation(provider):
            response = await provider.launch_app(device_id, product_id)
            self._check_status(response)
            operation_id = response.op_id
            if not operation_id:
                raise SmartglassError("Xbox n'a pas fourni d'identifiant de commande.")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.OPERATION_TIMEOUT
            while loop.time() < deadline:
                try:
                    report = await asyncio.wait_for(
                        provider.get_op_status(device_id, operation_id),
                        timeout=max(0.01, deadline - loop.time()),
                    )
                except asyncio.TimeoutError:
                    break
                self._check_status(report)
                for node in report.op_status_list:
                    if node.op_id != operation_id:
                        continue
                    status = getattr(node.operation_status, "value", node.operation_status)
                    if status == "Succeeded":
                        if not node.succeeded:
                            raise SmartglassError("La console n'a pas confirme le lancement.")
                        return {"status": "succeeded", "operation_id": operation_id}
                    if status in ("Error", "OffConsoleError", "TimedOut"):
                        raise SmartglassError(f"La console n'a pas lance l'application ({status}).")
                await asyncio.sleep(min(self.POLL_INTERVAL, max(0, deadline - loop.time())))
            return {"status": "pending", "operation_id": operation_id}
        return await self._execute(operation, stage="lancement de l'application")
