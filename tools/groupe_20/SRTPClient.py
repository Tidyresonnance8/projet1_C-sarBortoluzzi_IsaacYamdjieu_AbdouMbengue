import sys
from io import BufferedWriter

from time import time_ns
from SRTPPacket import *

CONNECTION_TRIES = 150
WINDOW_RECEPTION_TIMEOUT = 1000 # in us
max_consecutive_timeouts = 40
LOG_PACKETS = False
RECV_BUFFER_SIZE = 4 * 4 + 1024


class SRTPClient:
    def __init__(self, sock, servername):
        self.last_data_timestamp = b"\x00\x00\x00\x00"
        self.next_seqnum = 0
        self.window_len = 63
        self.sock = sock
        self.window = []
        self.last_valid_ack_segment = SRTPPacket(PTYPE_ACK, self.window_len, self.next_seqnum,
                                                 timestamp=self.last_data_timestamp).to_segment()
        
        first_packet = self.client_establish_connection(servername)
        self.window = [first_packet] if first_packet is not None else []

    def is_connected(self):
        return len(self.window) != 0

    def client_send_get(self, servername: str):
        pstr = servername.split("/")
        piloupilou = pstr[2].split(":")
        hostname = ':'.join(piloupilou[:-1])
        if hostname[-1] == "]":
            hostname = hostname[:-1]
        if hostname[0] == "[":
            hostname = hostname[1:]
        port = piloupilou[-1]
        http_request = b"GET /" + '/'.join(pstr[3:]).encode()
        self.sock.connect((hostname, int(port)))

        packet = SRTPPacket(PTYPE_DATA, 0, 0, b"\x00\x00\x00\x00", http_request)
        if LOG_PACKETS: sys.stderr.write(f"--> {packet}\n")
        assert self.sock.send(packet.to_segment()) == len(packet.to_segment())

    # Returns first data packet
    def client_establish_connection(self, servername: str):
        # Strategy given a maximum 1 way delay of 2s
        # Send CONNECTION_TRIES packets spaced at short intervals to combat packet loss
        # Monitor for 4 additional seconds in case any packets arrive to combat delay
        self.sock.settimeout(0.04)
        for _ in range(CONNECTION_TRIES):
            self.client_send_get(servername)
            try:
                packet = SRTPPacket.from_segment(self.sock.recv(RECV_BUFFER_SIZE))
                self.last_data_timestamp = packet.timestamp
                return packet
            except TimeoutError:
                pass
            except ValueError as e:
                if LOG_PACKETS: sys.stderr.write(f"Paquet ignore en raison de : {e}\n")
        self.sock.settimeout(4)
        while True:
            try:
                packet = SRTPPacket.from_segment(self.sock.recv(RECV_BUFFER_SIZE))
                self.last_data_timestamp = packet.timestamp
                return packet
            except ValueError as e:
                if LOG_PACKETS: sys.stderr.write(f"Paquet ignore pour la raison : {e}\n")
                continue
            except TimeoutError:
                break
            

    def client_ack(self):
        # all in order packets have been processed, only out-of-order packets left in window
        # Send ACK or SACK if receiver already has out-of-order packets
        sack_seqnums = [packet.seqnum for packet in self.window]
        free_window_space = self.window_len - len(self.window)
        if len(sack_seqnums) == 0:
            ack = SRTPPacket(PTYPE_ACK, free_window_space, self.next_seqnum, timestamp=self.last_data_timestamp)
        else:
            sack_payload = SRTPPacket.encode_sack_payload(sack_seqnums)
            ack = SRTPPacket(PTYPE_SACK, free_window_space, self.next_seqnum, timestamp=self.last_data_timestamp,
                             payload=sack_payload)
        self.last_valid_ack_segment = ack.to_segment()
        if LOG_PACKETS: sys.stderr.write(f"--> {ack} \n")
        assert self.sock.send(self.last_valid_ack_segment) == len(self.last_valid_ack_segment)

    """
    Write to file packets that are in order in the window
    Returns False if data transfer is over
    """
    def process_window(self, file: BufferedWriter):
        if len(self.window) == 0:
            return True

        while True:
            try:
                in_order_packet = next(p for p in self.window if p.seqnum == self.next_seqnum)
                self.window.remove(in_order_packet)
                self.next_seqnum = (self.next_seqnum + 1) % 2 ** 11

                # if last packet is of length 0, end data transfer
                if in_order_packet.length == 0:
                    return False
                file.write(in_order_packet.payload)
            except StopIteration:
                break

        self.client_ack()
        return True

    """Return True if packet added"""
    def process_packet(self, packet: SRTPPacket):
        if LOG_PACKETS: sys.stderr.write(f"<-- {packet} \n")
        assert packet.ptype == PTYPE_DATA

        # On mémorise le timestamp du paquet reçu pour l'inclure dans son ACK
        self.last_data_timestamp = packet.timestamp

        # Check if packet is in receive window
        distance = (packet.seqnum - self.next_seqnum) % (2 ** 11)
        if not (0 <= distance < self.window_len):
            if LOG_PACKETS: sys.stderr.write(
                f"Paquet hors fenetre ignore (seq={packet.seqnum}, attendu={self.next_seqnum}, fenetre={self.window_len})\n")
            if LOG_PACKETS: sys.stderr.write("--> Renvoi du dernier ACK valide\n")
            assert self.sock.send(self.last_valid_ack_segment) == len(self.last_valid_ack_segment)
            return False

        # add packet to window if not a duplicate
        if next((p for p in self.window if p.seqnum == packet.seqnum), None) is None:
            self.window.append(packet)
            return True
        else:
            if LOG_PACKETS: sys.stderr.write(f"Paquet duplique recu\n")
            return False

    def client_receive(self, file: BufferedWriter):
        # Timeout cannot be too long, otherwise we never send ack and get stuck receiving the same packets
        receive_timeout = 0.1 # timeout fixe,TODO : est-ce qu'on l'adapte dynamqiquement ?
        self.sock.settimeout(receive_timeout)
        consecutive_timeouts = 0

        while self.process_window(file):
            # receive available packets
            reception_time_of_last_new_packet = time_ns() / 1000
            while len(self.window) < self.window_len and (time_ns()/1000)-reception_time_of_last_new_packet < WINDOW_RECEPTION_TIMEOUT:
                try:
                    packet = SRTPPacket.from_segment(self.sock.recv(RECV_BUFFER_SIZE))
                except TimeoutError:
                    # If the "end_connection" packet is lost, the client should stop running at some point
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= max_consecutive_timeouts:
                         if LOG_PACKETS: sys.stderr.write("Le serveur semble s'etre deconnecte\n")
                         return
                    continue
                except ValueError as val_error:
                    consecutive_timeouts = 0
                    if LOG_PACKETS: sys.stderr.write(f"Paquet ignore pour la raison suivante : {val_error} \n")
                    continue
                consecutive_timeouts = 0
                if self.process_packet(packet):
                    reception_time_of_last_new_packet = time_ns() / 1000