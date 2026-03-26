import os
import queue
import sys
import time
from io import BufferedReader

from SRTPPacket import *
from socket import socket, AF_INET6, SOCK_DGRAM
RECV_BUFFER_SIZE = 4 * 4 + 1024

LOG_PACKETS = False
TIMESTAMP_MODULO = 2 ** 32

# Pour borner les valeurs de timeout
def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))

# Permet d'obtenir le timestamp actuel
def now_timestamp_bytes():
    now_ms = int(time.monotonic() * 1000) % TIMESTAMP_MODULO
    return now_ms.to_bytes(4, "big")

# Permet de calculer le nombre de secondes écoulées depuis l'envoi d'un paquet à partir de son timestamp
def elapsed_seconds_from_timestamp(timestamp_bytes):
    sent_ms = int.from_bytes(timestamp_bytes, "big")
    now_ms = int(time.monotonic() * 1000) % TIMESTAMP_MODULO
    elapsed_ms = (now_ms - sent_ms) % TIMESTAMP_MODULO
    return elapsed_ms / 1000.0

# Mise à jour de l'estimateur de RTT et du timeout
# srtt : estimation du RTT
# rttvar : estimation de la variance du RTT
# sample_rtt : échantillon de RTT mesuré à partir d'un ACK reçu
def update_rtt_estimator(srtt, rttvar, sample_rtt):
    alpha = 1 / 8 
    beta = 1 / 4
    if srtt is None:
        srtt = sample_rtt
        rttvar = sample_rtt / 2 # Initialisation de rttvar à la moitié du premier échantillon
    else:
        rttvar = (1 - beta) * rttvar + beta * abs(srtt - sample_rtt) # Mise à jour de rttvar en fonction de la différence entre le sample et l'estimation courante
        srtt = (1 - alpha) * srtt + alpha * sample_rtt # Mise à jour de srtt en fonction du sample
    rto = srtt + 4 * rttvar # Calcul du timeout en fonction de l'estimation du RTT et de sa variance
    return srtt, rttvar, rto



# Vérifie si un numéro de séquence est dans la fenêtre d'attente du client
def is_seqnum_in_window(seqnum, base_seqnum, window):
    if window <= 0:
        return False
    return (seqnum - base_seqnum) % 2 ** 11 < window


def server_end_connection(sock: socket, addr, seqnum):
    # send empty packet to end data transfer
    packet = SRTPPacket(PTYPE_DATA, 0, seqnum, b"\x00\x00\x00\x00")
    if LOG_PACKETS: sys.stderr.write(f"--> {packet}\n")
    assert sock.sendto(packet.to_segment(), addr) == len(packet.to_segment())


def get_next_blocks(file_segment_num, window, file: BufferedReader, acked=()):
    file.seek(file_segment_num * 1024, os.SEEK_SET)
    index = 0
    blocks = []
    while file.peek(1024) != b'' and window > 0:
        seqnum = (file_segment_num + index) % 2 ** 11
        if seqnum not in acked:
            blocks.append((seqnum, file.read(1024)))
            window -= 1
        else:
            file.seek(1024, os.SEEK_CUR)
        index += 1
    return blocks


def server_send(sock: socket, q: queue.Queue, addr, file: BufferedReader):
    window = 1
    srtt = None
    rttvar = None
    ack_timeout = 1.0
    consecutive_timeouts = 0
    max_consecutive_timeouts = 50

    file_segment_num = 0
    file_segment_num_mult = 0
    about_to_loop = False
    max_ack_advance = 63

    new_blocks = get_next_blocks(file_segment_num, window, file)
    while new_blocks:
        sent_timestamps = set() # On mémorise les timestamps des paquets envoyés
        for seqnum, data_segment in new_blocks:
            packet_timestamp = now_timestamp_bytes()
            sent_timestamps.add(packet_timestamp)
            packet = SRTPPacket(PTYPE_DATA, 0, seqnum, packet_timestamp, data_segment)
            if LOG_PACKETS: sys.stderr.write(f"--> {packet}\n")
            assert sock.sendto(packet.to_segment(), addr) == len(packet.to_segment())

        # Wait for ack
        selective_acked = ()
        try:
            ack = SRTPPacket.from_segment(q.get(timeout=ack_timeout))
            if LOG_PACKETS: sys.stderr.write(f"<-- {ack}\n")

            if ack.ptype not in (PTYPE_ACK, PTYPE_SACK):
                if LOG_PACKETS: sys.stderr.write(f"ACK ignore (type {ack.ptype})\n")
                continue
            
            #Check si le ACK est dans la fenêtre d'attente du serveur (seqnum cohérent avec les segments envoyés)
            current_seqnum = file_segment_num % 2 ** 11
            ack_advance = (ack.seqnum -current_seqnum) % (2 ** 11)
            if ack_advance > max_ack_advance:
                if LOG_PACKETS: sys.stderr.write(
                    f"ACK ignore : seqnum incoherent (seq={ack.seqnum}, courant={current_seqnum}, avance={ack_advance})\n")
                continue


            window = ack.window
            consecutive_timeouts = 0 

           # Check si le ACK est cohérent avec les segments envoyés (timestamp reconnu) et extraire les éventuels séqnums SACK
            selective_acked = ()
            if ack.ptype == PTYPE_SACK:
                selective_acked = SRTPPacket.decode_sack_payload(ack.payload)
                if not all(is_seqnum_in_window(seqnum, ack.seqnum, max(window, 1)) and seqnum != ack.seqnum
                           for seqnum in selective_acked):
                    if LOG_PACKETS: sys.stderr.write(f"ACK ignore : payload SACK incoherent (seq={ack.seqnum}, window={window}, sacks={selective_acked})\n")
                    continue

            # Check si le ACK correspond à un segment envoyé et met à jour l'estimation du RTT et du timeout, sinon on ignore le ACK pour le calcul du RTT
            if ack.timestamp in sent_timestamps:
                rtt_sample = elapsed_seconds_from_timestamp(ack.timestamp)
                srtt, rttvar, rto = update_rtt_estimator(srtt, rttvar, rtt_sample)
                ack_timeout = clamp(rto, 0.5, 2.0)
            else:
                if LOG_PACKETS: sys.stderr.write("ACK timestamp non reconnu, RTT non mis à jour\n")

            if ack.seqnum > 1000:
                about_to_loop = True
            if ack.seqnum < 64 and about_to_loop:
                about_to_loop = False
                file_segment_num_mult += 1

            file_segment_num = (file_segment_num_mult * 2 ** 11) + ack.seqnum  # todo: fixme, loops at 2048
        except queue.Empty:
            consecutive_timeouts += 1
            if consecutive_timeouts >= max_consecutive_timeouts:
                if LOG_PACKETS: sys.stderr.write("Client semble injoignable, arrêt de la transmission\n")
                return
        except ValueError as val_error:
            if LOG_PACKETS: sys.stderr.write(f"Paquet ignore par le serveur pour la raison : {val_error}\n")
            new_blocks = get_next_blocks(file_segment_num, window, file, acked=selective_acked)     # TODO : not sure about that
            continue

        new_blocks = get_next_blocks(file_segment_num, window, file, acked=selective_acked)

    server_end_connection(sock, addr, file_segment_num % 2 ** 11)
