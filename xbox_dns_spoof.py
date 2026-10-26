"""
DNS Spoof simple et plus fiable pour les domaines Xbox.

Utilisable de deux facons :
- en standalone : `python xbox_dns_spoof.py`
- importe par l'appli : `run_dns(log=..., stop_event=...)` (meme processus)

Nouveautes :
- RELAIS DNS (fix du 2026-10-26) : tout domaine qui n'est PAS un domaine de
  contenu Xbox est maintenant transmis a un vrai resolveur (8.8.8.8 par defaut,
  secours 1.1.1.1) et sa vraie reponse est renvoyee a la console. Avant, ces
  domaines ne recevaient AUCUNE reponse (la console = PC comme seul DNS), d'ou
  une connexion qui marchait "des fois oui des fois non". Seuls les domaines de
  contenu sont detournes.
- log optionnel de TOUTES les requetes DNS (log_all=True) pour decouvrir
  quels domaines la console contacte reellement.
- liste de domaines surchargeable (argument `domains`).

NOTE IMPORTANT (fix du 2026-10-26) :
- `packages.xboxlive.com` a ete RETIRE de la liste.
  Ce domaine est contacte par la console pour les metadonnees / licences,
  souvent en HTTPS (port 443). Comme ce PC ne sert que du HTTP (port 80),
  le spoofer detourne une requete qui DOIT atteindre les vrais serveurs
  Microsoft -> le telechargement avortait avant meme d'aller chercher le .xvc
  (aucun GET n'arrivait sur le serveur HTTP).
  On ne spoofe donc QUE les domaines de contenu (assets / dlassets / xvcf...).
"""
import socket
import os
import threading

XBOX_DOMAINS = [
    b"assets1.xboxlive.com",
    b"assets2.xboxlive.com",
    b"dlassets.xboxlive.com",
    b"dlassets2.xboxlive.com",
    b"d1.xboxlive.com",
    b"d2.xboxlive.com",
    b"xvcf1.xboxlive.com",
    b"xvcf2.xboxlive.com",
    b"download.xbox.com",
    # b"packages.xboxlive.com",  # <- RETIRE : HTTPS/licence, doit aller chez MS
]


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def parse_name(data, offset):
    """Decode un nom DNS, y compris la compression, avec bornes et anti-boucle."""
    labels, seen, resume = [], set(), None
    wire_length = 1
    while True:
        if offset >= len(data) or offset in seen:
            raise ValueError("Nom DNS tronque ou compression cyclique")
        seen.add(offset)
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("Pointeur DNS tronque")
            if resume is None:
                resume = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        if length & 0xC0:
            raise ValueError("Label DNS invalide")
        offset += 1
        if length == 0:
            return b".".join(labels), resume if resume is not None else offset
        if offset + length > len(data):
            raise ValueError("Label DNS tronque")
        wire_length += length + 1
        if wire_length > 255:
            raise ValueError("Nom DNS trop long")
        labels.append(data[offset:offset + length])
        offset += length


def get_question(query):
    if len(query) < 12 or query[2] & 0xF8 or query[4:6] != b"\x00\x01":
        raise ValueError("Requete DNS standard a une question requise")
    name, offset = parse_name(query, 12)
    if offset + 4 > len(query) or query[offset + 2:offset + 4] != b"\x00\x01":
        raise ValueError("Question DNS tronquee ou classe non IN")
    # Reencoder le nom : les pointeurs de la requete ne sont pas reutilisables.
    question = b"".join(bytes([len(label)]) + label for label in name.split(b".") if label)
    question += b"\x00" + query[offset:offset + 4]
    return question, int.from_bytes(query[offset:offset + 2], "big")


def matches_domain(name, domains):
    name = name.lower().rstrip(b".")
    for domain in domains:
        if isinstance(domain, str):
            domain = domain.encode("ascii")
        domain = domain.lower().rstrip(b".")
        if domain and (name == domain or name.endswith(b"." + domain)):
            return True
    return False


def build_response(query, ip):
    tid = query[:2]
    question, qtype = get_question(query)

    # On ne forge une reponse A (type 1) que pour une question A.
    # Pour tout le reste (ex. AAAA / IPv6), on renvoie 0 answer pour
    # eviter une reponse malformee et le contournement par IPv6.
    if qtype != 1:
        flags = b"\x81\x80"
        counts = b"\x00\x01\x00\x00\x00\x00\x00\x00"  # 1 question, 0 answer
        return tid + flags + counts + question

    flags = b"\x81\x80"
    counts = b"\x00\x01\x00\x01\x00\x00\x00\x00"  # 1 question, 1 answer

    answer = b"\xc0\x0c"            # pointeur vers le nom
    answer += b"\x00\x01"           # type A
    answer += b"\x00\x01"           # classe IN
    answer += b"\x00\x00\x00\x3c"   # TTL 60
    answer += b"\x00\x04"           # rdlength
    answer += socket.inet_aton(ip)

    return tid + flags + counts + question + answer


