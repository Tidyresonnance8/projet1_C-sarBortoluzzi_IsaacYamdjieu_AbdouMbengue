import socket
import sys
import argparse
import protocol
from urllib.parse import urlparse
import random
import time


def create_socket_and_send_message(server_addr: str, port: int, path: str, save_path: str) -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.connect((server_addr, port))

        # Requête HTTP 0.9
        request = ("GET " + path).encode('ascii')
        ts_envoi = int(time.time() * 1000) % (2**32)
        packet = protocol.empackage(protocol.PTYPE_DATA, 63, 0, ts_envoi, request)
        sock.send(packet)

        buffer = {}
        seqnum_attendu = 0
        last_data_timestamp = 0  
        transfer_started = False 
        last_ack_sent = None 
        retransmits_initial = 0
        MAX_RETRANSMITS_INITIAL = 5 # Max retries before first byte received

        sock.settimeout(2.0)

        with open(save_path, "wb") as file:
            while True:
                try:
                    data, peer_addr = sock.recvfrom(1040)
                    retransmits_initial = 0
                except socket.timeout:
                    if not transfer_started:
                        # No response yet: retransmit the GET request
                        retransmits_initial += 1
                        if retransmits_initial > MAX_RETRANSMITS_INITIAL:
                            print("Timeout : aucune réponse du serveur, abandon.", file=sys.stderr)
                            break
                        ts_envoi = int(time.time() * 1000) % (2**32)
                        pkt = protocol.empackage(protocol.PTYPE_DATA, 63, 0, ts_envoi, request)
                        sock.send(pkt)
                    else:
                        # Mid-transfer: retransmit our last ACK to prod the server
                        if last_ack_sent is not None:
                            sock.send(last_ack_sent)
                    continue

                packet = protocol.depackage(data)
                if packet is None:
                    continue

                pack_type, window, seqnum, timestamp, payload = packet

                if pack_type == protocol.PTYPE_DATA:
                    if payload == b"":
                        if seqnum == seqnum_attendu:
                            # Echo the fin packet's timestamp in our final ACK
                            pkt_ack = protocol.empackage(protocol.PTYPE_ACK, 63, seqnum_attendu, timestamp, b"")
                            sock.send(pkt_ack)
                            break
                        # Paquet vide hors séquence : ignorer
                        continue

                    transfer_started = True
                    last_data_timestamp = timestamp

                    if seqnum == seqnum_attendu:
                        file.write(payload)
                        seqnum_attendu = (seqnum_attendu + 1) % 2048
                        # Vider le buffer des paquets suivants déjà reçus
                        while seqnum_attendu in buffer:
                            file.write(buffer[seqnum_attendu])
                            del buffer[seqnum_attendu]
                            seqnum_attendu = (seqnum_attendu + 1) % 2048
                    else:
                        # Paquet hors-séquence : on le met en buffer
                        buffer[seqnum] = payload

                    if len(buffer) == 0:
                        pkt_ack = protocol.empackage(protocol.PTYPE_ACK, 63, seqnum_attendu, last_data_timestamp, b"")
                        sock.send(pkt_ack)
                        last_ack_sent = pkt_ack
                    else:
                        list_key = list(buffer.keys())
                        payload_s = protocol.encode_sack(list_key)
                        pkt_sack = protocol.empackage(protocol.PTYPE_SACK, 63, seqnum_attendu, last_data_timestamp, payload_s)
                        sock.send(pkt_sack)
                        last_ack_sent = pkt_sack

        print(f"Fichier sauvegardé dans {save_path}", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--save', default='llm.model', help='Chemin de sauvegarde')
    args = parser.parse_args()

    parsed = urlparse(args.url)
    server_addr = parsed.hostname
    port = parsed.port
    path = parsed.path

    create_socket_and_send_message(server_addr, port, path, args.save)