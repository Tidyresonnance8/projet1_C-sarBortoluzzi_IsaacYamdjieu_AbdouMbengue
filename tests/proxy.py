import os
import sys
import random
import socket
import threading
from Helpers import *

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import protocol


#  Proxy UDP capturant — cœur de la stratégie interop avec server et client de reference

class UDPCapturingProxy:
    """
    Proxy UDP transparent intercalé entre client_ref et le serveur réel.

    Architecture :
        client_ref --UDP--> proxy (port proxy_port)
                            proxy --UDP--> server (port server_port)
                            proxy <--UDP-- server
        client_ref <--UDP-- proxy

    Le proxy relaie tous les paquets dans les deux sens.
    Il inspecte également tous les paquets DATA envoyés par le serveur
    vers le client, les décode avec protocol.depackage() et reconstitue
    le contenu du fichier dans self.received_data (bytes ordonnés) avec
    uniquement les paquets pour lesquels le client a renvoyé un ACK.

    Réseau imparfait (activé via imperfect_network=True) :
        - Chaque paquet (dans les deux sens) a une probabilité CORRUPT_PROB
        d'être corrompu (un octet aléatoire est altéré) avant transmission.
        - Chaque paquet a une probabilité DROP_PROB d'être totalement perdu
        (ni transmis, ni mémorisé dans _pending/_chunks).
        - Les deux événements sont indépendants ; un paquet peut être à la
        fois corrompu ET perdu (dans ce cas il est simplement supprimé).

    Usage :
        proxy = UDPCapturingProxy(server_port)
        proxy.start()
        # lancer client_ref vers proxy.proxy_port
        proxy.wait_done(timeout=20)
        proxy.stop()
        data = proxy.get_received_data() # bytes reçus par client_ref
    """

    # Probabilités réseau imparfait
    CORRUPT_PROB = 0.10   # 10 % de chance de corruption d'un paquet
    DROP_PROB    = 0.10   # 10 % de chance de perte totale d'un paquet
    # Combinées : ~1/5 des paquets sont affectés (corrompus OU perdus OU les deux)

    def __init__(self, server_port: int, host: str = HOST, imperfect_network: bool = False):
        self.server_port = server_port
        self.host = host
        self.imperfect_network = imperfect_network
        self.proxy_port = free_port()
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._thread = None
        self._chunks: dict[int, bytes] = {}  # seqnum -> payload (confirmés par ACK)
        self._pending: dict[int, bytes] = {} # seqnum -> payload (vus mais pas encore ACKés)
        self._fin_received = False
        self._lock = threading.Lock()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def wait_done(self, timeout: float = TIMEOUT_TRANSFER) -> bool:
        """Attend que le transfert soit terminé (paquet DATA vide reçu du serveur)."""
        return self._done_event.wait(timeout=timeout)

    # Simulation réseau imparfait

    def _apply_network_imperfections(self, data: bytes) -> bytes | None:
        """
        Applique aléatoirement perte et/ou corruption au paquet.

        Retourne :
        - None   → paquet perdu (à ignorer, ne pas envoyer ni mémoriser)
        - bytes  → paquet à transmettre (potentiellement corrompu)

        Un paquet corrompu a un octet choisi aléatoirement modifié par XOR
        avec une valeur non nulle. Son CRC ne correspondra plus, donc
        protocol.depackage() le rejettera côté récepteur — ce qui garantit
        qu'il ne sera jamais stocké dans _pending ni dans _chunks.
        """
        dropped   = random.random() < self.DROP_PROB
        corrupted = random.random() < self.CORRUPT_PROB

        if dropped:
            # Perte totale : on ne transmet rien
            return None

        if corrupted and len(data) > 0:
            # Corruption : altérer un octet aléatoire
            buf = bytearray(data)
            idx = random.randrange(len(buf))
            buf[idx] ^= random.randint(1, 255)   # XOR non nul → octet différent
            return bytes(buf)

        return data

    def get_received_data(self) -> bytes:
        """
        Reconstruit et retourne les données dans l'ordre des seqnums.

        Le seqnum SRTP est sur 11 bits (0–2047) et se wrap. On reconstitue
        l'ordre en détectant les sauts de wrap : si seqnum[i+1] < seqnum[i]
        on ajoute 2048. On trie ensuite par seqnum absolu.
        """
        with self._lock:
            if not self._chunks:
                return b""
            seqnums = sorted(self._chunks.keys())
            # Déwrapper les seqnums (window max = 63 paquets, wrap à 2048)
            abs_seqnums = []
            offset = 0
            prev = seqnums[0]
            abs_seqnums.append((seqnums[0] + offset, seqnums[0]))
            for s in seqnums[1:]:
                if s < prev - 1024:   # wrap détecté
                    offset += 2048
                abs_seqnums.append((s + offset, s))
                prev = s
            abs_seqnums.sort()
            return b"".join(self._chunks[orig] for _, orig in abs_seqnums)

    # Boucle interne 

    def _run(self):
        """
        Boucle principale du proxy à socket unique.

        Un serveur UDP répond toujours vers l'adresse SOURCE du paquet
        qu'il reçoit. En pratique, le serveur SRTP répond donc toujours
        vers proxy_port. Il n'y a donc qu'un seul socket nécessaire :
        on distingue client et serveur par l'adresse source de chaque
        paquet entrant.

        Si imperfect_network=True, chaque paquet transite par
        _apply_network_imperfections() avant d'être relayé :
        - None  → paquet silencieusement supprimé (perdu)
        - bytes → paquet transmis (intact ou corrompu)
        Les paquets corrompus sont transmis tels quels ; le récepteur les
        détectera via le CRC et les ignorera. Ils ne sont jamais mémorisés
        dans _pending ni _chunks.
        """
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.bind((self.host, self.proxy_port))
        sock.settimeout(0.05)
        server_addr = (self.host, self.server_port)
        client_addr = None  # adresse du client_ref (apprise au 1er paquet)

        try:
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    if self._fin_received:
                        self._done_event.set()
                    continue

                if client_addr is None or addr == client_addr:
                    # Paquet venant du client -> vers le serveur
                    client_addr = addr
                    if self.imperfect_network:
                        to_send = self._apply_network_imperfections(data)
                    else:
                        to_send = data
                    if to_send is not None:
                        sock.sendto(to_send, server_addr)
                        # N'inspecter que le paquet original non corrompu
                        if to_send == data:
                            self._inspect_client_packet(data)
                        # Paquet corrompu : rejeté par le serveur via CRC, rien à mémoriser
                else:
                    # Paquet venant du serveur -> vers le client
                    if self.imperfect_network:
                        to_send = self._apply_network_imperfections(data)
                    else:
                        to_send = data
                    if to_send is not None and client_addr is not None:
                        sock.sendto(to_send, client_addr)
                    # N'inspecter le paquet serveur que s'il est intact et transmis
                    if to_send is not None and to_send == data:
                        self._inspect_server_packet(data)
                    # Paquet perdu ou corrompu : pas de mémorisation dans _pending/_chunks

                if self._fin_received:
                    self._done_event.set()

        finally:
            sock.close()

    def _inspect_server_packet(self, raw: bytes):
        """Décode le paquet DATA serveur->client et le place en attente de confirmation."""
        pkt = protocol.depackage(raw)
        if pkt is None:
            return
        pack_type, window, seqnum, timestamp, payload = pkt
        if pack_type != protocol.PTYPE_DATA:
            return
        if payload == b"":
            # Paquet de fin de transfert
            with self._lock:
                self._fin_received = True
        else:
            with self._lock:
                # Mémoriser le payload, en attente d'un ACK/SACK du client
                if seqnum not in self._pending and seqnum not in self._chunks:
                    self._pending[seqnum] = payload

    def _inspect_client_packet(self, raw: bytes):
        """
        Décode le paquet ACK/SACK client->serveur et confirme les paquets acquittés.

        ACK(n): le client a reçu tous les seqnums jusqu'à n-1 (inclus).
                On confirme depuis _pending tout seqnum s tel que la
                distance circulaire de s à n est positive (s < n mod 2048).
        SACK(n):même chose pour la base cumulative n, plus les seqnums
                listés explicitement dans le payload SACK.
        """
        pkt = protocol.depackage(raw)
        if pkt is None:
            return
        pack_type, window, ack_seqnum, timestamp, payload = pkt
        if pack_type not in (protocol.PTYPE_ACK, protocol.PTYPE_SACK):
            return

        with self._lock:
            # Confirmer tous les seqnums strictement inférieurs à ack_seqnum
            # (distance circulaire sur 2048)
            to_confirm = [
                s for s in list(self._pending)
                if ((ack_seqnum - s) % 2048) > 0 and ((ack_seqnum - s) % 2048) < 1024
            ]
            for s in to_confirm:
                self._chunks[s] = self._pending.pop(s)

            # Pour SACK : confirmer aussi les seqnums acquittés sélectivement
            if pack_type == protocol.PTYPE_SACK and payload:
                for s in protocol.decode_sack(payload):
                    if s in self._pending:
                        self._chunks[s] = self._pending.pop(s)