def forward_query(query, client_addr, reply_sock, upstreams, log=print, log_all=False,
                  name=""):
    """Transmet une requete DNS a un vrai resolveur et renvoie la reponse au
    client. Essaie chaque upstream dans l'ordre. En cas d'echec total, ne
    repond rien (le client reessaiera)."""
    for up in upstreams:
        f = None
        try:
            f = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            f.settimeout(3.0)
            f.connect((up, 53))
            f.send(query)
            resp = f.recv(65535)
            if len(resp) < 12 or resp[:2] != query[:2] or not resp[2] & 0x80:
                continue
            reply_sock.sendto(resp, client_addr)
            if log_all:
                log(f"  (relai {up}) {name} <- {client_addr[0]}")
            return
        except Exception:
            continue
        finally:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
    log(f"  (relai echoue) {name} <- {client_addr[0]}")


def run_dns(log=print, stop_event=None, local_ip=None, domains=None, log_all=False,
            on_client=None, upstream="8.8.8.8", on_ready=None,
            bind_ip="0.0.0.0", port=53):
    """Boucle DNS = relais + spoof. `log` est un callable(str). `stop_event` un
    threading.Event. `log_all=True` affiche aussi les requetes relayees.
    `on_client` est un callable(ip) appele des qu'un nouveau client (= la console)
    envoie une requete DNS -> permet de signaler la console sans attendre un GET.
    `upstream` = vrai DNS vers lequel on relaie tout ce qui n'est PAS un domaine
    de contenu Xbox (auth, login, licences...), pour que la console reste
    connectee normalement. Seuls les domaines de `domains` sont detournes vers
    `local_ip`. Renvoie True si demarre correctement, False si le bind echoue."""
    if local_ip is None:
        local_ip = get_local_ip()
    if stop_event is None:
        stop_event = threading.Event()
    if domains is None:
        domains = XBOX_DOMAINS
    # Resolveurs de secours : l'upstream choisi, puis Cloudflare, puis Google.
    upstreams = []
    for up in (upstream, "1.1.1.1", "8.8.8.8"):
        if up and up not in upstreams:
            upstreams.append(up)

    slots = threading.BoundedSemaphore(32)
    workers = set()
    workers_lock = threading.Lock()

    def relay(*args, **kwargs):
        try:
            forward_query(*args, **kwargs)
        finally:
            with workers_lock:
                workers.discard(threading.current_thread())
            slots.release()

    log(f"DNS Spoof -> {local_ip}")
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind_ip, port))
        sock.settimeout(1.0)
    except Exception as e:
        log(f"Erreur bind port 53 : {e} (lance bien en administrateur)")
        if sock is not None:
            sock.close()
        return False

    if on_ready is not None:
        on_ready(sock.getsockname()[1])
    log(f"En ecoute sur le port 53 (relai -> {upstreams[0]}).")
    last_client = None
    try:
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            if len(data) < 12:
                continue
            try:
                get_question(data)
                name, _ = parse_name(data, 12)
            except Exception:
                continue

            # Nouveau client qui nous parle en DNS = la console. On le signale
            # tout de suite (une seule fois par IP), sans attendre un telechargement.
            client_ip = addr[0]
            if on_client is not None and client_ip != last_client:
                last_client = client_ip
                try:
                    on_client(client_ip)
                except Exception:
                    pass

            name_str = name.decode(errors="ignore")
            name_lower = name.lower()
            matched = matches_domain(name_lower, domains)
            if matched:
                # Domaine de contenu Xbox -> on detourne vers notre PC.
                # (build_response ne forge un A que pour une question A ; pour
                #  AAAA il renvoie 0 answer, ce qui force la console sur notre IPv4.)
                log(f"Spoof {name_str} -> {local_ip} pour {addr[0]}")
                resp = build_response(data, local_ip)
                sock.sendto(resp, addr)
            else:
                # Tout le reste (auth, login, licences, Windows Update...) est
                # relaye vers un vrai DNS, en thread pour ne pas bloquer la boucle.
                if not slots.acquire(blocking=False):
                    continue
                worker = threading.Thread(
                    target=relay,
                    args=(data, addr, sock, upstreams),
                    kwargs={"log": log, "log_all": log_all, "name": name_str},
                    daemon=True,
                )
                with workers_lock:
                    workers.add(worker)
                worker.start()
    except Exception as e:
        log(f"Erreur DNS : {e}")
    finally:
        with workers_lock:
            pending = list(workers)
        for worker in pending:
            worker.join()
        sock.close()
        log("DNS arrete.")
    return True


def main():
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dns_error.log")

    def file_log(msg):
        print(msg, flush=True)

    ok = run_dns(log=file_log, log_all=True)
    if not ok:
        with open(log_path, "w") as f:
            f.write("Erreur bind port 53. Lance bien en administrateur.\n")
            f.write("Verifie avec : netstat -ano | findstr :53\n")
        input("Appuie sur Entree pour fermer...")


if __name__ == "__main__":
    main()
