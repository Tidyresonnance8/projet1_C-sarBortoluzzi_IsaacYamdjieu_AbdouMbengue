import socket
import sys
import os
import argparse
import protocol
import time

def server_multitache(bind_addr: str, bind_port: int, root_dir: str) -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.bind((bind_addr, bind_port))
        clients = {}
        sock.settimeout(0.01)

        print(f"Serveur multi-clients à l'écoute sur {bind_addr}:{bind_port} (root={root_dir})...", file=sys.stderr)

        while True:
            try:
                data, peer_addr = sock.recvfrom(1040)
                packet = protocol.depackage(data)
                if packet is not None:
                    pack_type, window, seqnum, timestamp, payload = packet
                    if peer_addr not in clients:
                        # Nouveau client : doit être une requête DATA non vide
                        if pack_type != protocol.PTYPE_DATA or payload == b"":
                            continue
                        texte = payload.decode('ascii')
                        chemin_extrait = texte.split(' ')
                        if len(chemin_extrait) < 2:
                            continue
                        # BUG FIX #1 : utiliser root_dir au lieu de "."
                        chemin_local = os.path.join(root_dir, chemin_extrait[1].strip().lstrip('/'))
                        print(f"Nouvelle requête : {chemin_local}", file=sys.stderr)
                        try:
                            fichier = open(chemin_local, "rb")
                            clients[peer_addr] = {
                                'file': fichier,
                                'base': 0,
                                'prochain_seqnum': 0,
                                'memoire_envoi': {},
                                'fichier_fini': False,
                                'dernier_contact': time.time(),
                                'rtt': 2.0,
                                'fin_envoye': False,  # BUG FIX #2 : tracker si paquet fin envoyé
                                'last_ack': 0,        # BUG FIX #2 : dernier ACK reçu du client
                            }
                        except FileNotFoundError:
                            print(f"Fichier introuvable : {chemin_local}", file=sys.stderr)
                            # BUG FIX #2 : envoyer paquet vide avec seqnum=0 (premier attendu)
                            pkt_err = protocol.empackage(
                                protocol.PTYPE_DATA, 0, 0, timestamp, b""
                            )
                            sock.sendto(pkt_err, peer_addr)
                            continue
                    else:
                        etat = clients[peer_addr]
                        etat['dernier_contact'] = time.time()
                        if pack_type == protocol.PTYPE_ACK or pack_type == protocol.PTYPE_SACK:
                            # BUG FIX #2 : mémoriser le dernier ACK reçu
                            etat['last_ack'] = seqnum
                            now_ms = int(time.time() * 1000) % (2**32)
                            rtt_ms = (now_ms - timestamp) % (2**32)
                            rtt_sec = rtt_ms / 1000.0
                            if 0 < rtt_sec < 10:
                                etat['rtt'] = 0.875 * etat['rtt'] + 0.125 * rtt_sec
                            while etat['base'] != seqnum:
                                if etat['base'] in etat['memoire_envoi']:
                                    del etat['memoire_envoi'][etat['base']]
                                etat['base'] = (etat['base'] + 1) % 2048
                            if pack_type == protocol.PTYPE_SACK:
                                packet_s = protocol.decode_sack(payload)
                                for numero in packet_s:
                                    if numero in etat['memoire_envoi']:
                                        del etat['memoire_envoi'][numero]

            except socket.timeout:
                pass

            clients_a_supprimer = []
            temps_actuel = time.time()
            for addr, etat in clients.items():
                # Envoyer les données tant que fenêtre disponible et fichier pas fini
                while len(etat['memoire_envoi']) < 63 and not etat['fichier_fini']:
                    morceau = etat['file'].read(1024)
                    if morceau == b"":
                        etat['fichier_fini'] = True
                        break
                    packet = protocol.empackage(
                        protocol.PTYPE_DATA, 63,
                        etat['prochain_seqnum'], 0, morceau
                    )
                    sock.sendto(packet, addr)
                    etat['memoire_envoi'][etat['prochain_seqnum']] = packet
                    etat['prochain_seqnum'] = (etat['prochain_seqnum'] + 1) % 2048

                # BUG FIX #2 : envoyer le paquet de fin avec le bon seqnum
                # Le paquet vide doit avoir seqnum = prochain_seqnum attendu par le client
                # = etat['last_ack'] après que tous les paquets data ont été ACKés
                if etat['fichier_fini'] and len(etat['memoire_envoi']) == 0 and not etat['fin_envoye']:
                    # seqnum du paquet fin = prochain_seqnum (ce que le client attend)
                    pkt_fin = protocol.empackage(
                        protocol.PTYPE_DATA, 0,
                        etat['prochain_seqnum'], 0, b""
                    )
                    sock.sendto(pkt_fin, addr)
                    etat['fin_envoye'] = True
                    etat['pkt_fin'] = pkt_fin
                    etat['fin_time'] = temps_actuel

                # Retransmission timeout adaptatif
                timeout_adaptatif = max(0.5, etat['rtt'] * 2.5)
                if temps_actuel - etat['dernier_contact'] > timeout_adaptatif:
                    if not etat['fin_envoye']:
                        for pkt_sauvegarde in etat['memoire_envoi'].values():
                            sock.sendto(pkt_sauvegarde, addr)
                    elif 'pkt_fin' in etat:
                        # Retransmettre le paquet de fin si pas encore acquitté
                        sock.sendto(etat['pkt_fin'], addr)
                    etat['dernier_contact'] = temps_actuel

                # Considérer le transfert terminé 3 secondes après le paquet fin envoyé
                if etat['fin_envoye'] and temps_actuel - etat.get('fin_time', temps_actuel) > 3.0:
                    print(f"Transfert terminé pour {addr}", file=sys.stderr)
                    etat['file'].close()
                    clients_a_supprimer.append(addr)

            for addr in clients_a_supprimer:
                del clients[addr]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('hostname')
    parser.add_argument('port', type=int)
    parser.add_argument('--root', default='.', help='Dossier racine des fichiers')
    args = parser.parse_args()

    server_multitache(args.hostname, args.port, args.root)